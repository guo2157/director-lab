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
