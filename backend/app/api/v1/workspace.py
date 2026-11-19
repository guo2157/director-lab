"""工作台事件流接口（导演工作台 V2 Phase 3）。

约定与 /api/events、/api/tasks/events 完全一致：
- GET /api/workspace/events：SSE 增量推送，seq 严格大于判定，断线重连传最后收到的 seq
- GET /api/workspace/events/backfill：断线补齐，按 seq 升序返回
- 心跳注释行（: ping）防代理断连；不发送 event: 名称行，前端统一用 onmessage

本期不鉴权（与既有事件接口一致）；事件只来自 workspace_event outbox，
不做任何业务决策，也不生成进度数值。
"""
from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services import workspace_page_context as page_ctx
from app.services.event_stream import sse_event_stream, sse_response
from app.services.workspace_event import (
    fetch_events_after,
    serialize_event,
    write_workspace_event,
)

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


@router.get("/events")
async def workspace_event_stream(
    last_seq: int = Query(0, ge=0),
    session_id: str | None = Query(None),
) -> StreamingResponse:
    """工作台事件流：从 last_seq 之后增量推送。"""
    return sse_response(
        sse_event_stream(
            lambda db, seq: [serialize_event(ev) for ev in
                             fetch_events_after(db, seq, session_id=session_id)],
            last_seq=last_seq,
        )
    )


@router.get("/events/backfill")
def workspace_events_backfill(
    updated_since_seq: int = Query(..., ge=0),
    session_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    """断线补齐：返回 seq 严格大于给定值的事件，按 seq 升序。"""
    rows = fetch_events_after(db, updated_since_seq, session_id=session_id, limit=1000)
    items = [serialize_event(ev) for ev in rows]
    return {"total": len(items), "items": items}


# ---------- 页面上下文（V2 Phase 4）----------


@router.post("/page-context")
def post_page_context(payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    """上报当前页面上下文（前端导航落地后调用）。

    - 请求体与前端 services/workspaceNavigation.ts 的上报体一致，不做强制校验，
      未建模字段进 extra，前端新增字段无需改后端
    - expected_revision / force 为可选并发控制字段（先从请求体取出，不污染业务字段）
    - 写入成功后在同一事务内落 workspace.page_context 事件，供右侧 Agent 面板感知
    - revision 冲突由服务层抛 409，此处不静默改写
    """
    expected_revision = payload.pop("expected_revision", None)
    force = bool(payload.pop("force", False))
    try:
        result = page_ctx.report_context(
            db, payload, expected_revision=expected_revision, force=force
        )
        # 事件与上下文同事务提交：写入失败仅告警，不阻断上报结果
        try:
            snapshot = result.get("context") or {}
            write_workspace_event(
                db,
                "workspace.page_context",
                project_id=snapshot.get("project_id"),
                route_name=snapshot.get("route_name"),
                payload={
                    "page": snapshot.get("page"),
                    "tab": snapshot.get("tab"),
                    "entity_type": snapshot.get("entity_type"),
                    "entity_id": snapshot.get("entity_id"),
                    "focus": snapshot.get("focus"),
                    "nav_ok": snapshot.get("nav_ok"),
                    "nav_result": snapshot.get("nav_result"),
                    "revision": result.get("revision"),
                },
            )
        except Exception:
            import logging

            logging.getLogger("director-lab.workspace_event").warning(
                "write workspace.page_context event failed", exc_info=True
            )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


@router.get("/page-context")
def read_page_context(db: Session = Depends(get_db)) -> dict:
    """读取当前页面上下文快照（不存在时 revision=0、context=null）。"""
    return page_ctx.serialize_context(page_ctx.get_context(db))
