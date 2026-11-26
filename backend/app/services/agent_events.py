"""Agent 事件发布（Phase B B2）：把 Goal / Plan / Step / tick 的事实写入 workspace_event outbox。

设计约束：
- 只发布事实字段（id、状态、错误码），禁止放 LLM 文本、提示词或进度百分比估算
- 与业务写入同一事务（调用方 commit），本模块只 add + flush，不自行 commit
- 事件类型必须在 workspace_event.EVENT_TYPES 白名单内
- 「状态确实变化才发」：调用方负责比对前后状态，本模块不做推断
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.workspace_event import write_workspace_event

# payload 允许的键（测试断言用：禁止自由文本混入）
GOAL_PAYLOAD_KEYS = {"goal_id", "status", "lifecycle_state", "superseded_by", "goal_type", "scope"}
PLAN_PAYLOAD_KEYS = {"goal_id", "plan_id", "revision", "step_count", "steps"}
STEP_PAYLOAD_KEYS = {
    "goal_id",
    "plan_id",
    "step_id",
    "step_key",
    "tool_name",
    "status",
    "job_id",
    "confirmation_id",
    "error_code",
}
TICK_PAYLOAD_KEYS = {"goal_id", "lifecycle_state", "counts"}


def _goal_ids(goal) -> tuple[str | None, str | None]:
    return getattr(goal, "session_id", None), getattr(goal, "project_id", None)


def publish_goal(db: Session, goal, event_type: str, extra: dict | None = None) -> None:
    """发布 Goal 级事件（created / updated / superseded / finished）。"""
    session_id, project_id = _goal_ids(goal)
    payload = {
        "goal_id": getattr(goal, "goal_id", None) or getattr(goal, "id", None),
        "status": getattr(goal, "status", None),
        "lifecycle_state": getattr(goal, "lifecycle_state", None),
        "goal_type": getattr(goal, "goal_type", None),
    }
    for key, value in (extra or {}).items():
        if key in GOAL_PAYLOAD_KEYS:
            payload[key] = value
    write_workspace_event(
        db,
        event_type,
        session_id=session_id,
        project_id=project_id,
        payload={k: v for k, v in payload.items() if v is not None},
    )


def publish_plan_created(db: Session, goal, plan) -> None:
    """Plan 保存后发布 agent.plan.created（steps 只放 id/key/tool/status）。"""
    session_id, project_id = _goal_ids(goal)
    steps = [
        {
            "step_id": getattr(st, "id", None) or getattr(st, "step_id", None),
            "step_key": getattr(st, "step_key", None),
            "tool_name": getattr(st, "tool_name", None),
            "status": getattr(st, "status", None),
        }
        for st in (getattr(plan, "steps", None) or [])
    ]
    write_workspace_event(
        db,
        "agent.plan.created",
        session_id=session_id,
        project_id=project_id,
        payload={
            "goal_id": getattr(goal, "goal_id", None),
            "plan_id": getattr(plan, "plan_id", None),
            "revision": getattr(plan, "revision", 1),
            "step_count": len(steps),
            "steps": steps,
        },
    )


def publish_step(db: Session, goal, step) -> None:
    """发布单步状态变化（调用方保证状态确实发生变化）。"""
    session_id, project_id = _goal_ids(goal)
    payload = {
        "goal_id": getattr(goal, "goal_id", None),
        "plan_id": getattr(step, "plan_id", None),
        "step_id": getattr(step, "id", None),
        "step_key": getattr(step, "step_key", None),
        "tool_name": getattr(step, "tool_name", None),
        "status": getattr(step, "status", None),
        "job_id": getattr(step, "job_id", None),
        "confirmation_id": getattr(step, "confirmation_id", None),
        "error_code": getattr(step, "error_code", None),
    }
    write_workspace_event(
        db,
        "agent.step.updated",
        session_id=session_id,
        project_id=project_id,
        payload={k: v for k, v in payload.items() if v is not None},
    )


def publish_tick_finished(db: Session, goal, tick_result) -> None:
    """tick 结束发布 agent.tick.finished（只放计数，不放估算进度）。"""
    session_id, project_id = _goal_ids(goal)
    write_workspace_event(
        db,
        "agent.tick.finished",
        session_id=session_id,
        project_id=project_id,
        payload={
            "goal_id": getattr(goal, "goal_id", None),
            "lifecycle_state": getattr(tick_result, "lifecycle_state", None),
            "counts": dict(getattr(tick_result, "counts", None) or {}),
        },
    )