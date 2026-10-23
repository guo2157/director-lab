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
            ComfyUIClient().interrupt()
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
