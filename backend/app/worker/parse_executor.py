"""解析任务执行器：剧本解析任务的完整生命周期。
流程：载入剧本版本 → 调用语言模型解析（事务外）→ 归一化 → 结果存 job.result
     → 创建 candidate 解析版本 → 事件写入。
纪律：
- 语言模型调用在短数据库事务之外（AGENTS.md §3-4）
- 事件与状态同事务写入 outbox（AGENTS.md §3-5）
- 失败按 ARCHITECTURE.md §5.5 分类记录 error_code（llm_* 系列）
- 候选解析失败或未确认时当前分镜保持不变（API.md §5 parse）
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.integrations.llm import LLMClient, LLMError
from app.models import EpisodeParseRecord, Job, JobEvent, ParseVersion, ScriptVersion
from app.models import ids
from app.services.script_parse import normalize_parse_output
from app.services.prompt_store import get_prompt
from app.services import style_config


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_event(db: Session, job: Job, event_type: str, payload: dict | None = None) -> None:
    """事件与状态同事务写入 outbox，并回写 job.event_seq 供增量补齐。"""
    ev = JobEvent(job_id=job.id, event_type=event_type, payload=payload or {}, created_at=_now())
    db.add(ev)
    db.flush()
    job.event_seq = ev.event_seq


def _fail_job(db: Session, job: Job, code: str, message: str) -> None:
    job.status = "failed"
    job.error_code = code
    job.error_msg = message[:2000]
    job.finished_at = _now()
    _write_event(db, job, "job.failed", {"error_code": code, "error_msg": job.error_msg})
    db.commit()


def _upsert_records(db: Session, project_id: str, episode_start: int, episode_count: int,
                    status: str, *, error_msg: str | None = None,
                    asset_counts: dict[int, int] | None = None,
                    parse_version_id: str | None = None) -> list[EpisodeParseRecord]:
    """按集 upsert 集级解析记录（asset_type=all：当前单次 LLM 调用产出全部实体分组）。

    状态机：pending → running → succeeded/failed；failed 重试回 running。
    """
    now = _now().replace(tzinfo=None)
    records = []
    for ep in range(episode_start, episode_start + episode_count):
        rec = (
            db.query(EpisodeParseRecord)
            .filter(
                EpisodeParseRecord.project_id == project_id,
                EpisodeParseRecord.episode_no == ep,
                EpisodeParseRecord.asset_type == "all",
            )
            .first()
        )
        if rec is None:
            rec = EpisodeParseRecord(
                id=ids.episode_parse_record_id(), project_id=project_id,
                episode_no=ep, asset_type="all", status="pending",
                attempt_count=0, version=0, created_at=now,
            )
            db.add(rec)
        if status == "running":
            # 仅 pending/failed 可进入 running（防重入）
            if rec.status in ("running", "succeeded"):
                records.append(rec)
                continue
            rec.attempt_count = (rec.attempt_count or 0) + 1
        rec.status = status
        rec.error_msg = error_msg
        rec.updated_at = now
        rec.version = (rec.version or 0) + 1
        if asset_counts is not None and status == "succeeded":
            rec.asset_count = asset_counts.get(ep, 0)
        if parse_version_id:
            rec.parse_version_id = parse_version_id
        records.append(rec)
    db.flush()
    return records


def execute_parse_job(job_id: str) -> dict:
    """执行一个剧本解析任务。返回执行结果摘要。"""
    db: Session = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return {"ok": False, "reason": "job_not_found"}
        if job.status not in ("queued",):
            return {"ok": False, "reason": f"invalid_status:{job.status}"}

        job.status = "running"
        job.started_at = _now()
        job.attempts = (job.attempts or 0) + 1
        _write_event(db, job, "job.started", {"attempt": job.attempts})
        db.commit()

        params = dict(job.params or {})
        script_version_id = params.get("script_version_id")
        episode_start = int(params.get("episode_start") or 1)
        episode_count = int(params.get("episode_count") or 1)
        sv = db.get(ScriptVersion, script_version_id) if script_version_id else None
        if sv is None:
            _fail_job(db, job, "script_version_not_found", "剧本版本不存在")
            return {"ok": False, "reason": "script_version_not_found"}
        text = sv.content_snapshot

        # 集级解析记录置 running（pending/failed 可进入，succeeded/running 跳过）
        _upsert_records(db, job.project_id, episode_start, episode_count, "running")
        db.commit()

        # 项目内已有角色名→ID 映射（供 speaker_id 归一化）
        from app.models import Character
        char_map = {
            c.name: c.id
            for c in db.query(Character).filter(Character.project_id == job.project_id).all()
        }

        # 调用语言模型（事务外）
        from app.models import Project
        project = db.get(Project, job.project_id)
        style_settings = (project.settings if project is not None else {}) or {}
        style_snap = style_config.style_snapshot(style_settings,
                                                 project_id=job.project_id)
        user_prompt = (
            style_config.parse_context_block(style_settings)
            + f"请解析以下剧本，仅解析第 {episode_start} 集到第 {episode_start + episode_count - 1} 集，"
            f"共 {episode_count} 集。范围外的集不要输出。\n\n剧本全文：\n{text}"
        )
        client = LLMClient()
        try:
            raw = client.chat_json(get_prompt("parse_system"), user_prompt)
        except LLMError as e:
            # 集级记录回写 failed（含本次范围内的全部集）
            _upsert_records(db, job.project_id, episode_start, episode_count, "failed", error_msg=e.message[:2000])
            _fail_job(db, job, e.code, e.message)
            return {"ok": False, "reason": e.code}

        # 归一化（P0 强约束）
        norm = normalize_parse_output(
            raw, episode_start=episode_start, episode_count=episode_count,
            character_name_to_id=char_map,
        )

        # 结果落库：完整解析结果存 job.result（API.md §5 parse 契约）
        style_diffs = style_config.compute_style_diff(
            style_settings, norm.data.get("project_analysis"))
        parse_result = {
            **norm.data,
            "style_config_snapshot": style_snap,
            "style_config_diffs": style_diffs,
            "parse_warnings": norm.parse_warnings,
            "review_issues": norm.review_issues,
            "episode_start": episode_start,
            "episode_count": episode_count,
        }

        # 创建 candidate 解析版本（版本号取项目内最大 +1）
        max_ver = (
            db.query(ParseVersion.version)
            .filter(ParseVersion.project_id == job.project_id)
            .order_by(ParseVersion.version.desc())
            .first()
        )
        next_ver = (max_ver[0] if max_ver else 0) + 1
        shot_total = sum(len(sc.get("shots") or []) for sc in norm.data.get("scenes") or [])
        pv = ParseVersion(
            id=ids.parse_version_id(),
            project_id=job.project_id,
            script_version_id=sv.id,
            version=next_ver,
            status="candidate",
            # 口径锁定：写入总集数，供集级状态查询与前端判断解析是否完成使用
            # （此前遗漏该字段，导致历史版本 total_episodes 为空、前端无法判断完整进度）
            total_episodes=episode_start + episode_count - 1,
            diff_summary={
                "characters": len(norm.data.get("characters") or []),
                "props": len(norm.data.get("props") or []),
                "scenes": len(norm.data.get("scenes") or []),
                "shots": shot_total,
                "episodes": len(norm.data.get("episodes") or []),
                "parse_warnings": len(norm.parse_warnings),
                "review_issues": len(norm.review_issues),
            },
            created_at=_now().replace(tzinfo=None),
        )
        db.add(pv)

        # 按集统计镜头数并回写集级记录 succeeded
        per_episode_shots: dict[int, int] = {}
        for sc in norm.data.get("scenes") or []:
            for sh in sc.get("shots") or []:
                ep = int(sh.get("episode") or episode_start)
                per_episode_shots[ep] = per_episode_shots.get(ep, 0) + 1
        _upsert_records(
            db, job.project_id, episode_start, episode_count, "succeeded",
            asset_counts=per_episode_shots, parse_version_id=pv.id,
        )

        job.status = "succeeded"
        job.result = {**parse_result, "parse_version_id": pv.id}
        job.finished_at = _now()
        _write_event(db, job, "job.finished", {"status": "succeeded", "result": {"parse_version_id": pv.id, "shots": shot_total}})
        db.commit()
        return {"ok": True, "parse_version_id": pv.id, "shots": shot_total}
    except Exception as e:  # 兜底：未预期异常按执行错误记录
        db.rollback()
        try:
            job = db.get(Job, job_id)
            if job is not None and job.status not in ("succeeded", "failed"):
                _fail_job(db, job, "llm_error", f"解析执行器异常: {e}")
        except Exception:
            pass
        return {"ok": False, "reason": f"executor_exception:{e}"}
    finally:
        db.close()
