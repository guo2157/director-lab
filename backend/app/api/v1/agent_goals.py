"""Agent Goal 状态查询接口（Phase B B8）。

状态全部来自数据库（agent_goal / agent_plan / agent_plan_step /
agent_execution_log），不读取聊天消息，因此页面刷新后看到的就是真实执行状态。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.agent import AgentGoal, AgentSession
from app.services.agent_runtime import (
    cancel_goal_run,
    get_goal_status_by_id,
    pause_goal,
    resume_goal_control,
)
from app.services.errors import biz_error

router = APIRouter(prefix="/api/agent", tags=["agent-goals"])


def _session_goal_query(session_id: str):
    return select(AgentGoal).where(AgentGoal.session_id == session_id)


@router.get("/{session_id}/goal")
def get_current_goal(session_id: str, db: Session = Depends(get_db)) -> dict:
    """当前 active Goal 的完整状态；没有 active Goal 时返回 {"goal": null}。"""
    session = db.get(AgentSession, session_id)
    if session is None:
        raise biz_error(code="agent_session_not_found", http=404, message="会话不存在")

    goal = db.scalars(
        _session_goal_query(session_id)
        .where(AgentGoal.status == "active")
        .order_by(AgentGoal.updated_at.desc())
        .limit(1)
    ).first()
    if goal is None:
        return {"goal": None}
    return get_goal_status_by_id(db, goal.id)


@router.get("/{session_id}/goals")
def list_session_goals(
    session_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    """该会话的历史 Goal 列表（按 updated_at 倒序分页）。"""
    session = db.get(AgentSession, session_id)
    if session is None:
        raise biz_error(code="agent_session_not_found", http=404, message="会话不存在")

    q = _session_goal_query(session_id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(
        q.order_by(AgentGoal.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [
            {
                "goal_id": row.id,
                "description": row.description,
                "goal_type": row.goal_type,
                "status": row.status,
                "lifecycle_state": row.lifecycle_state,
                "project_id": row.project_id,
                "fingerprint": row.fingerprint,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


@router.get("/goals/{goal_id}")
def get_goal_by_goal_id(goal_id: str, db: Session = Depends(get_db)) -> dict:
    """按 goal_id 查询完整状态（与 /{session_id}/goal 同一份载荷）。"""
    status = get_goal_status_by_id(db, goal_id)
    if not status.get("goal"):
        raise biz_error(code="agent_goal_not_found", http=404, message="目标不存在")
    return status


class CancelGoalRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


def _control_response(db: Session, goal_id: str, outcome: dict) -> dict:
    if not outcome.get("ok"):
        if outcome.get("error") == "goal_not_found":
            raise biz_error(code="agent_goal_not_found", http=404, message="目标不存在")
        raise biz_error(
            code="agent_goal_terminal",
            http=409,
            message=f"目标已结束（{outcome.get('status')}），不能再操作",
        )
    db.commit()
    return get_goal_status_by_id(db, goal_id)


@router.post("/goals/{goal_id}/pause")
def pause_goal_endpoint(goal_id: str, db: Session = Depends(get_db)) -> dict:
    """暂停目标：不再推进新步骤；已在跑的 Job 不取消。"""
    return _control_response(db, goal_id, pause_goal(db, goal_id))


@router.post("/goals/{goal_id}/resume")
def resume_goal_endpoint(goal_id: str, db: Session = Depends(get_db)) -> dict:
    """恢复目标并立即同步推进一次（受 MAX_STEPS 与 60 秒上限约束）。"""
    from app.services.agent_runtime import tick

    outcome = resume_goal_control(db, goal_id)
    _control_response(db, goal_id, outcome)
    tick(None, goal_id, trigger="manual", db=db)
    db.commit()
    return get_goal_status_by_id(db, goal_id)


@router.post("/goals/{goal_id}/cancel")
def cancel_goal_endpoint(
    goal_id: str,
    payload: CancelGoalRequest | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """取消目标：在途 Job 与 pending 审批一并取消，执行记录保留。"""
    reason = payload.reason if payload is not None else None
    return _control_response(db, goal_id, cancel_goal_run(db, goal_id, reason=reason))
