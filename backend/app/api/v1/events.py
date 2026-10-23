"""事件流接口：API.md §12 契约。
- GET /api/events：SSE 实时事件流，从 job_event outbox 轮询增量事件
- 事件带全局递增 seq（event_seq 自增），跨项目连续；断线重连以 seq 严格大于判定
- 本期不鉴权；每个事件含 event_type、id、status、updated_at
"""
import asyncio
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.models import Job, JobEvent

router = APIRouter(prefix="/api", tags=["events"])

# SSE 轮询间隔（秒）与单连接最长持续时间（秒，防止连接无限挂起）
_POLL_INTERVAL = 1.0
_MAX_STREAM_SECONDS = 300


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


@router.get("/events")
async def event_stream(last_seq: int = Query(0, ge=0)) -> StreamingResponse:
    """SSE 事件流：从 last_seq 之后开始推送，客户端断线重连时传最后收到的 seq。"""

    async def gen():
        seq = last_seq
        elapsed = 0.0
        # 独立会话，避免长连接占用请求级会话
        while elapsed < _MAX_STREAM_SECONDS:
            db = SessionLocal()
            try:
                rows = (
                    db.query(JobEvent)
                    .filter(JobEvent.event_seq > seq)
                    .order_by(JobEvent.event_seq)
                    .limit(200)
                    .all()
                )
                for ev in rows:
                    job = db.get(Job, ev.job_id)
                    data = _event_payload(ev, job)
                    seq = max(seq, ev.event_seq)
                    yield f"id: {ev.event_seq}\nevent: {ev.event_type}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            finally:
                db.close()
            # 心跳注释，防代理断连
            yield ": ping\n\n"
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/events/backfill")
def events_backfill(updated_since_seq: int = Query(..., ge=0),
                    db: Session = Depends(get_db)) -> dict:
    """断线补齐：返回 seq 严格大于给定值的事件，按 seq 升序（§12 重连约定）。"""
    rows = (
        db.query(JobEvent)
        .filter(JobEvent.event_seq > updated_since_seq)
        .order_by(JobEvent.event_seq)
        .limit(1000)
        .all()
    )
    items = []
    for ev in rows:
        job = db.get(Job, ev.job_id)
        items.append(_event_payload(ev, job))
    return {"total": len(items), "items": items}
