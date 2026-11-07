"""Parent Agent：任务运行时（意图识别改造 Phase 3 最小核心）。
设计约束（AGENTS.md P0 / 意图识别架构方案 §8/§13）：
- 意图识别不等于任务规划：意图识别回答『用户想做什么』，
  任务规划回答『需要执行哪些步骤』
- 模型只负责识别与解释，任务创建与状态流转由确定性业务代码执行
- 任务状态机：pending → running → completed / failed / cancelled
- 任务由结构化意图识别结果创建（goal/risk_level/confidence 快照落库）
- 任务完成时记录 result_summary；失败时记录错误信息
- 任务与消息关联（message_id），支持审计追溯
- 简单任务（query/analysis/普通对话）不创建任务，避免任务表膨胀
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.agent import AgentMessage, AgentTask
from app.models.ids import agent_task_id
from app.services.intent_resolver import ResolvedIntent

# 需要创建任务运行的意图（执行类意图；query/analysis/ambiguous/unsafe 不建任务）
TASK_INTENTS = {"generate_request", "pipeline_action"}

# 任务状态机合法流转（completed/cancelled 为终态不可变更；
# failed 允许重试回 running，与统一 Task 层 job.retry 的重试能力对齐）
_TASK_TRANSITIONS = {
    "pending": {"running", "completed", "failed", "cancelled"},
    "running": {"completed", "failed", "cancelled"},
    "completed": set(),
    "failed": {"running"},
    "cancelled": set(),
}


def create_task_from_intent(
    db: Session,
    session_id: str,
    message: AgentMessage,
    resolved: ResolvedIntent,
) -> AgentTask | None:
    """从标准化意图创建任务（执行类意图才建任务）。
    - goal：意图目标（LLM 路径）或意图类别（规则路径）
    - success_criteria：目标实体与解析路径快照（机器可读）
    - 非执行类意图返回 None（不建任务）
    """
    if resolved.intent not in TASK_INTENTS:
        return None
    task = AgentTask(
        id=agent_task_id(),
        session_id=session_id,
        message_id=message.id,
        intent=resolved.intent,
        intent_goal=(resolved.source.goal if resolved.source and resolved.source.goal else resolved.intent),
        risk_level=resolved.risk_level,
        confidence=resolved.confidence,
        success_criteria={
            "target": resolved.target,
            "resolution_path": resolved.resolution_path,
        },
        status="pending",
        created_at=datetime.now(),
    )
    db.add(task)
    db.flush()
    return task


def transition_task(db: Session, task: AgentTask, new_status: str, *, result_summary: str | None = None) -> bool:
    """任务状态流转（状态机门禁：非法流转拒绝，终态不可变更）。
    返回是否流转成功。"""
    allowed = _TASK_TRANSITIONS.get(task.status, set())
    if new_status not in allowed:
        return False
    task.status = new_status
    if result_summary is not None:
        task.result_summary = result_summary[:2000]
    db.flush()
    return True


def complete_task(db: Session, task: AgentTask, result_summary: str) -> bool:
    """任务完成：记录结果摘要。"""
    return transition_task(db, task, "completed", result_summary=result_summary)


def fail_task(db: Session, task: AgentTask, error_summary: str) -> bool:
    """任务失败：记录错误摘要（保留任务与日志供审计）。"""
    return transition_task(db, task, "failed", result_summary=error_summary)


def cancel_task(db: Session, task: AgentTask, reason: str | None = None) -> bool:
    """任务取消（用户主动取消或会话归档级联）。"""
    return transition_task(db, task, "cancelled", result_summary=reason)


def retry_task(db: Session, task: AgentTask, note: str | None = None) -> bool:
    """任务重试：failed → running（由 job.retry / job.quality_retry 等确定性事件触发）。
    与统一 Task 层保持两层状态一致；completed/cancelled 终态不可重试。"""
    return transition_task(db, task, "running", result_summary=note)
