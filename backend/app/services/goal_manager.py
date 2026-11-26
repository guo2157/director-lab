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

import hashlib
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
        fingerprint: str | None = None,
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
        self.fingerprint = fingerprint
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
            "fingerprint": self.fingerprint,
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
        fingerprint=getattr(row, "fingerprint", None),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def compute_fingerprint(
    *,
    project_id: str | None,
    goal_type: str,
    target_stage: str,
    scope: str = "",
) -> str:
    """计算目标指纹：同一「项目 + 意图 + 范围」视为同一目标。

    Phase A 没有结构化目标解析，target_stage 由 planner.classify_description 推导，
    scope 取 constraints["scope"]（没有则为空串）。
    Phase C 由结构化 Goal 解析（LLM 只解析目标，计划仍由代码生成）替换。
    """
    raw = "|".join(
        [
            project_id or "",
            goal_type or "",
            target_stage or "",
            scope or "",
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def derive_target_stage(description: str) -> str:
    """按描述推导 target_stage（与 Planner 共用同一份意图判定）。"""
    from app.services.planner import classify_description

    return classify_description(description)


def _append_history(existing, description: str) -> dict:
    """把最新描述追加到 constraints["history"]，最多保留 10 条。"""
    constraints = dict(existing.constraints or {})
    history = list(constraints.get("history") or [])
    if not history or history[-1] != description:
        history.append(description)
    constraints["history"] = history[-10:]
    return constraints


def get_or_create_goal(
    db: Session,
    *,
    session_id: str,
    description: str,
    project_id: str | None = None,
    goal_type: str = "user_request",
    constraints: dict | None = None,
    success_criteria: list | None = None,
    target_stage: str | None = None,
) -> Goal:
    """获取或创建当前会话的活跃目标（A8 / P0-10：按指纹判定是否同一目标）。

    规则：
    - 同 session 存在 active 目标，且指纹（project_id + goal_type + target_stage + scope）相同
      → 复用：只更新 updated_at；**不覆盖 description**，最新描述追加到 constraints["history"]（最多 10 条）
    - 指纹不同 → 旧目标 status=superseded（终态），新建目标
    - project_id 不同自然导致指纹不同 → 新建目标
    """
    from app.models.agent import AgentGoal

    scope = ""
    if constraints:
        scope = str(constraints.get("scope") or "")
    stage = target_stage or derive_target_stage(description)
    fingerprint = compute_fingerprint(
        project_id=project_id,
        goal_type=goal_type or "user_request",
        target_stage=stage,
        scope=scope,
    )

    existing = db.scalars(
        select(AgentGoal).where(
            and_(AgentGoal.session_id == session_id, AgentGoal.status == "active")
        )
    ).first()

    if existing is not None:
        existing_fp = getattr(existing, "fingerprint", None)
        if existing_fp is None:
            # 历史目标没有指纹：按当前描述推导一次并写入，避免把所有旧目标都判成「不同意图」
            existing_fp = compute_fingerprint(
                project_id=existing.project_id,
                goal_type=existing.goal_type or "user_request",
                target_stage=derive_target_stage(existing.description or ""),
                scope=str((existing.constraints or {}).get("scope") or ""),
            )
            existing.fingerprint = existing_fp

        if existing_fp == fingerprint:
            merged = _append_history(existing, description)
            if constraints:
                for key, value in constraints.items():
                    if key != "history":
                        merged[key] = value
            existing.constraints = merged
            if success_criteria is not None:
                existing.success_criteria = success_criteria
            existing.updated_at = datetime.now()
            db.flush()
            return _row_to_goal(existing)

        # 不同意图：旧目标 superseded（终态，不可再回到 active）
        existing.status = "superseded"
        existing.updated_at = datetime.now()
        db.flush()

    new_constraints = dict(constraints or {})
    new_constraints.setdefault("history", [description])
    g = AgentGoal(
        id=goal_id(),
        session_id=session_id,
        project_id=project_id,
        description=description,
        goal_type=goal_type,
        constraints=new_constraints,
        success_criteria=success_criteria or [],
        status="active",
        fingerprint=fingerprint,
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
