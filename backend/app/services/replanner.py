"""Replanner：Agent Runtime 重规划与错误恢复（Phase 2.4）。

职责：
- 错误分类：Tool 失败 / Job 失败 / 审核拒绝 / 依赖变化 / 预算超限
- 恢复策略：Retry / Replan / Wait / Fail
- Plan Revision 递增 + 保留历史
- 限制：最大 Retry=3 / Step，最大 Replan=3 / Goal
- 触发条件：Verifier.verify_step 失败 → Replanner.replan

恢复策略定义：
1. Retry：瞬态错误（网络超时 / 临时不可用），同 Step 重试
2. Replan：永久错误（工具不存在 / 参数错误 / 业务门禁拒绝），修订 Plan
3. Wait：异步 Job 未完成 / 等待审批，暂停等待外部事件
4. Fail：超过最大重试/重规划次数，标记失败
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.services.planner import (
    Plan,
    build_and_save_plan,
    get_latest_plan,
    get_next_pending_steps,
)
from app.services.verifier import StepVerificationResult


# ── 常量 ──────────────────────────────────────────────────────────────

MAX_RETRY_PER_STEP = 3
MAX_REPLAN_PER_GOAL = 3

# 可重试的错误码（瞬态错误）
RETRYABLE_ERRORS: set[str] = {
    "TimeoutError",
    "ConnectionError",
    "OperationalError",
    "ConnectionResetError",
    "BrokenPipeError",
    "ResourceBusy",
    "TemporaryUnavailable",
}

# 需要等待的错误（异步未完成）
WAITABLE_ERRORS: set[str] = {
    "JobStillRunning",
    "AwaitingApproval",
    "WaitingUserInput",
    "JobQueued",
}


# ── 枚举 ──────────────────────────────────────────────────────────────

class ReplanAction(Enum):
    """重规划动作。"""

    RETRY = "retry"           # 重试当前步骤
    REPLAN = "replan"         # 修订计划
    WAIT = "wait"             # 等待外部事件
    FAIL = "fail"             # 标记失败
    SKIP = "skip"             # 跳过当前步骤
    CANCEL = "cancel"         # 取消整个目标


# ── 数据类 ────────────────────────────────────────────────────────────

@dataclass
class ErrorContext:
    """错误上下文：用于分类与决策。"""

    step_id: str
    step_key: str = ""
    tool_name: str = ""
    error_code: str | None = None
    error_message: str | None = None
    retry_count: int = 0
    max_retries: int = MAX_RETRY_PER_STEP
    step_status: str = "failed"
    verification: StepVerificationResult | None = None


@dataclass
class ReplanDecision:
    """重规划决策。"""

    action: ReplanAction = ReplanAction.FAIL
    reason: str = ""
    suggestion: str = ""
    updated_plan: Plan | None = None
    next_step_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "suggestion": self.suggestion,
            "has_updated_plan": self.updated_plan is not None,
            "next_step_ids": self.next_step_ids,
        }


# ── 错误分类 ──────────────────────────────────────────────────────────

def classify_error(ctx: ErrorContext) -> ReplanAction:
    """错误分类：根据 error_code / error_message / context 判定恢复策略。

    优先级：
    1. 预算超限 / 安全拒绝 → FAIL
    2. 工具不存在 / 参数错误 → REPLAN
    3. 瞬态错误（网络/超时）且未超重试上限 → RETRY
    4. 异步未完成 → WAIT
    5. 审核拒绝 → REPLAN（更新约束）
    6. 其他 → FAIL
    """
    code = ctx.error_code or ""
    msg = (ctx.error_message or "").lower()

    # 1. 预算/安全拒绝 → FAIL
    if any(kw in msg for kw in ["budget", "exceed", "policy", "denied", "blocked"]):
        return ReplanAction.FAIL
    if code in ("BudgetExceeded", "PolicyDenied", "SafetyBlocked"):
        return ReplanAction.FAIL

    # 2. 工具不存在/参数错误 → REPLAN
    if any(kw in msg for kw in ["not found in registry", "tool_not_found", "invalid"]):
        return ReplanAction.REPLAN
    if code in ("ToolNotFound", "InvalidArgs", "ValidationError", "TypeError", "ValueError"):
        return ReplanAction.REPLAN

    # 3. 瞬态错误 → RETRY（如未超上限）
    if code in RETRYABLE_ERRORS or any(
        kw in msg for kw in ["timeout", "connection", "temporary", "unavailable", "retry"]
    ):
        if ctx.retry_count < ctx.max_retries:
            return ReplanAction.RETRY
        return ReplanAction.REPLAN

    # 4. 异步未完成 → WAIT
    if code in WAITABLE_ERRORS or any(
        kw in msg for kw in ["running", "queued", "awaiting", "pending", "approval"]
    ):
        return ReplanAction.WAIT

    # 5. 审核拒绝 → REPLAN
    if any(kw in msg for kw in ["rejected", "revision_requested"]):
        return ReplanAction.REPLAN

    # 6. 默认 → FAIL
    return ReplanAction.FAIL


# ── Replanner 核心 ─────────────────────────────────────────────────────

def _get_step_retry_count(db: Session, step_id: str) -> int:
    """获取步骤的已重试次数。"""
    from app.models.agent import AgentPlanStep

    step = db.get(AgentPlanStep, step_id)
    return (step.retry_count or 0) if step else 0


def _increment_retry(db: Session, step_id: str) -> int:
    """递增步骤重试计数并返回新值。"""
    from app.models.agent import AgentPlanStep

    step = db.get(AgentPlanStep, step_id)
    if step:
        step.retry_count = (step.retry_count or 0) + 1
        step.status = "pending"  # 重置为 pending 以便重试
        db.flush()
        return step.retry_count
    return 0


def _count_goal_replans(db: Session, goal_id: str) -> int:
    """统计 Goal 的已重规划次数（superseded Plan 数量）。"""
    from app.models.agent import AgentPlan

    from sqlalchemy import select

    count = db.scalar(
        select(AgentPlan).where(
            AgentPlan.goal_id == goal_id,
            AgentPlan.status == "superseded",
        )
    )
    # 简化：用 count 查询
    try:
        from sqlalchemy import func

        return db.scalar(
            select(func.count()).where(
                AgentPlan.goal_id == goal_id,
                AgentPlan.status == "superseded",
            )
        ) or 0
    except Exception:
        return 0


def replan(
    db: Session,
    *,
    error_ctx: ErrorContext,
    plan_id: str,
    goal_id: str,
    project_id: str | None = None,
    current_description: str | None = None,
) -> ReplanDecision:
    """执行重规划决策。

    流程：
    1. 分类错误 → 确定 action
    2. action=RETRY：递增 retry_count，重置 step=ready
    3. action=REPLAN：检查 replan 上限，生成新 Plan（版本+1）
    4. action=WAIT：更新 step=waiting_job/waiting_approval
    5. action=FAIL/SKIP/CANCEL：更新 step 状态
    """
    action = classify_error(error_ctx)
    decision = ReplanDecision(action=action, reason=f"错误分类: {error_ctx.error_code}")

    from app.models.agent import AgentPlanStep

    step = db.get(AgentPlanStep, error_ctx.step_id)
    if step is None:
        decision.action = ReplanAction.FAIL
        decision.reason = f"步骤 {error_ctx.step_id} 不存在"
        return decision

    if action == ReplanAction.RETRY:
        new_count = _increment_retry(db, error_ctx.step_id)
        decision.reason = f"重试步骤（第 {new_count}/{error_ctx.max_retries} 次）"
        decision.suggestion = "步骤已重置为 pending，等待重新执行"
        decision.next_step_ids = [error_ctx.step_id]

    elif action == ReplanAction.REPLAN:
        replan_count = _count_goal_replans(db, goal_id)
        if replan_count >= MAX_REPLAN_PER_GOAL:
            decision.action = ReplanAction.FAIL
            decision.reason = f"超过最大重规划次数 ({MAX_REPLAN_PER_GOAL})"
            step.status = "failed"
            step.error_message = f"超过最大重规划次数 ({MAX_REPLAN_PER_GOAL})"
            db.flush()
            return decision

        # 生成新 Plan（修订版）
        desc = current_description or step.description or "自动 Replan"
        plan, report = build_and_save_plan(
            db,
            goal_id=goal_id,
            description=f"[Replan v{replan_count + 1}] {desc}",
            project_id=project_id,
        )
        decision.updated_plan = plan
        decision.reason = f"生成新计划 v{plan.revision}（第 {replan_count + 1} 次 Replan）"
        decision.suggestion = "原 Plan 已 superseded，新 Plan 待执行"
        decision.next_step_ids = [s.get("id", "") for s in get_next_pending_steps(db, plan.plan_id)]

        # 标记当前步骤
        step.status = "failed"
        step.error_message = f"Replan 已生成新版本 plan_id={plan.plan_id}"
        db.flush()

    elif action == ReplanAction.WAIT:
        # 判断等待类型
        if any(kw in (error_ctx.error_code or "").lower() for kw in ["approval"]):
            step.status = "waiting_approval"
        else:
            step.status = "waiting_job"
        decision.reason = f"等待外部事件完成: {error_ctx.error_code}"
        decision.suggestion = "步骤已进入等待状态，外部事件完成后自动恢复"
        db.flush()

    elif action == ReplanAction.SKIP:
        step.status = "skipped"
        decision.reason = "跳过当前步骤"
        db.flush()

    else:  # FAIL / CANCEL
        step.status = "failed" if action == ReplanAction.FAIL else "cancelled"
        step.error_message = error_ctx.error_message or decision.reason
        decision.reason = f"步骤标记为 {step.status}: {error_ctx.error_code}"
        db.flush()

    return decision


def replan_from_verification(
    db: Session,
    *,
    step_id: str,
    plan_id: str,
    goal_id: str,
    verification: StepVerificationResult,
    execution_error: dict | None = None,
    project_id: str | None = None,
) -> ReplanDecision:
    """从验证结果触发重规划（Verifier → Replanner 标准衔接点）。

    如果 verification.verified=True，则跳过重规划，直接返回成功。
    否则构造 ErrorContext 并调用 replan()。
    """
    if verification.verified:
        return ReplanDecision(
            action=ReplanAction.RETRY,  # 语义：继续下一步
            reason="验证通过，无需重规划",
        )

    # 构造错误上下文
    error_info = verification.issues[0] if verification.issues else {}
    error_ctx = ErrorContext(
        step_id=step_id,
        tool_name=execution_error.get("tool_name", "") if execution_error else "",
        error_code=error_info.get("check", "VERIFICATION_FAILED"),
        error_message=error_info.get("reason", "验证失败"),
        retry_count=_get_step_retry_count(db, step_id),
        verification=verification,
    )

    return replan(
        db,
        error_ctx=error_ctx,
        plan_id=plan_id,
        goal_id=goal_id,
        project_id=project_id,
    )


# ── 状态查询 ──────────────────────────────────────────────────────────

def get_replan_history(db: Session, goal_id: str) -> list[dict]:
    """获取 Goal 的所有 Replan 历史（superseded Plan 列表）。"""
    from app.models.agent import AgentPlan

    from sqlalchemy import select

    rows = db.scalars(
        select(AgentPlan)
        .where(AgentPlan.goal_id == goal_id)
        .order_by(AgentPlan.revision.desc())
    ).all()

    return [
        {
            "plan_id": r.id,
            "revision": r.revision,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
