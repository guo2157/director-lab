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
    WAIT = "wait"             # 等待外部事件（Job / 审批）
    WAIT_USER = "wait_user"   # 等待用户处理（前置条件缺失等，需要用户先做事）
    FAIL = "fail"             # 标记失败
    SKIP = "skip"             # 跳过当前步骤
    CANCEL = "cancel"         # 取消整个目标


# A9：error_code → 恢复动作的显式映射表（优先于 message 关键词）
# 只有不在表里的错误码才回退到关键词分类，避免 "invalid"/"pending"/"running" 这类
# 过宽的关键词把业务错误误判成 REPLAN 或 WAIT。
ERROR_CODE_ACTIONS: dict[str, ReplanAction] = {
    # 瞬态：可重试
    "tool_internal_error": ReplanAction.RETRY,
    # 生成类 Job 失败：Phase B 不自动重新提交生成（Worker 内部已有重试与熔断），
    # 交给用户决定 → waiting_user
    "job_failed": ReplanAction.WAIT_USER,
    "job_cancelled": ReplanAction.FAIL,
    # 规则计划无法自动修正：直接失败，避免「生成同样的计划 → 同样失败」重复 3 次
    "tool_invalid_args": ReplanAction.FAIL,
    "tool_not_found": ReplanAction.FAIL,
    # 前置条件缺失：需要用户先补（剧本 / 解析 / 资产等）
    "precondition_failed": ReplanAction.WAIT_USER,
    # 审批 / 等待
    "approval_required": ReplanAction.WAIT,
    "approval_rejected": ReplanAction.WAIT_USER,
    # 策略 / 预算
    "policy_denied": ReplanAction.FAIL,
    "budget_exceeded": ReplanAction.FAIL,
    # 结果校验
    "verification_failed": ReplanAction.REPLAN,
}


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
    """错误分类：优先按 error_code 映射，只有未知错误码才回退到 message 关键词（A9）。

    优先级：
    1. ERROR_CODE_ACTIONS 显式映射
    2. 瞬态错误（网络/超时）且未超重试上限 → RETRY
    3. 异步未完成（Job 运行中 / 等待审批）→ WAIT
    4. 其他 → FAIL

    已删除 "invalid"、"pending"、"running" 这类过宽关键词：它们会把很多业务错误
    误判为 REPLAN / WAIT。
    """
    code = ctx.error_code or ""
    msg = (ctx.error_message or "").lower()

    # 复合错误码（如 job_failed:oom）按前缀映射到基础分类
    base_code = code.split(":", 1)[0]
    if base_code in ERROR_CODE_ACTIONS:
        action = ERROR_CODE_ACTIONS[base_code]
        if action == ReplanAction.RETRY and ctx.retry_count >= ctx.max_retries:
            return ReplanAction.FAIL
        return action

    # 没有显式映射：只保留少数安全关键词
    if code in RETRYABLE_ERRORS:
        return (
            ReplanAction.RETRY
            if ctx.retry_count < ctx.max_retries
            else ReplanAction.FAIL
        )
    if code in WAITABLE_ERRORS:
        return ReplanAction.WAIT

    if any(kw in msg for kw in ["timeout", "connection reset", "temporarily unavailable"]):
        return (
            ReplanAction.RETRY
            if ctx.retry_count < ctx.max_retries
            else ReplanAction.FAIL
        )

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


def _count_goal_replans(db: Session, goal_id: str, plan_id: str | None = None) -> int:
    """统计 Goal 的已重规划次数：沿 parent_plan_id 链向上数（A9）。

    不再用 superseded Plan 数量统计——A8 的 supersede（换目标）不应计入 replan 次数。
    """
    from app.models.agent import AgentPlan

    if plan_id is None:
        latest = get_latest_plan(db, goal_id)
        if latest is None:
            return 0
        plan_id = latest.plan_id

    count = 0
    seen: set[str] = set()
    current = db.get(AgentPlan, plan_id)
    while current is not None and current.id not in seen:
        seen.add(current.id)
        parent_id = getattr(current, "parent_plan_id", None)
        if not parent_id:
            break
        count += 1
        current = db.get(AgentPlan, parent_id)
    return count


