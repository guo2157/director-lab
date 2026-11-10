"""系列续写作业执行器（job_type = series_agent）。

执行阶段（全部为真实执行，进度不伪造）：
1. context_building：Context Builder 确定性构建上下文并落库 Manifest
2. writing：调用 LLM 生成剧情大纲与完整剧本，落库为 awaiting_review 草稿
3. validating：结构化连续性检查，结果写入剧本版本
4. awaiting_review：等待人工审核（任务完成，不自动确认剧本）

纪律：
- 本执行器不修改任何正式剧情状态（story_events / snapshots / state_version）。
- 上下文构建失败直接失败，不使用空上下文继续。
- 失败时同时把统一任务置为 failed，避免任务长期悬挂。
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.integrations.llm import LLMError
from app.models import Job, JobEvent, Series, SeriesEpisode
from app.models.task import Task, TaskStage
from app.services import task_orchestrator
from app.services.series import (
    context_builder_service,
    episode_writing_service,
)

logger = logging.getLogger(__name__)

STAGE_LABELS: dict[str, str] = {
    "context_building": "构建续写上下文",
    "writing": "生成剧情大纲与剧本",
    "validating": "执行连续性检查",
    "awaiting_review": "等待剧本审核",
}


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _write_event(db: Session, job: Job, event_type: str, payload: dict | None = None) -> None:
    ev = JobEvent(job_id=job.id, event_type=event_type, payload=payload or {}, created_at=_now())
    db.add(ev)
    db.flush()
    job.event_seq = ev.event_seq


def _stage(db: Session, task: Task | None, stage_key: str) -> TaskStage | None:
    if task is None:
        return None
    return (
        db.query(TaskStage)
        .filter(TaskStage.task_id == task.id, TaskStage.stage_key == stage_key)
        .first()
    )


def _stage_start(db: Session, task: Task | None, stage_key: str) -> None:
    if task is None:
        return
    stage = _stage(db, task, stage_key)
    if stage is None:
        return
    task_orchestrator.start_stage(db, task, stage)
    task_orchestrator.set_task_step(
        db, task, step=stage_key, message=STAGE_LABELS.get(stage_key, stage_key)
    )


def _stage_progress(db: Session, task: Task | None, stage_key: str, progress: int) -> None:
    if task is None:
        return
    stage = _stage(db, task, stage_key)
    if stage is None:
        return
    task_orchestrator.update_stage_progress(db, task, stage, progress)


def _stage_finish(
    db: Session, task: Task | None, stage_key: str, status: str, result: dict | None = None
) -> None:
    if task is None:
        return
    stage = _stage(db, task, stage_key)
    if stage is None:
        return
    task_orchestrator.finish_stage(db, task, stage, status, result)


def _fail(db: Session, job: Job, task: Task | None, code: str, message: str, stage_key: str) -> None:
    _write_event(db, job, "job.failed", {"error_code": code, "error_msg": message[:2000]})
    job.status = "failed"
    job.error_code = code[:48]
    job.error_msg = message[:2000]
    job.finished_at = _now()
    if task is not None:
        _stage_finish(db, task, stage_key, "failed", {"error_code": code, "error_msg": message[:1000]})
        task_orchestrator.fail_task(db, task, error_code=code, error_msg=message)
    db.commit()


def execute_series_agent_job(job_id: str) -> dict:
    """执行一个系列续写作业。返回执行结果摘要（供 worker 与调度器使用）。"""
    db: Session = SessionLocal()
    current_stage = "context_building"
    try:
        job = db.get(Job, job_id)
        if job is None:
            return {"ok": False, "reason": "job_not_found"}
        if job.status != "queued":
            return {"ok": False, "reason": f"invalid_status:{job.status}"}

        params = dict(job.params or {})
        task_id = params.get("task_id")
        task = db.get(Task, task_id) if task_id else None

        job.status = "running"
        job.started_at = _now()
        job.attempts = (job.attempts or 0) + 1
        _write_event(db, job, "series.started", {"attempt": job.attempts})
        db.commit()

        series = db.get(Series, params.get("series_id") or "")
        episode = db.get(SeriesEpisode, params.get("episode_id") or "")
        if series is None or episode is None:
            _fail(db, job, task, "series_not_found", "系列或剧集不存在", current_stage)
            return {"ok": False, "reason": "series_not_found"}
        if episode.script_status == "confirmed":
            _fail(
                db,
                job,
                task,
                "episode_already_confirmed",
                "该集剧本已确认，不能覆盖既有确认版本",
                current_stage,
            )
            return {"ok": False, "reason": "episode_already_confirmed"}

        user_instruction = str(params.get("user_instruction") or "").strip()
        if not user_instruction:
            _fail(db, job, task, "invalid_request", "创作要求为空", current_stage)
            return {"ok": False, "reason": "invalid_request"}
        constraints = params.get("constraints") or {}

        # ---- 阶段 1：上下文构建 ----
        _stage_start(db, task, "context_building")
        _write_event(db, job, "series.stage", {"stage": "context_building", "status": "running"})
        db.commit()

        episode.script_status = "analyzing"
        db.commit()

        try:
            manifest_row = context_builder_service.build_and_persist_for_episode(
                db,
                series=series,
                episode=episode,
                user_instruction=user_instruction,
                constraints=constraints,
                model=None,
            )
            db.commit()
        except Exception as exc:  # 上下文缺失或构建失败
            db.rollback()
            message = getattr(exc, "detail", None)
            if isinstance(message, dict):
                message = message.get("message")
            _fail(
                db,
                job,
                task,
                "series_context_unavailable",
                str(message or exc)[:1000],
                "context_building",
            )
            return {"ok": False, "reason": "series_context_unavailable"}

        _stage_progress(db, task, "context_building", 100)
        _stage_finish(
            db,
            task,
            "context_building",
            "succeeded",
            {"manifest_id": manifest_row.id, "context_version": manifest_row.context_version},
        )
        _write_event(
            db,
            job,
            "series.context_ready",
            {
                "manifest_id": manifest_row.id,
                "context_version": manifest_row.context_version,
                "context_hash": manifest_row.context_hash,
            },
        )
        db.commit()

        # ---- 阶段 2：生成剧本 ----
        current_stage = "writing"
        _stage_start(db, task, "writing")
        _write_event(db, job, "series.stage", {"stage": "writing", "status": "running"})
        db.commit()

        try:
            version = episode_writing_service.generate_draft(
                db,
                episode=episode,
                manifest=manifest_row.manifest_json or {},
                manifest_id=manifest_row.id,
            )
            db.commit()
        except LLMError as exc:
            db.rollback()
            _fail(db, job, task, exc.code, exc.message, "writing")
            return {"ok": False, "reason": exc.code}
        except Exception as exc:
            db.rollback()
            _fail(db, job, task, "series_writing_failed", str(exc)[:1000], "writing")
            return {"ok": False, "reason": "series_writing_failed"}

        _stage_progress(db, task, "writing", 100)
        _stage_finish(
            db, task, "writing", "succeeded", {"script_version_id": version.id, "version": version.version}
        )
        _write_event(
            db,
            job,
            "series.script_drafted",
            {"script_version_id": version.id, "version": version.version},
        )
        db.commit()

        # ---- 阶段 3：连续性检查 ----
        current_stage = "validating"
        _stage_start(db, task, "validating")
        _write_event(db, job, "series.stage", {"stage": "validating", "status": "running"})
        db.commit()

        try:
            check = episode_writing_service.run_continuity_check(
                db, series=series, episode=episode, script_version=version
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            _fail(db, job, task, "series_validate_failed", str(exc)[:1000], "validating")
            return {"ok": False, "reason": "series_validate_failed"}

        _stage_progress(db, task, "validating", 100)
        _stage_finish(db, task, "validating", "succeeded", {"continuity_status": check["status"]})
        _write_event(
            db,
            job,
            "series.validated",
            {"continuity_status": check["status"], "issue_count": len(check["issues"])},
        )
        db.commit()

        # ---- 阶段 4：等待审核 ----
        current_stage = "awaiting_review"
        _stage_start(db, task, "awaiting_review")
        _stage_progress(db, task, "awaiting_review", 100)
        _stage_finish(
            db, task, "awaiting_review", "succeeded", {"script_version_id": version.id}
        )

        job.status = "succeeded"
        job.finished_at = _now()
        job.result = {
            "episode_id": episode.id,
            "episode_number": episode.episode_number,
            "script_version_id": version.id,
            "script_version": version.version,
            "manifest_id": manifest_row.id,
            "continuity_status": check["status"],
            "issue_count": len(check["issues"]),
        }
        _write_event(db, job, "job.finished", {"summary": "剧本草稿已生成，等待审核"})

        if task is not None:
            task_orchestrator.complete_task(
                db,
                task,
                summary="剧本草稿已生成，等待审核",
                result_data={
                    "result_type": "series_script_draft",
                    "episode_id": episode.id,
                    "episode_number": episode.episode_number,
                    "script_version_id": version.id,
                    "continuity_status": check["status"],
                    "issues": check["issues"][:20],
                },
            )
        db.commit()
        return {"ok": True, "script_version_id": version.id}

    except Exception as exc:  # 兜底：任何未预期异常都要落失败态
        logger.exception("series_agent job %s 执行异常", job_id)
        db.rollback()
        try:
            job = db.get(Job, job_id)
            task = db.get(Task, (job.params or {}).get("task_id")) if job and job.params else None
            if job is not None:
                _fail(db, job, task, "series_internal_error", str(exc)[:1000], current_stage)
        except Exception:  # 落错本身失败时不再抛出，避免阻塞 worker
            db.rollback()
        return {"ok": False, "reason": "series_internal_error"}
    finally:
        db.close()
