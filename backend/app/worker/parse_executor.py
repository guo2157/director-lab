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
from app.integrations.llm import LLMClient, LLMError, is_truncated_json_output
from app.models import EpisodeParseRecord, Job, JobEvent, ParseVersion, ScriptVersion, ids
from app.services import style_config
from app.services.prompt_store import get_prompt
from app.services.script_parse import normalize_parse_output


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_event(db: Session, job: Job, event_type: str, payload: dict | None = None) -> None:
    """事件与状态同事务写入 outbox，并回写 job.event_seq 供增量补齐。"""
    ev = JobEvent(job_id=job.id, event_type=event_type, payload=payload or {}, created_at=_now())
    db.add(ev)
    db.flush()
    job.event_seq = ev.event_seq
    # 统一任务编排层桥接：job_event → task_event（Job 归属任务时同步任务阶段状态）
    try:
        from app.services.task_orchestrator import bridge_job_event
        bridge_job_event(db, job_id=job.id, event_type=event_type, payload=payload or {})
    except Exception:  # noqa: BLE001
        pass  # 桥接失败不影响既有执行链路


def _fail_job(db: Session, job: Job, code: str, message: str) -> None:
    job.status = "failed"
    job.error_code = code
    job.error_msg = message[:2000]
    job.finished_at = _now()
    _write_event(db, job, "job.failed", {"error_code": code, "error_msg": job.error_msg})
    db.commit()


def _push_result_to_session(db: Session, job: Job, pv: ParseVersion,
                            data: dict, shot_total: int) -> None:
    """解析完成后把结果摘要回推到发起会话（job.params.session_id）。

    解析是异步耗时任务，用户是在对话框发起它的；若不回推，对话只能停在
    『正在分析』，用户还得自己去页面找结果。这里把集数/场景/镜头/角色/道具
    摘要写回会话，并给出可在对话框继续的下一步。失败不阻断解析主链路。
    """
    session_id = (job.params or {}).get("session_id")
    if not session_id:
        return
    try:
        from sqlalchemy import func, select

        from app.models.agent import AgentMessage, AgentSession
        from app.models.ids import agent_message_id

        session = db.get(AgentSession, session_id)
        if session is None:
            return
        chars = [c.get("name") for c in (data.get("characters") or []) if c.get("name")]
        props = [p.get("name") for p in (data.get("props") or []) if p.get("name")]
        scenes = data.get("scenes") or []
        episodes = data.get("episodes") or []
        content = (
            "剧本分析已完成。\n"
            f"- 解析版本：v{pv.version}（{pv.id}，待确认）\n"
            f"- 集数：{len(episodes) or 1} 集\n"
            f"- 场景：{len(scenes)} 个\n"
            f"- 镜头：{shot_total} 个\n"
            f"- 角色（{len(chars)}）：{'、'.join(chars) if chars else '无'}\n"
            f"- 道具（{len(props)}）：{'、'.join(props) if props else '无'}\n"
            "回复『确认解析结果』即可把解析结果落库，并继续生成角色图/场景图/道具图。"
        )
        current = db.scalar(
            select(func.max(AgentMessage.seq)).where(AgentMessage.session_id == session_id)
        )
        db.add(
            AgentMessage(
                id=agent_message_id(),
                session_id=session_id,
                role="assistant",
                content=content,
                payload={
                    "route_action": "pipeline",
                    "tool": "analyze_script",
                    "async": False,
                    "phase": "analyzed",
                    "job_id": job.id,
                    "parse_version_id": pv.id,
                    "parse_version": pv.version,
                },
                seq=int(current or 0) + 1,
                created_at=_now().replace(tzinfo=None),
            )
        )
        session.updated_at = _now().replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        pass  # 回推失败不影响解析结果落库


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


def _task_progress(db: Session, job_id: str, progress: int) -> None:
    """统一任务编排层：按 job_id 更新关联任务阶段进度（里程碑推进，异常隔离）。"""
    try:
        from app.services.task_orchestrator import update_stage_progress_by_job
        update_stage_progress_by_job(db, job_id, progress)
    except Exception:  # noqa: BLE001
        pass


