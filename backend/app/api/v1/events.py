"""事件流接口：API.md §12 契约。
- GET /api/events：SSE 实时事件流，从 job_event outbox 轮询增量事件
- 事件带全局递增 seq（event_seq 自增），跨项目连续；断线重连以 seq 严格大于判定
- 本期不鉴权；每个事件含 event_type、id、status、updated_at

实现纪律见 app/services/event_stream.py 模块 docstring：
Session 必须在任何 yield 之前关闭，阻塞查询不得跑在事件循环线程上。
"""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Job, JobEvent
from app.services.event_stream import MAX_BATCH, sse_event_stream, sse_response

router = APIRouter(prefix="/api", tags=["events"])


def _event_payload(ev: JobEvent, job: Job | None) -> dict:
    data = {
        "seq": ev.event_seq,
        "event_type": ev.event_type,
        "id": ev.job_id,
        "status": (job.status if job else None),
        "updated_at": ev.created_at.isoformat() if ev.created_at else None,
    }
    if ev.event_type == "job.finished" and job is not None:
        data["result"] = job.result
    elif ev.payload:
        data.update({k: v for k, v in ev.payload.items() if k not in data})
    return data


def _fetch_job_events(db: Session, seq: int, limit: int = MAX_BATCH) -> list[dict]:
    """按 seq 严格大于读取一批 job 事件，物化为纯 dict（Session 由调用方关闭）。"""
    rows = (
        db.query(JobEvent)
        .filter(JobEvent.event_seq > seq)
        .order_by(JobEvent.event_seq)
        .limit(limit)
        .all()
    )
    out = []
    for ev in rows:
        job = db.get(Job, ev.job_id)
        out.append(_event_payload(ev, job))
    return out


@router.get("/events")
async def event_stream(last_seq: int = Query(0, ge=0)) -> StreamingResponse:
    """SSE 事件流：从 last_seq 之后开始推送，客户端断线重连时传最后收到的 seq。"""
    return sse_response(sse_event_stream(_fetch_job_events, last_seq=last_seq))


@router.get("/events/backfill")
def events_backfill(updated_since_seq: int = Query(..., ge=0),
                    db: Session = Depends(get_db)) -> dict:
    """断线补齐：返回 seq 严格大于给定值的事件，按 seq 升序（§12 重连约定）。"""
    items = _fetch_job_events(db, updated_since_seq, limit=1000)
    return {"total": len(items), "items": items}


# ---------- 统一任务事件流（Phase 2）：task_event outbox → SSE ----------


def _task_event_payload(ev, task) -> dict:
    data = {
        "seq": ev.event_seq,
        "event_type": ev.event_type,
        "task_id": ev.task_id,
        "status": (task.status if task else None),
        "progress": (task.progress if task else None),
        "updated_at": ev.created_at.isoformat() if ev.created_at else None,
    }
    if ev.payload:
        data.update({k: v for k, v in ev.payload.items() if k not in data})
    return data


def _fetch_task_events(db: Session, seq: int, limit: int = MAX_BATCH) -> list[dict]:
    from app.models.task import Task, TaskEvent

    rows = (
        db.query(TaskEvent)
        .filter(TaskEvent.event_seq > seq)
        .order_by(TaskEvent.event_seq)
        .limit(limit)
        .all()
    )
    out = []
    for ev in rows:
        task = db.get(Task, ev.task_id)
        out.append(_task_event_payload(ev, task))
    return out


@router.get("/tasks/events")
async def task_event_stream(last_seq: int = Query(0, ge=0)) -> StreamingResponse:
    """统一任务事件流：task_event outbox 增量推送，语义与 /api/events 一致（seq 严格大于判定）。"""
    return sse_response(sse_event_stream(_fetch_task_events, last_seq=last_seq))


@router.get("/tasks/events/backfill")
def task_events_backfill(updated_since_seq: int = Query(..., ge=0),
                          db: Session = Depends(get_db)) -> dict:
    """任务事件断线补齐：seq 严格大于给定值，按 seq 升序。"""
    items = _fetch_task_events(db, updated_since_seq, limit=1000)
    return {"total": len(items), "items": items}
