"""任务查询接口：API.md §任务 契约（阶段 4 先落地读取部分）。
- GET /api/jobs/{id}：任务详情，含 error_code/error_msg/retryable/result
- GET /api/jobs：项目内任务列表（供分镜页轮询解析任务状态）
retryable 判定：failed/lost 且 attempts < job_max_attempts（§5.7）。
取消/重试/SSE 在后续批次接入。
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models import Job
from app.schemas.script import JobOut
from app.services import errors

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_RETRYABLE_STATUS = ("failed", "lost")


def _to_out(j: Job) -> JobOut:
    settings = get_settings()
    retryable = j.status in _RETRYABLE_STATUS and (j.attempts or 0) < settings.job_max_attempts
    return JobOut(
        id=j.id, project_id=j.project_id, job_type=j.job_type, status=j.status,
        error_code=j.error_code, error_msg=j.error_msg, retryable=retryable,
        result=j.result, attempts=j.attempts or 0,
        created_at=j.created_at, started_at=j.started_at, finished_at=j.finished_at,
    )


@router.get("")
def list_jobs(project_id: str = Query(...), status: str | None = Query(None),
              job_type: str | None = Query(None), limit: int = Query(50, ge=1, le=200),
              db: Session = Depends(get_db)) -> dict:
    q = db.query(Job).filter(Job.project_id == project_id)
    if status:
        q = q.filter(Job.status == status)
    if job_type:
        q = q.filter(Job.job_type == job_type)
    total = q.count()
    items = q.order_by(Job.created_at.desc()).limit(limit).all()
    return {"total": total, "items": [_to_out(j).model_dump(mode="json") for j in items]}


@router.get("/stats")
def job_stats(project_id: str | None = Query(None), db: Session = Depends(get_db)) -> dict:
    """任务统计（API.md §任务）：今日成功/失败、进行中、近 7 日平均耗时。"""
    from datetime import timedelta
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    q = db.query(Job)
    if project_id:
        q = q.filter(Job.project_id == project_id)
    today_succeeded = q.filter(Job.status == "succeeded", Job.finished_at >= today_start).count()
    today_failed = q.filter(Job.status.in_(("failed", "lost")), Job.finished_at >= today_start).count()
    in_progress = q.filter(Job.status.in_(("queued", "running"))).count()
    finished_week = q.filter(Job.status.in_(("succeeded", "failed", "lost")), Job.finished_at >= week_ago).all()
    durations = [
        (j.finished_at - j.started_at).total_seconds()
        for j in finished_week
        if j.finished_at and j.started_at
    ]
    avg_duration = round(sum(durations) / len(durations), 2) if durations else None
    return {
        "today_succeeded": today_succeeded,
        "today_failed": today_failed,
        "in_progress": in_progress,
        "avg_duration_seconds": avg_duration,
    }


@router.get("/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobOut:
    j = db.get(Job, job_id)
    if j is None:
        raise errors.job_not_found()
    return _to_out(j)


_GENERATE_TYPES = {"generate_keyframe", "generate_clip", "generate_audio", "generate_bgm", "generate_sfx"}


@router.post("/{job_id}/cancel", status_code=204)
def cancel_job(job_id: str, db: Session = Depends(get_db)):
    """取消任务：仅 queued/running 可取消。
    running 的生成类任务调用 ComfyUI /interrupt（全局中断，§5.2）；
    其余 running 任务标记取消，worker 在下一检查点确认终态。
    """
    from datetime import datetime, timezone
    from app.models import JobEvent
    j = db.get(Job, job_id)
    if j is None:
        raise errors.job_not_found()
    if j.status not in ("queued", "running"):
        raise errors.invalid_state_transition(f"任务状态 {j.status} 不可取消")
    if j.status == "running" and j.job_type in _GENERATE_TYPES and j.prompt_id:
        try:
            from app.integrations.comfyui import ComfyUIClient
            # 多实例：中断任务实际使用的实例（input_snapshot.comfyui_instance）
            used_url = (j.input_snapshot or {}).get("comfyui_instance")
            ComfyUIClient(base_url=used_url).interrupt() if used_url else ComfyUIClient().interrupt()
        except Exception as e:  # 中断失败不阻断本地取消，记入事件
            db.add(JobEvent(job_id=j.id, event_type="job.interrupt_failed",
                            payload={"reason": type(e).__name__},
                            created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    j.status = "cancelled"
    j.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(JobEvent(job_id=j.id, event_type="job.cancelled", payload={},
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    db.commit()
    return None


@router.post("/{job_id}/retry", status_code=201)
def retry_job(job_id: str, db: Session = Depends(get_db)) -> JobOut:
    """重试任务：仅 failed/lost 且 retryable 可重试，生成新的任务 ID（§任务）。"""
    from datetime import datetime, timezone
    from app.models import ids
    settings = get_settings()
    j = db.get(Job, job_id)
    if j is None:
        raise errors.job_not_found()
    if j.status not in ("failed", "lost"):
        raise errors.invalid_state_transition(f"任务状态 {j.status} 不可重试")
    if (j.attempts or 0) >= settings.job_max_attempts:
        raise errors.invalid_request(f"已达重试上限 {settings.job_max_attempts} 次，不可再重试")
    new_job = Job(
        id=ids.job_id(), project_id=j.project_id, shot_id=j.shot_id,
        job_type=j.job_type, workflow_id=j.workflow_id, workflow_version=j.workflow_version,
        params=j.params, status="queued",
        attempts=j.attempts,  # 继承累计次数，执行器运行时再 +1
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(new_job)
    db.commit()
    return _to_out(new_job)


@router.post("/projects/{project_id}/retry-failed", status_code=201)
def retry_failed_jobs(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """批量重试项目内失败任务（D-14）：按状态筛选 failed/lost 且未达重试上限的任务，
    逐个创建重试任务（复用单任务重试语义：新任务 ID、继承累计次数）；
    可选 job_ids 限定范围。返回重试/跳过明细。"""
    from datetime import datetime, timezone
    from app.models import ids
    settings = get_settings()
    q = db.query(Job).filter(
        Job.project_id == project_id,
        Job.status.in_(["failed", "lost"]),
        Job.attempts < settings.job_max_attempts,
    )
    job_ids = payload.get("job_ids")
    if job_ids:
        q = q.filter(Job.id.in_(job_ids))
    retried, skipped = [], []
    for j in q.all():
        new_job = Job(
            id=ids.job_id(), project_id=j.project_id, shot_id=j.shot_id,
            job_type=j.job_type, workflow_id=j.workflow_id, workflow_version=j.workflow_version,
            params=j.params, status="queued",
            attempts=j.attempts,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(new_job)
        retried.append({"source_job_id": j.id, "new_job_id": new_job.id})
    # 已达上限的失败任务单独列出（前端提示不可重试原因）
    capped = (
        db.query(Job)
        .filter(
            Job.project_id == project_id,
            Job.status.in_(["failed", "lost"]),
            Job.attempts >= settings.job_max_attempts,
        )
        .all()
    )
    skipped = [j.id for j in capped]
    db.commit()
    return {
        "retried": retried,
        "retried_count": len(retried),
        "skipped_capped": skipped,
        "skipped_count": len(skipped),
    }