def _task_step(db: Session, job_id: str, step: str, message: str, progress: int) -> None:
    """写入任务当前步骤与文案（真实里程碑，异常隔离）。

    与 _task_progress 并存：阶段进度用于任务卡片进度条，步骤文案用于对话展示
    『正在做什么』。两者都由确定性代码在真实边界写入，不做模拟推进。
    """
    _task_progress(db, job_id, progress)
    try:
        from app.services.task_orchestrator import set_task_step_by_job
        set_task_step_by_job(db, job_id, step=step, message=message, progress=progress)
    except Exception:  # noqa: BLE001
        pass


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
        _task_step(db, job.id, "reading_script", "正在读取剧本内容", 10)
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

        def _parse_llm(prompt_text: str) -> dict:
            """解析调用：输出被截断时放大 max_tokens 并强制紧凑输出重试一次。

            单集分镜正文很长，上游又不一定回 finish_reason=length，
            表现为 JSON 在字符串中途结束；直接失败会让用户反复重试。
            """
            try:
                return client.chat_json(get_prompt("parse_system"), prompt_text)
            except LLMError as exc:
                truncated = exc.code == "llm_output_truncated" or (
                    exc.code == "llm_invalid_output"
                    and is_truncated_json_output(exc.message)
                )
                if not truncated:
                    raise
                retry_prompt = prompt_text + (
                    "\n\n【输出约束】上一次输出在中途被截断。请重新输出完整 JSON："
                    "紧凑排版（不使用换行缩进）、省略无实际信息的空值字段，确保 JSON 完整闭合。"
                )
                try:
                    return client.chat_json(
                        get_prompt("parse_system"), retry_prompt,
                        max_tokens=min(client.max_tokens * 2, 32768),
                    )
                except LLMError as retry_exc:
                    # 放大后仍失败：保留首次错误语义（截断比 HTTP 报错更准确）
                    if retry_exc.code in ("llm_error", "llm_timeout", "llm_unreachable"):
                        raise exc from retry_exc
                    raise

        try:
            raw = _parse_llm(user_prompt)
        except LLMError as e:
            # 集级记录回写 failed（含本次范围内的全部集）
            _upsert_records(db, job.project_id, episode_start, episode_count, "failed", error_msg=e.message[:2000])
            _fail_job(db, job, e.code, e.message)
            return {"ok": False, "reason": e.code}

        _task_step(db, job.id, "llm_analysis", "正在解析角色、场景、道具与分镜", 50)
        # 归一化（P0 强约束）
        norm = normalize_parse_output(
            raw, episode_start=episode_start, episode_count=episode_count,
            character_name_to_id=char_map,
        )
        shot_total = sum(len(sc.get("shots") or []) for sc in norm.data.get("scenes") or [])
        # 推理模型偶发「合法 JSON 但 scenes 无 shots」，表现为下游 no_pending_plans。
        # 在同一任务内自动重问一次，避免把可恢复的模型抖动直接暴露成流程失败。
        if shot_total == 0:
            _task_step(db, job.id, "parse_repair", "模型漏输出分镜，正在自动补全", 55)
            repair_prompt = user_prompt + (
                "\n\n【补全要求】上一次输出缺少每个 scene 的 shots 数组。"
                "请重新输出完整 JSON，必须为每个 scene 提供至少 1 个 shot，"
                "shot 字段与上述 schema 完全一致，不得省略或留空。"
            )
            try:
                raw = _parse_llm(repair_prompt)
            except LLMError as e:
                _upsert_records(db, job.project_id, episode_start, episode_count, "failed", error_msg=e.message[:2000])
                _fail_job(db, job, e.code, e.message)
                return {"ok": False, "reason": e.code}
            norm = normalize_parse_output(
                raw, episode_start=episode_start, episode_count=episode_count,
                character_name_to_id=char_map,
            )
            shot_total = sum(len(sc.get("shots") or []) for sc in norm.data.get("scenes") or [])

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

        _task_step(db, job.id, "normalize", "正在归一化与校验解析结果", 70)
        # 创建 candidate 解析版本（版本号取项目内最大 +1）
        max_ver = (
            db.query(ParseVersion.version)
            .filter(ParseVersion.project_id == job.project_id)
            .order_by(ParseVersion.version.desc())
            .first()
        )
        next_ver = (max_ver[0] if max_ver else 0) + 1
        # 空镜头解析不可确认：自动补全后仍无 shots，说明模型持续漏输出，
        # 若放行会在分镜图阶段才报 no_pending_plans，故障点后移且难以定位。
        if shot_total == 0:
            msg = "模型连续两次未输出任何镜头，请重试、换用非推理模型或缩小解析集范围"
            # 保留原始响应，便于定位是模型漏输出、字段名不符还是被截断
            job.result = {"raw_response": raw}
            _upsert_records(db, job.project_id, episode_start, episode_count, "failed", error_msg=msg)
            _fail_job(db, job, "parse_no_shots", msg)
            return {"ok": False, "reason": "parse_no_shots"}
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
        _task_step(db, job.id, "saving_result", "正在保存解析结果", 90)

        # 覆盖式自动确认：解析成功后直接落库场景/角色/道具/镜头，
        # 不再依赖前端停留页面触发 confirm 接口。
        from app.models import Scene as SceneModel
        from app.services.script_parse import apply_parse_result
        current_scene_ids = [
            row[0] for row in db.query(SceneModel.id).filter(SceneModel.project_id == job.project_id).all()
        ]
        apply_parse_result(
            db, project, parse_result,
            episode_start=episode_start,
            episode_count=episode_count,
            current_scene_ids=current_scene_ids,
        )
        # 旧版本标记 superseded，新版本设为 current 并更新项目引用
        old_id = project.current_parse_version_id
        if old_id and old_id != pv.id:
            old = db.get(ParseVersion, old_id)
            if old is not None and old.status == "current":
                old.status = "superseded"
        pv.status = "current"
        pv.confirmed_at = _now().replace(tzinfo=None)
        project.current_parse_version_id = pv.id
        # 镜头总数回写项目，供前端判断解析是否完成
        project.shot_total = shot_total

        job.status = "succeeded"
        job.result = {**parse_result, "parse_version_id": pv.id}
        job.finished_at = _now()
        _write_event(db, job, "job.finished", {"status": "succeeded", "result": {"parse_version_id": pv.id, "shots": shot_total}})
        # 回推结果摘要到发起会话：对话内即可看到分析结果并继续下一步
        _push_result_to_session(db, job, pv, norm.data, shot_total)
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
