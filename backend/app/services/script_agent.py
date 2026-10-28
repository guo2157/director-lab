"""剧本一体化分析 Agent（script-agent）：评估+解析合并，集数由 Agent 决定。

流程（对应改造设计）：
1. 算法预检：estimate_script 估算集数（不落库、不要求用户确认）
2. 软闸口评估：evaluate_script 生成评分与问题清单（失败不阻断，仅记录事件）
3. 自动分批：按 max_episodes_per_batch 拆批串行解析（复用 parse_system 提示词）
4. 口径锁定：首批成功后将 total_episodes + script_hash 写入 ParseVersion；
   剧本 hash 变化则口径失效，需重新全量分析
5. 首批自动 + 后续半自动：agent_first_batch_auto=true 时仅解析首批，
   前端对未解析集点『继续分析』再次触发（episode_start 指向后续批次）

纪律：与 parse_executor 一致——LLM 调用在事务外，事件与状态同事务写 outbox。
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.integrations.llm import LLMClient, LLMError
from app.models import EpisodeParseRecord, Job, JobEvent, ParseVersion, ScriptVersion
from app.models import ids
from app.services.script_parse import estimate_script, normalize_parse_output
from app.services.prompt_store import get_prompt


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_event(db: Session, job: Job, event_type: str, payload: dict | None = None) -> None:
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
    # 终态清理 delta 事件：失败原因已存 error_code/error_msg，流式事件仅用于过程展示
    db.query(JobEvent).filter(
        JobEvent.job_id == job.id, JobEvent.event_type == "agent.llm_delta"
    ).delete(synchronize_session=False)
    db.commit()


def _upsert_records(db: Session, project_id: str, episode_start: int, episode_count: int,
                    status: str, *, error_msg: str | None = None,
                    asset_counts: dict[int, int] | None = None,
                    parse_version_id: str | None = None) -> None:
    """按集 upsert 集级解析记录（复用 parse_executor 同款状态机语义）。"""
    from app.worker.parse_executor import _upsert_records as _upsert
    _upsert(db, project_id, episode_start, episode_count, status,
            error_msg=error_msg, asset_counts=asset_counts,
            parse_version_id=parse_version_id)


def _find_or_create_parse_version(db: Session, project_id: str, script_version_id: str,
                                  script_hash: str) -> ParseVersion:
    """按 project + script_hash 匹配已有解析版本（candidate 或 current，多批次共用），无则创建。

    兼容 current：批次 1 确认后（status=current）批次 2 继续分析时复用同一 ParseVersion，
    避免版本分裂与 diff_summary 丢失；剧本 hash 变化则不匹配（口径失效，创建新版本）。
    """
    pv = (
        db.query(ParseVersion)
        .filter(
            ParseVersion.project_id == project_id,
            ParseVersion.script_hash == script_hash,
            ParseVersion.status.in_(["candidate", "current"]),
        )
        .order_by(ParseVersion.version.desc())
        .first()
    )
    if pv is not None:
        return pv
    max_ver = (
        db.query(ParseVersion.version)
        .filter(ParseVersion.project_id == project_id)
        .order_by(ParseVersion.version.desc())
        .first()
    )
    next_ver = (max_ver[0] if max_ver else 0) + 1
    pv = ParseVersion(
        id=ids.parse_version_id(),
        project_id=project_id,
        script_version_id=script_version_id,
        version=next_ver,
        status="candidate",
        script_hash=script_hash,
        created_at=_now().replace(tzinfo=None),
    )
    db.add(pv)
    db.flush()
    return pv


def execute_script_agent_job(job_id: str) -> dict:
    """执行一个 script-agent 一体化分析任务。返回执行结果摘要。"""
    from app.core.config import get_settings
    settings = get_settings()
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
        _write_event(db, job, "agent.started", {"attempt": job.attempts})
        db.commit()

        params = dict(job.params or {})
        script_version_id = params.get("script_version_id")
        episode_start = int(params.get("episode_start") or 1)
        sv = db.get(ScriptVersion, script_version_id) if script_version_id else None
        if sv is None:
            _fail_job(db, job, "script_version_not_found", "剧本版本不存在")
            return {"ok": False, "reason": "script_version_not_found"}
        text = sv.content_snapshot
        script_hash = sv.content_hash or ""

        # ---- 0. 幂等防御（2026-10-28）：同剧本版本且同 episode_start 已有成功任务时，
        # 复用其结果直接标记 succeeded，跳过 LLM 评估与解析（接口层已拦截，此处防御
        # 并发提交/绕过接口直建任务等场景，避免重复消耗 LLM 调用）----
        _done = (
            db.query(Job)
            .filter(
                Job.project_id == job.project_id,
                Job.job_type == "script_agent",
                Job.status == "succeeded",
                Job.id != job.id,
            )
            .order_by(Job.created_at.desc())
            .all()
        )
        for _prev in _done:
            _pp = _prev.params or {}
            if _pp.get("script_version_id") == sv.id and int(_pp.get("episode_start") or 1) == episode_start:
                job.status = "succeeded"
                job.result = dict(_prev.result or {})
                job.result["idempotent_reuse"] = True
                job.result["reused_from_job_id"] = _prev.id
                job.finished_at = _now()
                _write_event(db, job, "agent.succeeded", {
                    "idempotent_reuse": True,
                    "reused_from_job_id": _prev.id,
                })
                db.commit()
                return {"ok": True, "idempotent_reuse": True, "reused_from_job_id": _prev.id}

        # ---- 1. 算法预检（不落库）----
        from app.models import Project
        project = db.get(Project, job.project_id)
        settings_map = (project.settings or {}) if project else {}
        episode_duration_target = int(settings_map.get("episode_duration_target", 90))
        max_per_batch = int(settings_map.get("max_episodes_per_batch", 10))
        estimate_data = estimate_script(
            text,
            episode_duration_target=episode_duration_target,
            max_episodes_per_batch=max_per_batch,
        )
        _write_event(db, job, "agent.precheck", {
            "estimated_episodes": estimate_data.get("estimated_episodes"),
            "estimated_total_duration": estimate_data.get("estimated_total_duration"),
            "exceeds_batch_limit": estimate_data.get("exceeds_batch_limit"),
        })
        db.commit()

        # ---- 2. 软闸口评估（失败不阻断）：流式输出评估过程 ----
        evaluation = None
        try:
            from app.services.script_evaluation import evaluate_script

            def _on_eval_delta(delta_text: str) -> None:
                _write_event(db, job, "agent.llm_delta", {"delta": delta_text, "phase": "evaluation"})
                db.commit()

            evaluation = evaluate_script(text, estimate_data, settings_map, on_delta=_on_eval_delta)
            _write_event(db, job, "agent.eval_info", {
                "score": evaluation.get("score"),
                "issues_count": len(evaluation.get("issues") or []),
            })
            db.commit()
        except Exception as e:  # noqa: BLE001
            _write_event(db, job, "agent.eval_skipped", {"reason": str(e)[:200]})
            db.commit()

        # ---- 3. 口径确定：优先已锁定口径，否则用算法估算 ----
        pv = _find_or_create_parse_version(db, job.project_id, sv.id, script_hash)
        total_episodes = pv.total_episodes or int(estimate_data.get("estimated_episodes") or 1)
        total_episodes = max(1, total_episodes)
        if episode_start > total_episodes:
            _fail_job(db, job, "invalid_request", f"episode_start {episode_start} 超出口径总集数 {total_episodes}")
            return {"ok": False, "reason": "out_of_scope"}

        # ---- 4. 批次范围：首批自动 + 后续半自动（agent_first_batch_auto）----
        first_batch_auto = getattr(settings, "agent_first_batch_auto", True)
        batch_end = min(episode_start + max_per_batch - 1, total_episodes)
        if first_batch_auto and episode_start == 1 and total_episodes > max_per_batch:
            batch_end = max_per_batch  # 首次调用只解析首批，后续由用户推进
        episode_count = batch_end - episode_start + 1

        # ---- 5. 逐批 LLM 解析（事务外调用）----
        from app.models import Character
        char_map = {
            c.name: c.id
            for c in db.query(Character).filter(Character.project_id == job.project_id).all()
        }
        client = LLMClient()
        user_prompt = (
            f"请解析以下剧本，仅解析第 {episode_start} 集到第 {batch_end} 集，"
            f"共 {episode_count} 集。范围外的集不要输出。\n\n剧本全文：\n{text}"
        )
        _upsert_records(db, job.project_id, episode_start, episode_count, "running", parse_version_id=pv.id)
        db.commit()
        # 流式解析：LLM 增量输出经 on_delta 回调节流写入 agent.llm_delta 事件（outbox），
        # 前端通过 SSE /api/events 实时查看分析过程；节流策略：≥1s 或累计 ≥240 字符时刷新
        import time as _time
        from app.integrations.llm import parse_llm_json
        _buf: list[str] = []
        _last_flush = _time.monotonic()

        def _flush_delta(force: bool = False) -> None:
            nonlocal _last_flush
            if not _buf:
                return
            now = _time.monotonic()
            if not force and now - _last_flush < 1.0 and sum(len(s) for s in _buf) < 240:
                return
            delta = "".join(_buf)
            _buf.clear()
            _last_flush = now
            _write_event(db, job, "agent.llm_delta", {"delta": delta})
            db.commit()

        def _on_delta(delta_text: str) -> None:
            _buf.append(delta_text)
            _flush_delta()

        try:
            content = client.chat_stream(
                get_prompt("parse_system"), user_prompt, json_mode=True, on_delta=_on_delta,
            )
            _flush_delta(force=True)
            raw = parse_llm_json(content)
        except LLMError as e:
            _upsert_records(db, job.project_id, episode_start, episode_count, "failed",
                            error_msg=e.message[:2000], parse_version_id=pv.id)
            _fail_job(db, job, e.code, e.message)
            return {"ok": False, "reason": e.code}

        norm = normalize_parse_output(
            raw, episode_start=episode_start, episode_count=episode_count,
            character_name_to_id=char_map,
        )

        # ---- 6. 口径锁定：首批成功后写入 total_episodes ----
        if pv.total_episodes is None:
            pv.total_episodes = total_episodes
        pv.script_hash = script_hash

        # 汇总统计与集级记录 succeeded
        shot_total = sum(len(sc.get("shots") or []) for sc in norm.data.get("scenes") or [])
        per_episode_shots: dict[int, int] = {}
        for sc in norm.data.get("scenes") or []:
            for sh in sc.get("shots") or []:
                ep = int(sh.get("episode") or episode_start)
                per_episode_shots[ep] = per_episode_shots.get(ep, 0) + 1
        _upsert_records(db, job.project_id, episode_start, episode_count, "succeeded",
                        asset_counts=per_episode_shots, parse_version_id=pv.id)

        # diff_summary 增量合并（多批次共用同一 ParseVersion）
        prev_summary = pv.diff_summary or {}
        pv.diff_summary = {
            "characters": len(norm.data.get("characters") or []),
            "props": len(norm.data.get("props") or []),
            "scenes": len(norm.data.get("scenes") or []),
            "shots": (prev_summary.get("shots") or 0) + shot_total,
            "episodes": total_episodes,
            "parse_warnings": len(norm.parse_warnings),
            "review_issues": len(norm.review_issues),
        }

        # ---- 7. 终态 ----
        has_more = batch_end < total_episodes
        job.status = "succeeded"
        job.result = {
            **norm.data,
            "parse_warnings": norm.parse_warnings,
            "review_issues": norm.review_issues,
            "episode_start": episode_start,
            "episode_count": episode_count,
            "total_episodes": total_episodes,
            "has_more": has_more,
            "next_episode_start": batch_end + 1 if has_more else None,
            "evaluation": evaluation,
            "parse_version_id": pv.id,
        }
        job.finished_at = _now()
        _write_event(db, job, "agent.succeeded", {
            "parse_version_id": pv.id,
            "episodes": f"{episode_start}-{batch_end}/{total_episodes}",
            "shots": shot_total,
            "has_more": has_more,
        })
        # 终态清理 delta 事件：完整结果已存 job.result，流式事件仅用于过程展示
        db.query(JobEvent).filter(
            JobEvent.job_id == job.id, JobEvent.event_type == "agent.llm_delta"
        ).delete(synchronize_session=False)
        db.commit()
        return {"ok": True, "parse_version_id": pv.id, "shots": shot_total, "has_more": has_more}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        try:
            job = db.get(Job, job_id)
            if job is not None and job.status not in ("succeeded", "failed"):
                _fail_job(db, job, "llm_error", f"Agent 执行器异常: {e}")
        except Exception:
            pass
        return {"ok": False, "reason": f"executor_exception:{e}"}
    finally:
        db.close()
