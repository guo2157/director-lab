"""Agent Loop SSE 端点：客户端通过 POST /api/agent/loop/run 发起编排请求，
服务端在独立线程中执行 run_agent_loop，通过 asyncio.Queue 将进度事件推送为 SSE。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db

logger = logging.getLogger("director-lab.agent-loop")

router = APIRouter(prefix="/api/agent/loop", tags=["agent-loop"])


# ── 请求/响应模型 ───────────────────────────────────────────────────────

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
    """SSE 事件外发模型。"""

    event_type: str
    payload: dict


# ── 路由实现 ────────────────────────────────────────────────────────────

@router.post("/run")
async def run_agent_loop_sse(
    request: AgentLoopRunRequest,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """启动 Agent Loop 并以 SSE 流式返回进度事件。

    推送事件类型：
    - goal_created
    - plan_generated
    - step_started
    - step_completed
    - step_failed
    - step_skipped
    - completed / failed / waiting / waiting_approval / waiting_job / blocked

    事件格式（SSE）：
        id: <seq>
        data: {"event_type": "...", "payload": {...}}
    """
    queue: asyncio.Queue[AgentLoopEventOut] = asyncio.Queue()
    seq_counter = [0]  # mutable to increment in callback

    def on_progress(event_type: str, payload: dict) -> None:
        """同步回调：将事件放入 asyncio 队列。

        此回调在 run_agent_loop 的 DB 线程中执行，
        通过 call_soon_threadsafe 安全桥接到事件循环。
        """
        try:
            seq_counter[0] += 1
            loop = asyncio.get_event_loop()
            if payload is None:
                payload = {}
            loop.call_soon_threadsafe(
                queue.put_nowait,
                AgentLoopEventOut(event_type=event_type, payload=payload),
            )
        except RuntimeError:
            # 事件循环已关闭，静默丢弃
            pass

    async def sse_generator():
        """异步生成器：从队列读取事件并序列化为 SSE。"""
        loop_completed = False
        # 输出第一个心跳，让客户端确认连接
        yield ": start\n\n"
        while not loop_completed:
            try:
                event: AgentLoopEventOut = await asyncio.wait_for(
                    queue.get(), timeout=30.0
                )
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue

            seq = seq_counter[0] - queue.qsize()
            data_str = event.model_dump_json()
            yield f"id: {seq}\ndata: {data_str}\n\n"

            if event.event_type in (
                "completed", "failed", "blocked",
                "waiting_approval", "waiting_job",
            ):
                loop_completed = True
                yield ": done\n\n"

        # 等待队列清空（最多再等 5 秒）
        for _ in range(5):
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
                seq = seq_counter[0]
                data_str = event.model_dump_json()
                yield f"id: {seq}\ndata: {data_str}\n\n"
            except asyncio.TimeoutError:
                break

    async def run_in_background():
        """在独立线程中执行 run_agent_loop，完成后关闭 DB session。"""
        try:
            from app.services.agent_runtime import run_agent_loop

            result = run_agent_loop(
                db,
                session_id=request.session_id,
                description=request.description,
                project_id=request.project_id,
                constraints=request.constraints,
                progress_callback=on_progress,
            )
        except Exception as exc:
            logger.exception("run_agent_loop failed")
            seq_counter[0] += 1
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(
                queue.put_nowait,
                AgentLoopEventOut(
                    event_type="failed",
                    payload={"error": str(exc)[:500]},
                ),
            )
        finally:
            pass  # Session 由 FastAPI 依赖注入管理，不在此处关闭

    # 启动后台线程
    import threading
    t = threading.Thread(target=asyncio.run, args=(run_in_background(),), daemon=True)
    t.start()

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
    from app.services.goal_manager import get_active_goal

    goal = get_active_goal(db, goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail={"code": "goal_not_found", "message": f"Goal {goal_id} 不存在"})
    return get_goal_status(db, goal.session_id)
