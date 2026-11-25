"""生成类操作审批门禁（B 档后续补齐）。

设计约束（AGENTS.md P0）：
- 模型只提出生成请求，真正执行必须经人工审批放行（模型提议、代码裁决）
- 未获批的请求不产生任何副作用：不建任务、不提交工作流、不消耗算力
- 同会话同工具同入参视为同一请求（入参指纹幂等），避免重复审批与重复入队
- 审批与裁决都落 role=approval 的审计消息，可追溯且不进入对话上下文

数据模型约束：
- AgentConfirmation.task_id 非空，故每次审批创建一条 AgentTask 承载意图快照
- 审批记录不存 session_id，统一经 task_id 关联 AgentTask.session_id 查询
"""
import hashlib
import json
import logging
from datetime import datetime

_log = logging.getLogger(__name__)

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AgentConfirmation, AgentMessage, AgentTask
from app.models.ids import agent_confirmation_id, agent_message_id, agent_task_id
from app.services.errors import biz_error


def args_fingerprint(tool_name: str, args: dict | None) -> str:
    """入参指纹：同工具同入参得到同一指纹，用于请求去重（先排序键再哈希）。"""
    payload = json.dumps(
        {"tool": tool_name, "args": args or {}}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _next_seq(db: Session, session_id: str) -> int:
    """会话内下一个消息序号（审批消息与聊天消息共用 seq 序列）。"""
    current = db.scalar(
        select(func.max(AgentMessage.seq)).where(AgentMessage.session_id == session_id)
    )
    return int(current or 0) + 1


def _pending_of_session(
    db: Session, session_id: str, tool_name: str
) -> list[AgentConfirmation]:
    """查询该会话下指定工具的待审批记录。"""
    return list(
        db.scalars(
            select(AgentConfirmation)
            .join(AgentTask, AgentConfirmation.task_id == AgentTask.id)
            .where(
                AgentTask.session_id == session_id,
                AgentConfirmation.tool_name == tool_name,
                AgentConfirmation.status == "pending",
            )
        ).all()
    )


def create_confirmation(
    db: Session,
    *,
    session_id: str,
    tool_name: str,
    args: dict,
    goal: str = "",
    risk_level: str = "high",
) -> AgentConfirmation:
    """创建待审批记录；同会话同工具同入参已有待审记录时直接复用（幂等）。

    只落审批记录，不执行任何生成动作。
    """
    fingerprint = args_fingerprint(tool_name, args)
    for existing in _pending_of_session(db, session_id, tool_name):
        if args_fingerprint(tool_name, existing.args) == fingerprint:
            return existing

    task = AgentTask(
        id=agent_task_id(),
        session_id=session_id,
        intent="generate_request",
        intent_goal=goal or "执行生成类操作（需人工审批）",
        risk_level=risk_level,
        confidence=1.0,
        success_criteria={"tool": tool_name, "fingerprint": fingerprint},
        status="pending",
        created_at=datetime.now(),
    )
    db.add(task)
    confirmation = AgentConfirmation(
        id=agent_confirmation_id(),
        task_id=task.id,
        tool_name=tool_name,
        args=args,
        status="pending",
        created_at=datetime.now(),
    )
    db.add(confirmation)
    db.commit()
    db.refresh(confirmation)
    return confirmation


def find_approved(
    db: Session, *, session_id: str, tool_name: str, fingerprint: str
) -> AgentConfirmation | None:
    """按入参指纹查找已批准的记录（生成工具据此判断能否真正执行）。

    只认 approved 状态，已执行过的记录不再匹配，避免同一审批被重复消费。
    """
    rows = db.scalars(
        select(AgentConfirmation)
        .join(AgentTask, AgentConfirmation.task_id == AgentTask.id)
        .where(
            AgentTask.session_id == session_id,
            AgentConfirmation.tool_name == tool_name,
            AgentConfirmation.status == "approved",
            # P0 状态语义修复（2026-11-25）：已进入终态（completed/cancelled/failed 且无重试）
            # 的任务不再匹配，防止同一审批被重复消费；waiting_approval 属活跃态可正常匹配
            AgentTask.status.notin_(("completed", "cancelled")),
        )
        .order_by(AgentConfirmation.created_at.asc())
    ).all()
    for row in rows:
        if args_fingerprint(tool_name, row.args) == fingerprint:
            return row
    return None


def get_confirmation(db: Session, confirmation_id: str) -> AgentConfirmation:
    confirmation = db.get(AgentConfirmation, confirmation_id)
    if confirmation is None:
        raise biz_error(code="confirmation_not_found", http=404, message="审批记录不存在")
    return confirmation


def list_pending(db: Session, session_id: str) -> list[AgentConfirmation]:
    """列出会话内待处理的审批记录（审批面板用）。

    包含 pending（待裁决）与 approved（已批准待执行）两类：
    批准后记录必须继续可见，否则界面无从触发执行，审批链路会断在半路；
    任务执行完成后（task.status=completed）才从列表移除。
    已拒绝的记录直接消失，避免占位干扰。
    """
    return list(
        db.scalars(
            select(AgentConfirmation)
            .join(AgentTask, AgentConfirmation.task_id == AgentTask.id)
            .where(
                AgentTask.session_id == session_id,
                AgentConfirmation.status.in_(("pending", "approved")),
                AgentTask.status != "completed",
            )
            .order_by(AgentConfirmation.created_at.asc())
        ).all()
    )


def resolve_confirmation(
    db: Session,
    *,
    confirmation_id: str,
    approve: bool,
    comment: str | None = None,
) -> AgentConfirmation:
    """裁决审批：仅允许处理 pending 记录，重复裁决返回 409。

    裁决结果同步写一条 role=approval 的审计消息；批准本身不执行生成，
    执行由调用方在拿到 approved 记录后显式触发（审批与执行分离）。
    """
    confirmation = get_confirmation(db, confirmation_id)
    if confirmation.status != "pending":
        raise biz_error(
            code="confirmation_already_resolved",
            http=409,
            message=f"该审批已处理（{confirmation.status}），不能重复裁决",
        )

    new_status = "approved" if approve else "rejected"
    confirmation.status = new_status
    confirmation.comment = comment

    task = db.get(AgentTask, confirmation.task_id)
    if task is not None:
        from app.services.parent_agent import transition_task as _tt

        new_ts = "running" if approve else "cancelled"
        summary = comment or ("已批准，待执行" if approve else "已拒绝，未执行")
        ok = _tt(db, task, new_ts, result_summary=summary)
        if not ok:
            _log.warning(
                "resolve_confirmation: transition_task(%s→%s) 被门禁拒绝，"
                "当前 task.status=%s 可能已进入终态",
                task.id, new_ts, task.status,
            )
        db.add(
            AgentMessage(
                id=agent_message_id(),
                session_id=task.session_id,
                role="approval",
                content="",
                payload={
                    "confirmation_id": confirmation.id,
                    "tool_name": confirmation.tool_name,
                    "action": new_status,
                    "comment": comment,
                    "args": confirmation.args,
                },
                seq=_next_seq(db, task.session_id),
                created_at=datetime.now(),
            )
        )
    # 统一任务编排层桥接（Phase 5）：审批裁决同步写 task.approval_resolved 事件
    try:
        from app.services.task_orchestrator import bridge_approval_resolved
        bridge_approval_resolved(
            db, confirmation_id=confirmation_id, approve=approve, comment=comment,
        )
    except Exception:
        _log.warning(
            "bridge_approval_resolved 调用失败 (confirmation_id=%s, approve=%s): %s",
            confirmation_id, approve, __import__("traceback").format_exc(),
        )
    # 工作台事件（V2 Phase 3）：审批裁决事实落 outbox，与裁决同事务提交。
    # 旁路事实流：写入失败不阻断审批，仅记录 warning。
    try:
        from app.services.workspace_event import write_workspace_event
        write_workspace_event(
            db,
            "workspace.approval_resolved",
            session_id=getattr(confirmation, "session_id", None),
            payload={
                "confirmation_id": confirmation.id,
                "tool_name": confirmation.tool_name,
                "status": new_status,
                "approved": bool(approve),
                "comment": comment,
            },
        )
    except Exception:
        _log.warning(
            "write workspace.approval_resolved event failed (confirmation_id=%s): %s",
            confirmation_id, __import__("traceback").format_exc(),
        )
    db.commit()
    db.refresh(confirmation)
    return confirmation


def mark_task_finished(db: Session, confirmation_id: str, summary: str) -> None:
    """执行结束后回写任务状态，保留审批链路可追溯。
    P0 状态语义修复（2026-11-25）：不再直接赋值 task.status，统一经 parent_agent
    状态机流转——此前绕过门禁的裸赋值是双重状态写入口径之一。
    """
    confirmation = db.get(AgentConfirmation, confirmation_id)
    if confirmation is None:
        return
    task = db.get(AgentTask, confirmation.task_id)
    if task is None:
        return
    from app.services.parent_agent import transition_task as _tt
    ok = _tt(db, task, "completed", result_summary=summary[:2000])
    if not ok:
        _log.warning(
            "mark_task_finished: transition_task(%s→completed) 被门禁拒绝，当前 task.status=%s",
            task.id, task.status,
        )
    db.commit()
