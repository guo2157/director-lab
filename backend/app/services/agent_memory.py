"""Agent 记忆服务：跨会话持久化记忆的写入、检索与上下文注入（记忆系统迭代一）。
设计约束（AGENTS.md P0）：
- 记忆只作上下文补充，不得覆盖用户当前明确指令（注入提示词中显式声明）
- 记忆必须可追溯：source（user/agent）+ source_ref（message_id/task_id）
- 记忆有生命周期：superseded 保留记录不物理删除，superseded_by 指向新记忆
- 注入条数与字符数有上限，防止记忆膨胀挤占对话窗口
- 记忆写入为确定性代码执行，LLM 只负责提炼内容，不直接操作数据库
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentMemory, ids

# 注入上下文的记忆条数与总字符数上限（防膨胀）
MEMORY_INJECT_LIMIT = 20
_MEMORY_INJECT_MAX_CHARS = 3000
# 单条记忆正文上限（写入时截断保护）
_MEMORY_CONTENT_MAX_CHARS = 500

_MEMORY_TYPE_LABELS = {
    "project": "项目记忆",
    "preference": "用户偏好",
    "experience": "经验规则",
}


def add_memory(
    db: Session,
    *,
    content: str,
    memory_type: str = "project",
    project_id: str | None = None,
    source: str = "agent",
    source_ref: dict | None = None,
) -> AgentMemory:
    """写入一条记忆（调用方负责 commit）。
    - content 超长截断保护
    - memory_type 白名单校验，非法值抛 ValueError
    """
    if memory_type not in _MEMORY_TYPE_LABELS:
        raise ValueError(f"非法记忆类型：{memory_type}")
    if not content or not content.strip():
        raise ValueError("记忆内容不能为空")
    mem = AgentMemory(
        id=ids.agent_memory_id(),
        memory_type=memory_type,
        project_id=project_id,
        content=content.strip()[:_MEMORY_CONTENT_MAX_CHARS],
        source=source if source in ("user", "agent") else "agent",
        source_ref=source_ref,
        status="active",
    )
    db.add(mem)
    db.flush()
    return mem


def list_memories(
    db: Session,
    *,
    project_id: str | None = None,
    memory_type: str | None = None,
    include_superseded: bool = False,
    limit: int = 50,
) -> list[AgentMemory]:
    """查询记忆列表（默认仅 active，新→旧）。"""
    q = select(AgentMemory)
    conds = []
    if not include_superseded:
        conds.append(AgentMemory.status == "active")
    if project_id is not None:
        # 项目记忆按项目过滤；全局偏好（project_id 为空）始终包含
        conds.append(
            (AgentMemory.project_id == project_id)
            | (AgentMemory.project_id.is_(None))
        )
    else:
        # P0-1 记忆隔离修复（2026-11-25）：Global 会话（project_id=None）只能读取
        # 全局记忆（project_id IS NULL），不得注入任何项目记忆——此前无此过滤，
        # 全库项目记忆会泄漏进无项目会话（实证见 docs/agent/agent-2.0-current-code-audit.md §7）
        conds.append(AgentMemory.project_id.is_(None))
    if memory_type is not None:
        conds.append(AgentMemory.memory_type == memory_type)
    if conds:
        q = q.where(*conds)
    q = q.order_by(AgentMemory.created_at.desc(), AgentMemory.id.desc()).limit(limit)
    return list(db.scalars(q).unique().all())


def supersede_memory(db: Session, memory_id: str, *, new_content: str | None = None) -> AgentMemory | None:
    """替代一条记忆：旧记忆置 superseded，new_content 提供时写入新记忆并回填 superseded_by。
    返回新记忆（无 new_content 时返回 None）。目标不存在或已 superseded 时抛 ValueError。"""
    old = db.get(AgentMemory, memory_id)
    if old is None:
        raise ValueError(f"记忆不存在：{memory_id}")
    if old.status != "active":
        raise ValueError(f"记忆已非 active，不可替代：{memory_id}")
    old.status = "superseded"
    if new_content and new_content.strip():
        new_mem = add_memory(
            db,
            content=new_content,
            memory_type=old.memory_type,
            project_id=old.project_id,
            source="agent",
            source_ref={"supersedes": memory_id},
        )
        old.superseded_by = new_mem.id
        db.flush()
        return new_mem
    db.flush()
    return None


def build_memory_context(db: Session, project_id: str | None) -> str | None:
    """构建注入对话系统提示词的记忆文本（无可用记忆时返回 None）。
    - 项目记忆按 project_id 过滤；全局偏好（project_id 为空）始终注入
    - 条数与字符数双重上限，超出截断
    - 显式声明：记忆仅供参考，与用户当前指令冲突时以用户指令为准
    """
    memories = list_memories(db, project_id=project_id, limit=MEMORY_INJECT_LIMIT)
    if not memories:
        return None
    lines: list[str] = []
    total = 0
    for m in memories:
        label = _MEMORY_TYPE_LABELS.get(m.memory_type, m.memory_type)
        line = f"- [{label}] {m.content}"
        if total + len(line) > _MEMORY_INJECT_MAX_CHARS:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return None
    return (
        "以下是长期记忆（项目规范/用户偏好/历史经验，仅供参考；"
        "如与用户当前指令冲突，以用户当前指令为准）：\n" + "\n".join(lines)
    )
