"""Goal Manager：Agent 用户目标生命周期管理（Agent Runtime Phase 1）。

设计约束：
- 目标结构化：用户请求 → Goal（goal_id/project_id/session_id/constraints/
  success_criteria/status），不每次消息孤立创建
- 目标追踪：维护 Goal 生命周期，支持 active/paused/completed/failed/cancelled
- 目标复用：同一 session 下的活跃目标复用以避免碎片化
- 目标判定：检查 project/script/asset/review 状态判断 Goal 完成度
- 与 parent_agent.py 的 create_task_from_intent 衔接：Goal 创建后才建 Task
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from app.models.ids import agent_goal_id

# ── 常量 ──────────────────────────────────────────────────────────────

GOAL_STATUSES = {"active", "paused", "completed", "failed", "cancelled", "superseded"}
GOAL_TYPES = {"user_request", "system_continuation", "recovery"}

GOAL_TRANSITIONS: dict[str, set[str]] = {
    "active": {"paused", "completed", "failed", "cancelled", "superseded"},
    "paused": {"active", "completed", "failed", "cancelled"},
    "completed": set(),
    "failed": {"active"},
    "cancelled": set(),
    "superseded": set(),
}


# ── 数据类 ────────────────────────────────────────────────────────────

class Goal:
    """运行时目标对象（轻量容器，对齐 agent_goal 表结构）。"""

    def __init__(
        self,
        goal_id: str,
        session_id: str,
        description: str,
        *,
        project_id: str | None = None,
        goal_type: str = "user_request",
        constraints: dict | None = None,
        success_criteria: list | None = None,
        status: str = "active",
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ):
        self.goal_id = goal_id
        self.session_id = session_id
        self.project_id = project_id
        self.description = description
        self.goal_type = goal_type
        self.constraints = constraints or {}
        self.success_criteria = success_criteria or []
        self.status = status
        self.created_at = created_at or datetime.now()
        self.updated_at = updated_at or datetime.now()

    def to_dict(self) -> dict:
        return {
            "goal_id": self.goal_id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "description": self.description,
            "goal_type": self.goal_type,
            "constraints": self.constraints,
            "success_criteria": self.success_criteria,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ── Goal Manager ───────────────────────────────────────────────────────


def goal_id() -> str:
    """生成 Goal ID。"""
    return agent_goal_id()


def _row_to_goal(row) -> Goal:
    """SQLAlchemy 行对象 → Goal 数据类。"""
    return Goal(
        goal_id=row.id,
        session_id=row.session_id,
        project_id=row.project_id,
        description=row.description,
        goal_type=row.goal_type,
        constraints=row.constraints,
        success_criteria=row.success_criteria,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def get_or_create_goal(
    db: Session,
    *,
    session_id: str,
    description: str,
    project_id: str | None = None,
    goal_type: str = "user_request",
    constraints: dict | None = None,
    success_criteria: list | None = None,
) -> Goal:
    """获取或创建当前会话的活跃目标。

    规则：
    - 同一 session 存在 active 目标时复用（防止碎片化）
    - 否则创建新目标
    - 复用时会更新 description/constraints/success_criteria（覆盖最新意图）
    """
    from app.models.agent import AgentGoal

    # 查找当前会话活跃目标
    existing = db.scalars(
        select(AgentGoal).where(
            and_(AgentGoal.session_id == session_id, AgentGoal.status == "active")
        )
    ).first()

    if existing is not None:
        # 复用：更新描述与约束
        existing.description = description
        if project_id is not None:
            existing.project_id = project_id
        if goal_type:
            existing.goal_type = goal_type
        if constraints is not None:
            existing.constraints = constraints
        if success_criteria is not None:
            existing.success_criteria = success_criteria
        existing.updated_at = datetime.now()
        db.flush()
        return _row_to_goal(existing)

    # 新建
    g = AgentGoal(
        id=goal_id(),
        session_id=session_id,
        project_id=project_id,
        description=description,
        goal_type=goal_type,
        constraints=constraints or {},
        success_criteria=success_criteria or [],
        status="active",
    )
    db.add(g)
    db.flush()
    return _row_to_goal(g)


def get_active_goal(db: Session, session_id: str) -> Goal | None:
    """获取指定会话的活跃目标。"""
    from app.models.agent import AgentGoal

    row = db.scalars(
        select(AgentGoal).where(
            and_(AgentGoal.session_id == session_id, AgentGoal.status == "active")
        )
    ).first()
    return _row_to_goal(row) if row else None


def get_goal_by_id(db: Session, goal_id_str: str) -> Goal | None:
    """按 ID 获取目标。"""
    from app.models.agent import AgentGoal

    row = db.get(AgentGoal, goal_id_str)
    return _row_to_goal(row) if row else None


def transition_goal_status(
    db: Session, goal_id_str: str, new_status: str, *, reason: str | None = None
) -> bool:
    """目标状态流转（状态机门禁）。返回是否流转成功。"""
    from app.models.agent import AgentGoal

    if new_status not in GOAL_STATUSES:
        return False

    row = db.get(AgentGoal, goal_id_str)
    if row is None:
        return False

    allowed = GOAL_TRANSITIONS.get(row.status, set())
    if new_status not in allowed:
        return False

    row.status = new_status
    row.updated_at = datetime.now()
    db.flush()
    return True


def complete_goal(db: Session, goal_id_str: str) -> bool:
    """标记目标为已完成。"""
    return transition_goal_status(db, goal_id_str, "completed")


def fail_goal(db: Session, goal_id_str: str) -> bool:
    """标记目标为失败。"""
    return transition_goal_status(db, goal_id_str, "failed")


def cancel_goal(db: Session, goal_id_str: str) -> bool:
    """取消目标。"""
    return transition_goal_status(db, goal_id_str, "cancelled")


def pause_goal(db: Session, goal_id_str: str) -> bool:
    """暂停目标（等待外部事件恢复）。"""
    return transition_goal_status(db, goal_id_str, "paused")


def resume_goal(db: Session, goal_id_str: str) -> bool:
    """恢复暂停的目标。"""
    return transition_goal_status(db, goal_id_str, "active")


def check_goal_completion(
    db: Session, goal_id_str: str
) -> dict:
    """检查目标完成度（基于关联 Plan/Task 状态）。

    返回 {"is_complete": bool, "progress": str, "blockers": [...]}
    """
    from app.models.agent import AgentGoal, AgentPlan

    row = db.get(AgentGoal, goal_id_str)
    if row is None:
        return {"is_complete": False, "progress": "unknown", "blockers": ["goal_not_found"]}

    # 查询关联 Plan
    plans = db.scalars(
        select(AgentPlan).where(AgentPlan.goal_id == goal_id_str).order_by(AgentPlan.created_at.desc())
    ).all()

    if not plans:
        return {"is_complete": False, "progress": "no_plan", "blockers": []}

    latest_plan = plans[0]
    if latest_plan.status == "completed":
        return {"is_complete": True, "progress": "plan_completed", "blockers": []}
    if latest_plan.status == "failed":
        return {"is_complete": False, "progress": "plan_failed", "blockers": ["plan_execution_failed"]}

    return {
        "is_complete": False,
        "progress": latest_plan.status,
        "blockers": [],
    }


def list_goals_by_project(db: Session, project_id: str) -> list[Goal]:
    """列出项目的所有目标（按时间倒序）。"""
    from app.models.agent import AgentGoal

    rows = db.scalars(
        select(AgentGoal)
        .where(AgentGoal.project_id == project_id)
        .order_by(AgentGoal.created_at.desc())
    ).all()
    return [_row_to_goal(r) for r in rows]