def _same_step_definition(db: Session, previous, new_plan, step) -> bool:
    """新旧计划中同一 step_key 的工具与参数是否完全一致（决定能否自动修正）。"""
    if previous is None:
        return False
    old_spec = next((p for p in previous.steps if p.step_key == step.step_key), None)
    new_spec = next((p for p in new_plan.steps if p.step_key == step.step_key), None)
    if old_spec is None or new_spec is None:
        return False
    return (
        (old_spec.tool_name or "") == (new_spec.tool_name or "")
        and (old_spec.args or {}) == (new_spec.args or {})
    )


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
        replan_count = _count_goal_replans(db, goal_id, plan_id)
        if replan_count >= MAX_REPLAN_PER_GOAL:
            decision.action = ReplanAction.FAIL
            decision.reason = f"超过最大重规划次数 ({MAX_REPLAN_PER_GOAL})"
            step.status = "failed"
            step.error_message = f"超过最大重规划次数 ({MAX_REPLAN_PER_GOAL})"
            db.flush()
            return decision

        # 生成新 Plan（修订版）：必须传 previous_plan，否则 revision 恒为 1
        previous = get_latest_plan(db, goal_id)
        desc = current_description or step.description or "自动 Replan"
        new_plan, report = build_and_save_plan(
            db,
            goal_id=goal_id,
            description=desc,
            project_id=project_id,
            previous_plan=previous,
        )
        if not report.passed:
            decision.action = ReplanAction.FAIL
            decision.reason = f"Replan 生成的新计划未通过校验：{report.errors} 个错误"
            step.status = "failed"
            step.error_message = decision.reason
            db.flush()
            return decision

        # 关联父计划（供 replan 次数统计与计划历史展示）
        from app.models.agent import AgentPlan

        new_row = db.get(AgentPlan, new_plan.plan_id)
        if new_row is not None and previous is not None:
            new_row.parent_plan_id = previous.plan_id

        # 复用已完成且仍有效的步骤：不重复执行
        from app.services.planner import carry_over_steps

        carried = carry_over_steps(db, plan_id, new_plan) if previous is not None else []

        decision.updated_plan = new_plan
        decision.reason = (
            f"生成新计划 v{new_plan.revision}（第 {replan_count + 1} 次 Replan，"
            f"复用 {len(carried)} 个已完成步骤）"
        )
        decision.suggestion = "原 Plan 已 superseded，新 Plan 待执行"
        decision.next_step_ids = [
            s.get("id", "") for s in get_next_pending_steps(db, new_plan.plan_id)
        ]

        # 标记当前步骤
        step.status = "failed"
        step.error_message = f"Replan 已生成新版本 plan_id={new_plan.plan_id}"
        db.flush()

        # 规则计划无法自动修正：新计划中同一 step_key 的工具与参数若与旧计划完全一致，
        # 再执行一次只会同样失败 → 快速失败，避免「生成同样的计划 → 同样失败」重复 3 次。
        if error_ctx.error_code in ("tool_invalid_args", "precondition_failed", "tool_not_found"):
            if _same_step_definition(db, previous, new_plan, step):
                decision.action = ReplanAction.FAIL
                decision.reason = f"规则计划无法自动修正：{error_ctx.error_code}"
                decision.suggestion = "请修改目标描述或先补齐前置条件后再试"
                decision.updated_plan = None
                step.status = "failed"
                step.error_code = error_ctx.error_code
                step.error_message = decision.reason
                db.flush()
                return decision

    elif action == ReplanAction.WAIT:
        # 判断等待类型
        if any(kw in (error_ctx.error_code or "").lower() for kw in ["approval"]):
            step.status = "waiting_approval"
        else:
            step.status = "waiting_job"
        decision.reason = f"等待外部事件完成: {error_ctx.error_code}"
        decision.suggestion = "步骤已进入等待状态，外部事件完成后自动恢复"
        db.flush()

    elif action == ReplanAction.WAIT_USER:
        step.status = "waiting_user"
        step.error_code = error_ctx.error_code
        step.error_message = error_ctx.error_message or "需要用户先处理前置条件"
        decision.reason = f"等待用户处理：{error_ctx.error_code}"
        decision.suggestion = error_ctx.error_message or "请先补齐前置条件后重试"
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
    # A9：优先使用 ToolResult 的分类码（tool_invalid_args / precondition_failed / ...），
    # 只有拿不到 error_code 时才回退到验证检查名。
    exec_code = None
    if isinstance(execution_error, dict):
        exec_code = execution_error.get("error_code")
        if not exec_code:
            inner = execution_error.get("result")
            if isinstance(inner, dict):
                exec_code = inner.get("error_code")
    exec_message = None
    if isinstance(execution_error, dict):
        exec_message = execution_error.get("error")
        if not exec_message:
            inner = execution_error.get("result")
            if isinstance(inner, dict):
                exec_message = inner.get("error_message")
    error_ctx = ErrorContext(
        step_id=step_id,
        tool_name=execution_error.get("tool_name", "") if execution_error else "",
        error_code=exec_code or error_info.get("check", "verification_failed"),
        # 工具错误信息优先（「缺少剧本」比「步骤状态为 failed」对用户更有意义）
        error_message=exec_message or error_info.get("reason", "验证失败"),
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
