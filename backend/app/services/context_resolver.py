"""Context Resolver：Agent Runtime 五层上下文统一解析器（Phase 1）。

职责：
- 整合 intent_resolver.py（意图识别上下文）与 context_manager.py（会话上下文）
- 五层上下文快照：User → Session → Project → Task → Memory
- ContextSnapshot：执行 Plan 前的不可变快照，含 project/script/asset 版本
- 不修改系统状态，只读查询

五层定义（优先级递减）：
1. User Context：AgentSession + permissions（当前用户与授权）
2. Session Context：AgentMessage short-term window + session summary（会话记忆）
3. Project Context：Project/Script/Asset/Shot + derive_progress（实时项目状态）
4. Task Context：AgentTask + Task + TaskStage（当前 Goal 关联）
5. Memory Context：AgentMemory（按 project_id/type 检索的长期记忆）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, and_
from sqlalchemy.orm import Session


# ── 数据类 ────────────────────────────────────────────────────────────

@dataclass
class UserContext:
    """用户级上下文：会话归属与权限。"""

    session_id: str
    project_id: str | None = None
    session_title: str = ""
    session_status: str = "active"
    model: str | None = None


@dataclass
class SessionContextData:
    """会话级上下文：短期消息窗口 + 摘要。"""

    session_id: str
    recent_messages: list[dict] = field(default_factory=list)
    summary: str | None = None
    summary_upto_seq: int | None = None
    focus_entity_type: str | None = None
    focus_entity_id: str | None = None
    focus_entity_name: str | None = None
    last_operation: str | None = None
    last_result: str | None = None


@dataclass
class ProjectContext:
    """项目级上下文：实时 DB 查询的项目/剧本/资产/镜头状态。"""

    project_id: str | None = None
    project_name: str | None = None
    pipeline_stage: str | None = None
    script_id: str | None = None
    script_progress: float = 0.0
    character_count: int = 0
    scene_count: int = 0
    prop_count: int = 0
    shot_count: int = 0
    asset_counts: dict = field(default_factory=dict)
    pending_reviews: list[dict] = field(default_factory=list)
    active_jobs: list[str] = field(default_factory=list)


@dataclass
class TaskContext:
    """任务级上下文：当前 Goal 关联的执行状态。"""

    goal_id: str | None = None
    plan_id: str | None = None
    current_step_id: str | None = None
    lifecycle_state: str | None = None
    agent_task_id: str | None = None
    agent_task_status: str | None = None
    task_stages: list[dict] = field(default_factory=list)


@dataclass
class MemoryContext:
    """记忆上下文：项目级长期记忆。"""

    items: list[dict] = field(default_factory=list)


@dataclass
class ContextSnapshot:
    """执行 Plan 前的不可变上下文快照。

    包含五层上下文及快照时间戳，作为 Planner 的确定性输入。
    """

    snapshot_at: datetime = field(default_factory=datetime.now)
    user: UserContext = field(default_factory=UserContext)
    session: SessionContextData = field(default_factory=SessionContextData)
    project: ProjectContext = field(default_factory=ProjectContext)
    task: TaskContext = field(default_factory=TaskContext)
    memory: MemoryContext = field(default_factory=MemoryContext)
    
    def to_dict(self) -> dict:
        return {
            "snapshot_at": self.snapshot_at.isoformat(),
            "user": {
                "session_id": self.user.session_id,
                "project_id": self.user.project_id,
                "session_title": self.user.session_title,
            },
            "session": {
                "recent_messages_count": len(self.session.recent_messages),
                "has_summary": self.session.summary is not None,
                "focus": {
                    "entity_type": self.session.focus_entity_type,
                    "entity_id": self.session.focus_entity_id,
                    "entity_name": self.session.focus_entity_name,
                },
                "last_operation": self.session.last_operation,
            },
            "project": {
                "project_id": self.project.project_id,
                "project_name": self.project.project_name,
                "pipeline_stage": self.project.pipeline_stage,
                "character_count": self.project.character_count,
                "scene_count": self.project.scene_count,
                "shot_count": self.project.shot_count,
            },
            "task": {
                "goal_id": self.task.goal_id,
                "plan_id": self.task.plan_id,
                "lifecycle_state": self.task.lifecycle_state,
            },
        }


# ── Context Resolver ───────────────────────────────────────────────────


def _safe_dict(row, columns: list[str]) -> dict:
    """安全地将 SQLAlchemy 行转换为 dict，处理 JSON/日期序列化。"""
    result: dict[str, Any] = {}
    for col in columns:
        val = getattr(row, col, None)
        if isinstance(val, datetime):
            val = val.isoformat()
        result[col] = val
    return result


def resolve_user_context(db: Session, session_id: str) -> UserContext:
    """解析用户级上下文。"""
    from app.models.agent import AgentSession

    session = db.get(AgentSession, session_id)
    if session is None:
        return UserContext(session_id=session_id)

    return UserContext(
        session_id=session.id,
        project_id=session.project_id,
        session_title=session.title or "",
        session_status=session.status,
        model=session.model,
    )


def resolve_session_context(
    db: Session,
    session_id: str,
    *,
    recent_limit: int = 20,
) -> SessionContextData:
    """解析会话级上下文：最近消息 + 摘要 + 焦点实体。"""
    from app.models.agent import AgentSession, AgentMessage

    session = db.get(AgentSession, session_id)
    if session is None:
        return SessionContextData(session_id=session_id)

    # 最近消息
    recent_msgs = db.scalars(
        select(AgentMessage)
        .where(AgentMessage.session_id == session_id)
        .order_by(AgentMessage.seq.desc())
        .limit(recent_limit)
    ).all()
    recent_list = [
        {"seq": m.seq, "role": m.role, "content": (m.content or "")[:500]}
        for m in reversed(recent_msgs)
    ]

    # 焦点实体（从 context_state JSON 提取）
    ctx_state = session.context_state or {}
    focus = ctx_state.get("focus", {})

    return SessionContextData(
        session_id=session_id,
        recent_messages=recent_list,
        summary=session.summary,
        summary_upto_seq=session.summary_upto_seq,
        focus_entity_type=focus.get("entity_type"),
        focus_entity_id=focus.get("entity_id"),
        focus_entity_name=focus.get("entity_name"),
        last_operation=(ctx_state.get("operation", {}) or {}).get("operation"),
        last_result=(ctx_state.get("operation", {}) or {}).get("last_result"),
    )


def resolve_project_context(db: Session, project_id: str | None) -> ProjectContext:
    """解析项目级上下文：实时 DB 查询项目/剧本/资产状态。"""
    if not project_id:
        return ProjectContext()

    ctx = ProjectContext(project_id=project_id)

    try:
        from app.models.production import Project
        from app.models.narrative import Script

        project = db.get(Project, project_id)
        if project is not None:
            ctx.project_name = getattr(project, "name", None)
            ctx.pipeline_stage = getattr(project, "pipeline_stage", None)

        # 剧本进度
        scripts = db.execute(
            select(Script).where(Script.project_id == project_id).limit(1)
        ).scalars().all()
        if scripts:
            ctx.script_id = scripts[0].id if hasattr(scripts[0], "id") else None

        # 实体计数（使用 raw SQL 避免全量加载）
        from sqlalchemy import text

        counts = db.execute(
            text(
                "SELECT "
                "(SELECT COUNT(*) FROM `character` WHERE project_id = :pid) as cc, "
                "(SELECT COUNT(*) FROM scene WHERE project_id = :pid) as sc, "
                "(SELECT COUNT(*) FROM prop WHERE project_id = :pid) as pc, "
                "(SELECT COUNT(*) FROM shot WHERE project_id = :pid) as shc"
            ),
            {"pid": project_id},
        ).first()
        if counts:
            ctx.character_count = int(counts[0] or 0)
            ctx.scene_count = int(counts[1] or 0)
            ctx.prop_count = int(counts[2] or 0)
            ctx.shot_count = int(counts[3] or 0)
            ctx.asset_counts = {
                "character": ctx.character_count,
                "scene": ctx.scene_count,
                "prop": ctx.prop_count,
                "shot": ctx.shot_count,
            }

    except Exception:
        # 项目查询失败不阻塞主流程
        pass

    return ctx


def resolve_task_context(
    db: Session,
    *,
    goal_id: str | None = None,
    plan_id: str | None = None,
    agent_task_id: str | None = None,
) -> TaskContext:
    """解析任务级上下文：当前 Goal/Plan/AgentTask 关联的执行状态。"""
    ctx = TaskContext(
        goal_id=goal_id,
        plan_id=plan_id,
        agent_task_id=agent_task_id,
    )

    if agent_task_id:
        try:
            from app.models.agent import AgentTask

            task = db.get(AgentTask, agent_task_id)
            if task is not None:
                ctx.goal_id = ctx.goal_id or getattr(task, "goal_id", None)
                ctx.plan_id = ctx.plan_id or getattr(task, "plan_id", None)
                ctx.current_step_id = getattr(task, "current_step_id", None)
                ctx.lifecycle_state = getattr(task, "lifecycle_state", None)
                ctx.agent_task_status = task.status
        except Exception:
            pass

    return ctx


def resolve_memory_context(
    db: Session,
    *,
    project_id: str | None = None,
    session_id: str | None = None,
    limit: int = 5,
) -> MemoryContext:
    """解析记忆上下文：按项目/类型检索长期记忆。"""
    items: list[dict] = []

    try:
        from app.models.agent import AgentMemory

        conditions = []
        if project_id:
            conditions.append(AgentMemory.project_id == project_id)
        if session_id:
            conditions.append(AgentMemory.session_id == session_id)

        if conditions:
            rows = db.scalars(
                select(AgentMemory)
                .where(and_(*conditions))
                .order_by(AgentMemory.created_at.desc())
                .limit(limit)
            ).all()
            items = [
                {
                    "id": r.id,
                    "memory_type": r.memory_type,
                    "content": (r.content or "")[:300],
                    "source": r.source,
                }
                for r in rows
            ]

    except Exception:
        pass

    return MemoryContext(items=items)


def snapshot(
    db: Session,
    *,
    session_id: str,
    project_id: str | None = None,
    goal_id: str | None = None,
    plan_id: str | None = None,
    agent_task_id: str | None = None,
) -> ContextSnapshot:
    """构建完整上下文快照（五层统一采集）。

    这是 Planner/build_plan 执行前的唯一上下文入口。
    """
    # 1. User Context
    user_ctx = resolve_user_context(db, session_id)
    effective_project = project_id or user_ctx.project_id

    # 2. Session Context
    session_ctx = resolve_session_context(db, session_id)

    # 3. Project Context
    project_ctx = resolve_project_context(db, effective_project)

    # 4. Task Context
    task_ctx = resolve_task_context(
        db,
        goal_id=goal_id,
        plan_id=plan_id,
        agent_task_id=agent_task_id,
    )

    # 5. Memory Context
    memory_ctx = resolve_memory_context(
        db,
        project_id=effective_project,
        session_id=session_id,
    )

    return ContextSnapshot(
        snapshot_at=datetime.now(),
        user=user_ctx,
        session=session_ctx,
        project=project_ctx,
        task=task_ctx,
        memory=memory_ctx,
    )


def build_context_prompt(snapshot: ContextSnapshot) -> str:
    """将上下文快照格式化为 LLM 可用的结构化提示文本。

    供 Planner.generate() 注入 System Prompt。
    """
    parts: list[str] = []

    # 项目信息
    p = snapshot.project
    if p.project_name:
        parts.append(f"## 当前项目\n- 名称：{p.project_name}\n- ID：{p.project_id}")
        parts.append(
            f"- 进度：角色 {p.character_count} / 场景 {p.scene_count} / 道具 {p.prop_count} / 镜头 {p.shot_count}"
        )

    # 任务状态
    t = snapshot.task
    if t.goal_id:
        parts.append(f"## 当前任务\n- Goal ID：{t.goal_id}")
        if t.lifecycle_state:
            parts.append(f"- 生命周期：{t.lifecycle_state}")
        if t.agent_task_status:
            parts.append(f"- 状态：{t.agent_task_status}")

    # 焦点实体
    s = snapshot.session
    if s.focus_entity_type:
        parts.append(
            f"## 当前焦点\n- 类型：{s.focus_entity_type}\n- 名称：{s.focus_entity_name or '未命名'}"
        )

    # 记忆
    if snapshot.memory.items:
        mem_lines = ["## 相关记忆"]
        for m in snapshot.memory.items[:3]:
            mem_lines.append(f"- [{m['memory_type']}] {m['content'][:200]}")
        parts.append("\n".join(mem_lines))

    return "\n\n".join(parts)
