"""Agent Goal 状态查询接口（Phase B B8）。

状态全部来自数据库（agent_goal / agent_plan / agent_plan_step /
agent_execution_log），不读取聊天消息，因此页面刷新后看到的就是真实执行状态。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.agent import AgentGoal, AgentSession
from app.services.agent_runtime import get_goal_status_by_id
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
