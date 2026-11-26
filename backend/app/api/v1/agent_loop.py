"""Agent Loop SSE 调试端点（默认关闭）。

POST /api/agent/loop/run 在后台线程中执行 Runtime，并通过 SSE 推送进度事件。
Phase A A2 修复：
- 路由函数不再接收请求作用域的 Session；后台执行改用 asyncio.to_thread +
  run_agent_loop_in_new_session，Session 在后台线程内自建自关。
- 回调在主线程取到的 event loop 上用 call_soon_threadsafe 入队，事件不再丢失。
- 每个事件入队时就分配单调递增的 seq，SSE 的 id 直接使用它。
- 后台结束（成功或异常）必定推送终止事件，sse_generator 收到后结束。
- 该接口是调试用途：agent_loop_debug_enabled 关闭时 POST /run 返回 404。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db

logger = logging.getLogger("director-lab.agent-loop")

router = APIRouter(prefix="/api/agent/loop", tags=["agent-loop"])

# 终止事件集合：收到即结束 SSE 流
TERMINAL_EVENTS = frozenset(
    {
        "completed",
        "failed",
        "blocked",
        "waiting",
        "waiting_approval",
        "waiting_job",
        "waiting_user",
        "paused",
        "cancelled",
    }
)


class AgentLoopRunRequest(BaseModel):
    """Agent Loop 执行请求。"""

    session_id: str = Field(
        ..., min_length=1, max_length=32,
        description="会话 ID（ags_ 前缀），同一 session 内的 Goal 会被复用"
    )
    description: str = Field(
        ..., min_length=1, max_length=2000,
        description="用户目标描述文本"
    )
    project_id: str | None = Field(
        default=None, max_length=32,
        description="关联项目 ID（prj_ 前缀），提供后只读工具可真实执行"
    )
    constraints: dict | None = Field(
        default=None,
        description="目标约束（如 max_cost、allowed_tools）"
    )


class AgentLoopEventOut(BaseModel):
    """SSE 事件外发模型（seq 在入队时分配）。"""

    seq: int
    event_type: str
    payload: dict


def _debug_enabled() -> bool:
    return bool(getattr(get_settings(), "agent_loop_debug_enabled", False))


@router.post("/run")
async def run_agent_loop_sse(
    request: AgentLoopRunRequest,
    http_request: Request,
) -> StreamingResponse:
    """启动 Agent Loop 并以 SSE 流式返回进度事件（调试接口）。"""
    if not _debug_enabled():
        raise HTTPException(
            status_code=404,
            detail={"code": "agent_loop_debug_disabled", "message": "调试接口未启用"},
        )

    main_loop = asyncio.get_running_loop()
    queue: asyncio.Queue[AgentLoopEventOut | None] = asyncio.Queue()
    seq = [0]

    def _enqueue(event_type: str, payload: dict | None) -> None:
        seq[0] += 1
        queue.put_nowait(
            AgentLoopEventOut(seq=seq[0], event_type=event_type, payload=payload or {})
        )

    def on_progress(event_type: str, payload: dict) -> None:
        """同步回调：在 Runtime 线程中执行，通过主 loop 安全入队。"""
        try:
            main_loop.call_soon_threadsafe(_enqueue, event_type, payload)
        except RuntimeError:
            logger.debug("event loop closed, dropping event %s", event_type)

    async def sse_generator():
        yield ": start\n\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                if await http_request.is_disconnected():
                    return
                yield ": heartbeat\n\n"
                continue

            if event is None:
                break
            yield f"id: {event.seq}\ndata: {event.model_dump_json()}\n\n"
            if event.event_type in TERMINAL_EVENTS:
                break

        yield ": done\n\n"

    async def _run() -> None:
        from app.services.agent_runtime import run_agent_loop_in_new_session

        try:
            result = await asyncio.to_thread(
                run_agent_loop_in_new_session,
                session_id=request.session_id,
                description=request.description,
                project_id=request.project_id,
                constraints=request.constraints,
                progress_callback=on_progress,
            )
            # Runtime 自己会发终止事件；若状态异常或事件缺失，这里补一个，保证流一定结束
            if result.status not in TERMINAL_EVENTS:
                main_loop.call_soon_threadsafe(
                    _enqueue,
                    "failed",
                    {"status": result.status, "message": result.message},
                )
            else:
                main_loop.call_soon_threadsafe(
                    _enqueue,
                    result.status,
                    {
                        "goal_id": result.goal_id,
                        "plan_id": result.plan_id,
                        "status": result.status,
                        "message": result.message,
                        "completed": result.completed_steps,
                        "failed": result.failed_steps,
                        "total": result.total_steps,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("run_agent_loop failed")
            main_loop.call_soon_threadsafe(_enqueue, "failed", {"error": str(exc)[:500]})
        finally:
            # 无论成功/异常，都保证推一个终止事件，避免客户端挂起
            main_loop.call_soon_threadsafe(queue.put_nowait, None)

    # 持有引用，避免任务被 GC 提前回收
    background_task = asyncio.create_task(_run())
    background_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/status/{goal_id}")
async def get_loop_status(
    goal_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """查询指定 Goal 的执行状态（非流式）。"""
    from app.services.agent_runtime import get_goal_status
    from app.services.goal_manager import get_goal_by_id

    goal = get_goal_by_id(db, goal_id)
    if not goal:
        raise HTTPException(
            status_code=404,
            detail={"code": "goal_not_found", "message": f"Goal {goal_id} 不存在"},
        )
    return get_goal_status(db, goal.session_id)
