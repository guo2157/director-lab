"""Context Manager：导演台 Agent 结构化上下文管理（意图识别改造 Phase 2）。
设计约束（AGENTS.md P0 / 意图识别架构方案 §4）：
- 上下文分类：Conversation（消息窗口）/ Project（项目状态）/ Task（任务状态）/
  Focus（当前关注实体）/ Operation（最近操作）/ Memory（会话摘要长期记忆）
- Focus Context 必须保存实体 ID，不能只保存名称
- 项目切换时清空 focus/operation，防止跨项目上下文污染
- 焦点实体使用前必须经数据库验证（存在 + 属于当前项目），失效即弃用
- 上下文优先级：数据库实时状态 > 任务状态 > 操作焦点 > 最近对话 > 历史摘要 > 长期记忆
- 上下文只作为 LLM 语义判定的辅助输入与消歧回退，不作为执行依据；
  执行仍以确定性代码 + 数据库实时状态为准
持久化：AgentSession.context_state（JSON）：
- focus: {entity_type, entity_id, entity_name, updated_seq}
- operation: {operation, entity_type, entity_id, entity_name, last_result, last_error, updated_seq}
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import Character, Prop, Scene, Shot
from app.models.agent import AgentSession

# 焦点实体类型 → ORM 模型（与 generate_intent._ENTITY_MODELS 对齐）
_FOCUS_MODELS = {"character": Character, "scene": Scene, "prop": Prop}

# 焦点有效期：超过该消息条数未更新则不再作为消歧回退（防止陈旧焦点误导）
FOCUS_MAX_AGE_SEQ = 40


@dataclass
class FocusContext:
    """当前关注实体（保存 ID，不只保存名称）。"""

    entity_type: str  # character / scene / prop / shot
    entity_id: str
    entity_name: str
    updated_seq: int = 0


@dataclass
class OperationContext:
    """最近一次操作记录。"""

    operation: str  # 如 modify_prompt / regenerate_asset / generate_keyframe
    entity_type: str | None = None
    entity_id: str | None = None
    entity_name: str | None = None
    last_result: str | None = None  # success / failed
    last_error: str | None = None
    updated_seq: int = 0


@dataclass
class SessionContext:
    """会话结构化上下文快照（Context Manager 对外统一视图）。"""

    focus: FocusContext | None = None
    operation: OperationContext | None = None


# --- 读写（AgentSession.context_state JSON 字段） ---


def load_context(session: AgentSession) -> SessionContext:
    """从会话加载结构化上下文（字段缺失/损坏时安全降级为空上下文）。"""
    state = session.context_state if isinstance(session.context_state, dict) else {}
    focus = None
    f = state.get("focus")
    if isinstance(f, dict) and f.get("entity_id") and f.get("entity_type"):
        focus = FocusContext(
            entity_type=str(f["entity_type"]),
            entity_id=str(f["entity_id"]),
            entity_name=str(f.get("entity_name") or ""),
            updated_seq=int(f.get("updated_seq") or 0),
        )
    operation = None
    o = state.get("operation")
    if isinstance(o, dict) and o.get("operation"):
        operation = OperationContext(
            operation=str(o["operation"]),
            entity_type=o.get("entity_type"),
            entity_id=o.get("entity_id"),
            entity_name=o.get("entity_name"),
            last_result=o.get("last_result"),
            last_error=o.get("last_error"),
            updated_seq=int(o.get("updated_seq") or 0),
        )
    return SessionContext(focus=focus, operation=operation)


def _save_context(session: AgentSession, ctx: SessionContext) -> None:
    """写回会话 context_state（保留未知字段，防御并发覆盖由调用方事务保证）。"""
    state = session.context_state if isinstance(session.context_state, dict) else {}
    state = dict(state)  # 不原地修改，避免 SQLAlchemy JSON 字段未变更检测失效
    if ctx.focus is not None:
        state["focus"] = {
            "entity_type": ctx.focus.entity_type,
            "entity_id": ctx.focus.entity_id,
            "entity_name": ctx.focus.entity_name,
            "updated_seq": ctx.focus.updated_seq,
        }
    else:
        state.pop("focus", None)
    if ctx.operation is not None:
        state["operation"] = {
            "operation": ctx.operation.operation,
            "entity_type": ctx.operation.entity_type,
            "entity_id": ctx.operation.entity_id,
            "entity_name": ctx.operation.entity_name,
            "last_result": ctx.operation.last_result,
            "last_error": ctx.operation.last_error,
            "updated_seq": ctx.operation.updated_seq,
        }
    else:
        state.pop("operation", None)
    session.context_state = state


def update_focus(
    session: AgentSession,
    *,
    entity_type: str,
    entity_id: str,
    entity_name: str,
    updated_seq: int = 0,
) -> None:
    """更新当前关注实体（实体唯一定位成功后调用）。"""
    ctx = load_context(session)
    ctx.focus = FocusContext(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        updated_seq=updated_seq,
    )
    _save_context(session, ctx)


def update_operation(
    session: AgentSession,
    *,
    operation: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    entity_name: str | None = None,
    last_result: str | None = None,
    last_error: str | None = None,
    updated_seq: int = 0,
) -> None:
    """记录最近一次操作（操作发起/完成时调用）。"""
    ctx = load_context(session)
    ctx.operation = OperationContext(
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        last_result=last_result,
        last_error=last_error,
        updated_seq=updated_seq,
    )
    _save_context(session, ctx)


def clear_on_project_switch(session: AgentSession) -> bool:
    """项目切换时清空 focus/operation（防止跨项目上下文污染）。
    返回是否发生了清理（调用方可据此落审计消息）。"""
    ctx = load_context(session)
    if ctx.focus is None and ctx.operation is None:
        return False
    _save_context(session, SessionContext(focus=None, operation=None))
    return True


# --- 焦点验证（数据库实时状态优先，旧焦点不得覆盖数据库） ---


def validate_focus(
    db: Session, session: AgentSession, *, current_seq: int = 0
) -> FocusContext | None:
    """验证焦点实体仍然有效：存在 + 属于当前会话项目 + 未过期。
    任一条件不满足返回 None（调用方不得使用陈旧焦点执行操作）。"""
    ctx = load_context(session)
    if ctx.focus is None:
        return None
    focus = ctx.focus
    # 过期判断：焦点更新时间距当前消息过远则不再作为消歧回退
    if current_seq and focus.updated_seq and current_seq - focus.updated_seq > FOCUS_MAX_AGE_SEQ:
        return None
    # shot 类型：镜头经 Scene 归属项目
    if focus.entity_type == "shot":
        shot = db.get(Shot, focus.entity_id)
        if shot is None:
            return None
        scene = db.get(Scene, shot.scene_id)
        if scene is None:
            return None
        if session.project_id and scene.project_id != session.project_id:
            return None
        return focus
    model = _FOCUS_MODELS.get(focus.entity_type)
    if model is None:
        return None
    entity = db.get(model, focus.entity_id)
    if entity is None:
        return None
    if session.project_id and entity.project_id != session.project_id:
        return None
    return focus


# --- LLM 意图识别注入 ---


def build_focus_hint(focus: FocusContext | None, operation: OperationContext | None) -> str | None:
    """构建注入 LLM 意图识别的结构化上下文提示（无有效上下文时返回 None）。
    仅作为语义判定辅助：『重新生成』『继续修改』等指代消解的回退依据。"""
    parts: list[str] = []
    if focus is not None:
        type_label = {"character": "角色", "scene": "场景", "prop": "道具", "shot": "镜头"}.get(
            focus.entity_type, focus.entity_type
        )
        name = f"『{focus.entity_name}』" if focus.entity_name else ""
        parts.append(
            f"当前关注对象：{type_label} {name}（ID: {focus.entity_id}）。"
            "用户说『重新生成』『继续修改』『它』等指代时，默认指该对象。"
        )
    if operation is not None:
        op = operation.operation
        if operation.entity_name:
            op += f"（对象：{operation.entity_name}）"
        if operation.last_result:
            op += f"，结果：{operation.last_result}"
        parts.append(f"最近一次操作：{op}。")
    if not parts:
        return None
    return (
        "以下是本会话的结构化上下文（供理解用户指代使用，不代表数据库实时状态）：\n"
        + "\n".join(parts)
    )
