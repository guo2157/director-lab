"""Agent Runtime：完整 Agent Loop 编排入口（Phase 2.5 闭环）。

职责：
- 编排完整调用链：Goal→Context→Constraint→Plan→Execute→Verify→(Replan)→Complete
- 任务恢复：页面刷新/服务重启后可重读 agent_plan + agent_plan_step 继续
- 执行日志：每次 Loop 写入 agent_execution_log
- 异步 Job 桥接：Job 完成后通过 bridge_job_event 触发 Loop 恢复
- 审批恢复：审核完成后通过 bridge_approval_resolved 触发 Loop 恢复

设计约束（AGENTS.md P0）：
- 模型只负责识别与解释，任务创建与状态流转由确定性业务代码执行
- 保留现有 SSE 对话流，Runtime 注入 plan_id/goal_id 而非替代
- 重复请求防重：goal_id + session_id 去重
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.models.ids import agent_execution_log_id
from app.services.goal_manager import (
    cancel_goal,
    check_goal_completion,
    complete_goal,
    fail_goal,
    get_active_goal,
    get_or_create_goal,
    Goal,
)
from app.services.context_resolver import ContextSnapshot, snapshot
from app.services.constraint_manager import ConstraintReport, check_constraints


# ── 枚举 ──────────────────────────────────────────────────────────────

class LoopStatus(Enum):
    """Agent Loop 终态。"""

    COMPLETED = "completed"           # 目标完成
    FAILED = "failed"                 # 不可恢复失败
    BLOCKED = "blocked"              # 约束阻止
    WAITING_APPROVAL = "waiting_approval"  # 等待人工审批
    WAITING_JOB = "waiting_job"      # 等待异步任务
    WAITING_USER = "waiting_user"    # 等待用户输入
    PAUSED = "paused"                # 用户暂停
    CANCELLED = "cancelled"          # 用户取消


class StepResult(Enum):
    """单步执行结果。"""

    SUCCESS = "success"
    FAILED = "failed"
    WAITING = "waiting"
    SKIPPED = "skipped"
    REJECTED = "rejected"


# ── 数据类 ────────────────────────────────────────────────────────────

class ExecutionRecord:
    """单次执行记录（写入 agent_execution_log 前）。"""

    def __init__(
        self,
        *,
        goal_id: str | None = None,
        plan_id: str | None = None,
        step_id: str | None = None,
        tool_name: str | None = None,
        input_data: dict | None = None,
        output_data: dict | None = None,
        status: str = "started",
        error_code: str | None = None,
        error_message: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        duration_ms: int | None = None,
    ):
        self.goal_id = goal_id
        self.plan_id = plan_id
        self.step_id = step_id
        self.tool_name = tool_name
        self.input = input_data
        self.output = output_data
        self.status = status
        self.error_code = error_code
        self.error_message = error_message
        self.started_at = started_at or datetime.now()
        self.completed_at = completed_at
        self.duration_ms = duration_ms


@dataclass
class AgentLoopResult:
    """Agent Loop 完整执行结果。

    供 Agent API 消费，注入 SSE 流中通知前端。
    """

    status: str = "completed"         # completed / failed / blocked / waiting / paused / cancelled
    goal_id: str | None = None
    plan_id: str | None = None
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    skipped_steps: int = 0
    message: str = ""
    errors: list[dict] = field(default_factory=list)
    context_snapshot: dict | None = None
    next_action: str | None = None    # pending / wait_approval / wait_job / none
    wait_step_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "goal_id": self.goal_id,
            "plan_id": self.plan_id,
            "total_steps": self.total_steps,
            "completed_steps": self.completed_steps,
            "failed_steps": self.failed_steps,
            "skipped_steps": self.skipped_steps,
            "message": self.message,
            "errors": self.errors,
            "context_snapshot": self.context_snapshot,
            "next_action": self.next_action,
            "wait_step_id": self.wait_step_id,
        }


# ── 执行日志 ──────────────────────────────────────────────────────────

def log_execution(
    db: Session,
    record: ExecutionRecord,
) -> str:
    """写入 agent_execution_log 并返回 log_id。"""
    from app.models.agent import AgentExecutionLog

    log = AgentExecutionLog(
        id=agent_execution_log_id(),
        goal_id=record.goal_id,
        plan_id=record.plan_id,
        step_id=record.step_id,
        tool_name=record.tool_name,
        input=record.input,
        output=record.output,
        status=record.status,
        error_code=record.error_code,
        error_message=record.error_message,
        started_at=record.started_at,
        completed_at=record.completed_at,
        duration_ms=record.duration_ms,
    )
    db.add(log)
    db.flush()
    return log.id


def log_step_start(
    db: Session,
    *,
    goal_id: str,
    plan_id: str | None = None,
    step_id: str | None = None,
    tool_name: str | None = None,
    input_data: dict | None = None,
) -> str:
    """记录步骤开始。"""
    return log_execution(
        db,
        ExecutionRecord(
            goal_id=goal_id,
            plan_id=plan_id,
            step_id=step_id,
            tool_name=tool_name,
            input_data=input_data,
            status="started",
        ),
    )


def log_step_end(
    db: Session,
    *,
    goal_id: str,
    plan_id: str | None = None,
    step_id: str | None = None,
    tool_name: str | None = None,
    result: StepResult,
    output_data: dict | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    started_at: datetime | None = None,
) -> str:
    """记录步骤结束（成功/失败/跳过）。"""
    now = datetime.now()
    duration = None
    if started_at:
        duration = int((now - started_at).total_seconds() * 1000)
    return log_execution(
        db,
        ExecutionRecord(
            goal_id=goal_id,
            plan_id=plan_id,
            step_id=step_id,
            tool_name=tool_name,
            output_data=output_data,
            status="succeeded" if result == StepResult.SUCCESS else result.value,
            error_code=error_code,
            error_message=error_message,
            started_at=started_at,
            completed_at=now,
            duration_ms=duration,
        ),
    )


# ── Agent Runtime ─────────────────────────────────────────────────────


def start_or_resume_goal(
    db: Session,
    *,
    session_id: str,
    description: str,
    project_id: str | None = None,
    goal_type: str = "user_request",
    constraints: dict | None = None,
    success_criteria: list | None = None,
) -> Goal:
    """启动或恢复一个目标。同一 session 已有 active 目标时复用（任务恢复）。"""
    return get_or_create_goal(
        db,
        session_id=session_id,
        description=description,
        project_id=project_id,
        goal_type=goal_type,
        constraints=constraints,
        success_criteria=success_criteria,
    )


def build_goal_context(
    db: Session,
    *,
    session_id: str,
    project_id: str | None = None,
    goal_id: str | None = None,
) -> ContextSnapshot:
    """构建目标执行上下文（Context Resolver snapshot）。"""
    return snapshot(
        db,
        session_id=session_id,
        project_id=project_id,
        goal_id=goal_id,
    )


def check_goal_constraints(
    db: Session,
    *,
    goal_constraints: dict | None = None,
    project_id: str | None = None,
) -> ConstraintReport:
    """执行目标级约束检查。"""
    return check_constraints(
        db,
        goal_constraints=goal_constraints,
        project_id=project_id,
    )


def can_proceed(report: ConstraintReport) -> bool:
    """约束报告是否允许继续执行（无 BLOCK 级别拒绝）。"""
    return report.passed


def finalize_goal(db: Session, goal_id: str) -> dict:
    """完成目标并返回最终状态。"""
    completion = check_goal_completion(db, goal_id)
    if completion["is_complete"]:
        complete_goal(db, goal_id)
    return completion


def abort_goal(db: Session, goal_id: str, *, reason: str | None = None) -> None:
    """中止目标（用户取消或不可恢复错误）。"""
    cancel_goal(db, goal_id)
    log_execution(
        db,
        ExecutionRecord(
            goal_id=goal_id,
            status="cancelled",
            error_message=reason,
        ),
    )


# ═══════════════════════════════════════════════════════════════════════
#  Agent Loop 完整闭环（Phase 2.5）
# ═══════════════════════════════════════════════════════════════════════

_MAX_LOOP_ITERATIONS = 50  # 防止死循环


def run_agent_loop(
    db: Session,
    *,
    session_id: str,
    description: str,
    project_id: str | None = None,
    goal_type: str = "user_request",
    constraints: dict | None = None,
    success_criteria: list | None = None,
    approved: bool = False,
) -> AgentLoopResult:
    """Agent Loop 完整编排入口。

    流程：
    A. Goal 解析（创建或恢复）
    B. Context Snapshot
    C. Constraint 检查
    D. Goal 完成度判定
    E. Plan 获取或生成
    F. 执行循环（按步执行 + 验证 + 重规划）
    G. 目标终结

    返回 AgentLoopResult 供 Agent API 消费。
    """
    result = AgentLoopResult()

    # ── A. Goal 解析 ──
    goal = start_or_resume_goal(
        db,
        session_id=session_id,
        description=description,
        project_id=project_id,
        goal_type=goal_type,
        constraints=constraints,
        success_criteria=success_criteria,
    )
    result.goal_id = goal.goal_id

    # 记录 Goal 创建/恢复日志
    log_execution(
        db,
        ExecutionRecord(
            goal_id=goal.goal_id,
            status="goal_active",
            input_data={"description": description, "project_id": project_id},
        ),
    )

    # ── B. Context Snapshot ──
    ctx = build_goal_context(
        db,
        session_id=session_id,
        project_id=project_id,
        goal_id=goal.goal_id,
    )
    result.context_snapshot = ctx.to_dict()

    # ── C. Constraint 检查 ──
    constraint_report = check_goal_constraints(
        db,
        goal_constraints=goal.constraints,
        project_id=project_id,
    )
    if not constraint_report.passed:
        result.status = "blocked"
        result.message = "约束检查未通过"
        result.errors = [
            {"constraint": name, "message": f"被 {name} 阻止"}
            for name in constraint_report.blocked_by
        ]
        log_execution(
            db,
            ExecutionRecord(
                goal_id=goal.goal_id,
                status="blocked",
                error_message="; ".join(constraint_report.blocked_by),
            ),
        )
        return result

    # ── D. Goal 完成度判定 ──
    completion = check_goal_completion(db, goal.goal_id)
    if completion["is_complete"]:
        finalize_goal(db, goal.goal_id)
        result.status = "completed"
        result.message = "目标已完成（无需新 Plan）"
        return result

    # ── E. Plan 获取或生成 ──
    plan = _get_or_create_plan(
        db,
        goal_id=goal.goal_id,
        description=description,
        project_id=project_id,
    )
    if plan is None:
        result.status = "failed"
        result.message = "Plan 生成失败"
        return result

    result.plan_id = plan.plan_id

    # ── F. 执行循环 ──
    loop_result = _execute_plan_loop(
        db,
        goal=goal,
        plan=plan,
        session_id=session_id,
        project_id=project_id,
        approved=approved,
    )

    # ── G. 汇总结果 ──
    result.status = loop_result["status"]
    result.message = loop_result["message"]
    result.total_steps = loop_result["total_steps"]
    result.completed_steps = loop_result["completed_steps"]
    result.failed_steps = loop_result["failed_steps"]
    result.skipped_steps = loop_result["skipped_steps"]
    result.next_action = loop_result["next_action"]
    result.wait_step_id = loop_result.get("wait_step_id")
    result.errors = loop_result["errors"]

    # 记录 Loop 完成日志
    log_execution(
        db,
        ExecutionRecord(
            goal_id=goal.goal_id,
            plan_id=plan.plan_id,
            status=result.status,
            output_data={
                "completed": result.completed_steps,
                "failed": result.failed_steps,
                "skipped": result.skipped_steps,
            },
        ),
    )

    # 终态处理
    if result.status == "completed":
        finalize_goal(db, goal.goal_id)
    elif result.status == "failed":
        fail_goal(db, goal.goal_id)

    return result


def _get_or_create_plan(
    db: Session,
    *,
    goal_id: str,
    description: str,
    project_id: str | None = None,
) -> Any | None:
    """获取或创建执行计划。优先复用已有 active Plan。"""
    from app.services.planner import build_and_save_plan, get_latest_plan

    existing = get_latest_plan(db, goal_id)
    if existing:
        return existing

    plan, report = build_and_save_plan(
        db,
        goal_id=goal_id,
        description=description,
        project_id=project_id,
    )

    if not report.passed:
        log_execution(
            db,
            ExecutionRecord(
                goal_id=goal_id,
                status="plan_validation_failed",
                error_message=f"Plan 校验失败: {report.errors} errors",
            ),
        )
        return None

    return plan


def _execute_plan_loop(
    db: Session,
    *,
    goal: Goal,
    plan: Any,
    session_id: str,
    project_id: str | None = None,
    approved: bool = False,
) -> dict:
    """执行 Plan 步骤循环。

    返回执行摘要字典。
    """
    from app.services.planner import get_next_pending_steps, get_latest_plan
    from app.services.execution_manager import execute_step
    from app.services.verifier import verify_step
    from app.services.replanner import (
        ReplanAction,
        replan_from_verification,
    )

    completed = 0
    failed = 0
    skipped = 0
    errors: list[dict] = []
    iterations = 0
    current_plan = plan
    total_steps = len(plan.steps)

    while iterations < _MAX_LOOP_ITERATIONS:
        iterations += 1

        # 1. 获取下一步待执行步骤
        pending = get_next_pending_steps(db, current_plan.plan_id)
        if not pending:
            # 检查是否全部完成
            from app.services.verifier import verify_plan_completion

            plan_status = verify_plan_completion(db, plan_id=current_plan.plan_id)
            if plan_status["all_verified"]:
                return {
                    "status": "completed",
                    "message": f"所有步骤执行完毕（{completed} 成功 / {failed} 失败 / {skipped} 跳过）",
                    "total_steps": total_steps,
                    "completed_steps": completed,
                    "failed_steps": failed,
                    "skipped_steps": skipped,
                    "next_action": "none",
                    "errors": errors,
                }
            # 还有未完成的
            if plan_status["pending_steps"]:
                return {
                    "status": "waiting",
                    "message": f"{len(plan_status['pending_steps'])} 个步骤待执行（可能被阻塞）",
                    "total_steps": total_steps,
                    "completed_steps": completed,
                    "failed_steps": failed,
                    "skipped_steps": skipped,
                    "next_action": "pending",
                    "errors": errors,
                }
            return {
                "status": "completed",
                "message": "无可执行步骤",
                "total_steps": total_steps,
                "completed_steps": completed,
                "failed_steps": failed,
                "skipped_steps": skipped,
                "next_action": "none",
                "errors": errors,
            }

        # 2. 逐步骤执行
        for step_info in pending:
            step_id = step_info["id"]
            tool_name = step_info.get("tool_name", "")

            # 2a. 执行
            exec_result = execute_step(
                db,
                step_id=step_id,
                plan_id=current_plan.plan_id,
                goal_id=goal.goal_id,
                session_id=session_id,
                project_id=project_id,
                approved=approved,
            )

            # 2b. 审批等待
            if exec_result.get("policy") == "REQUIRE_CONFIRMATION":
                return {
                    "status": "waiting_approval",
                    "message": f"步骤 '{tool_name}' 需要人工审批",
                    "total_steps": total_steps,
                    "completed_steps": completed,
                    "failed_steps": failed,
                    "skipped_steps": skipped,
                    "next_action": "wait_approval",
                    "wait_step_id": step_id,
                    "errors": errors,
                }

            # 2c. 验证
            verification = verify_step(
                db,
                step_id=step_id,
                tool_name=tool_name,
                result_data=exec_result.get("result"),
                project_id=project_id,
            )

            if verification.verified:
                completed += 1
                continue

            # 2d. Replan
            decision = replan_from_verification(
                db,
                step_id=step_id,
                plan_id=current_plan.plan_id,
                goal_id=goal.goal_id,
                verification=verification,
                execution_error=exec_result,
                project_id=project_id,
            )

            if decision.action == ReplanAction.RETRY:
                skipped += 1
                continue  # 步骤已重置为 pending，下次循环重试

            elif decision.action == ReplanAction.REPLAN:
                if decision.updated_plan:
                    current_plan = decision.updated_plan
                    total_steps = len(current_plan.steps)
                break  # 退出内层 for，用新 Plan 重新开始

            elif decision.action == ReplanAction.WAIT:
                return {
                    "status": "waiting_job",
                    "message": decision.reason,
                    "total_steps": total_steps,
                    "completed_steps": completed,
                    "failed_steps": failed,
                    "skipped_steps": skipped,
                    "next_action": "wait_job",
                    "wait_step_id": step_id,
                    "errors": errors,
                }

            elif decision.action == ReplanAction.SKIP:
                skipped += 1
                continue

            else:  # FAIL / CANCEL
                failed += 1
                errors.append({
                    "step_id": step_id,
                    "tool_name": tool_name,
                    "error": exec_result.get("error", "未知错误"),
                    "verification": verification.to_dict(),
                })
                continue

        # 如果 Plan 被替换，继续外层循环
        continue

    # 超过最大迭代次数
    return {
        "status": "failed",
        "message": f"超过最大迭代次数 ({_MAX_LOOP_ITERATIONS})，可能存在死循环或 Replan 无限触发",
        "total_steps": total_steps,
        "completed_steps": completed,
        "failed_steps": failed,
        "skipped_steps": skipped,
        "next_action": "none",
        "errors": errors,
    }


# ── 异步恢复入口 ─────────────────────────────────────────────────────

def resume_after_job(
    db: Session,
    *,
    goal_id: str,
    plan_id: str,
    step_id: str,
    job_id: str,
    job_status: str,
    session_id: str | None = None,
    project_id: str | None = None,
) -> dict:
    """异步 Job 完成后恢复 Agent Loop（bridge_job_event 回调）。

    由 task_orchestrator.bridge_job_event 触发调用。
    """
    from app.services.execution_manager import get_step_execution_status
    from app.services.verifier import verify_step

    status = get_step_execution_status(db, step_id)
    if not status["found"]:
        return {"resumed": False, "error": "step_not_found"}

    # 更新 step 状态
    from app.models.agent import AgentPlanStep

    step = db.get(AgentPlanStep, step_id)
    if step and step.status == "waiting_job":
        if job_status == "succeeded":
            step.status = "succeeded"
        elif job_status == "failed":
            step.status = "failed"
            step.error_message = f"Job {job_id} failed"
        else:
            step.status = "running" if job_status == "running" else "waiting_job"
        db.flush()

    # 验证
    verification = verify_step(
        db,
        step_id=step_id,
        project_id=project_id,
    )

    log_execution(
        db,
        ExecutionRecord(
            goal_id=goal_id,
            plan_id=plan_id,
            step_id=step_id,
            tool_name="bridge_job_event",
            status=f"job_{job_status}",
            output_data={"job_id": job_id, "verification": verification.to_dict()},
        ),
    )

    return {
        "resumed": True,
        "step_id": step_id,
        "verification": verification.to_dict(),
    }


def resume_after_approval(
    db: Session,
    *,
    goal_id: str,
    plan_id: str,
    step_id: str,
    approved: bool = True,
    project_id: str | None = None,
    session_id: str | None = None,
) -> AgentLoopResult:
    """审批完成后恢复 Agent Loop（bridge_approval_resolved 回调）。

    由 Human Gate 通过审批事件触发调用。
    直接重新进入 run_agent_loop 推进下一步骤。
    """
    from app.models.agent import AgentPlanStep

    step = db.get(AgentPlanStep, step_id)
    if step and step.status == "waiting_approval":
        if approved:
            step.status = "pending"
            step.requires_approval = False
        else:
            step.status = "cancelled"
            step.error_message = "审批被拒绝"
        db.flush()

    log_execution(
        db,
        ExecutionRecord(
            goal_id=goal_id,
            plan_id=plan_id,
            step_id=step_id,
            tool_name="bridge_approval_resolved",
            status=f"approval_{'approved' if approved else 'rejected'}",
        ),
    )

    # 继续推进
    goal = get_active_goal(db, session_id or "")
    if goal is None:
        return AgentLoopResult(status="failed", goal_id=goal_id, message="目标不存在")

    # 获取当前 Plan
    from app.services.planner import get_latest_plan

    plan = get_latest_plan(db, goal_id)
    if plan is None:
        return AgentLoopResult(status="failed", goal_id=goal_id, message="Plan 不存在")

    # 继续执行循环
    loop_result = _execute_plan_loop(
        db,
        goal=goal,
        plan=plan,
        session_id=session_id or "",
        project_id=project_id,
        approved=approved,
    )

    return AgentLoopResult(
        status=loop_result["status"],
        goal_id=goal_id,
        plan_id=plan_id,
        total_steps=loop_result["total_steps"],
        completed_steps=loop_result["completed_steps"],
        failed_steps=loop_result["failed_steps"],
        skipped_steps=loop_result["skipped_steps"],
        message=loop_result["message"],
        next_action=loop_result["next_action"],
        wait_step_id=loop_result.get("wait_step_id"),
    )


# ── Runtime 状态查询 ─────────────────────────────────────────────────

def get_goal_status(db: Session, session_id: str) -> dict:
    """获取当前会话的目标执行状态。供前端 Agent Workbench 展示。"""
    goal = get_active_goal(db, session_id)
    if goal is None:
        return {"has_active_goal": False}

    completion = check_goal_completion(db, goal.goal_id)

    plan_info: dict[str, Any] = {}
    try:
        from sqlalchemy import select
        from app.models.agent import AgentPlan

        plan_row = db.scalars(
            select(AgentPlan)
            .where(AgentPlan.goal_id == goal.goal_id)
            .order_by(AgentPlan.created_at.desc())
            .limit(1)
        ).first()
        if plan_row:
            plan_info = {
                "plan_id": plan_row.id,
                "revision": plan_row.revision,
                "status": plan_row.status,
            }
    except Exception:
        pass

    return {
        "has_active_goal": True,
        "goal": goal.to_dict(),
        "completion": completion,
        "latest_plan": plan_info or None,
    }


def get_execution_logs(
    db: Session,
    *,
    goal_id: str | None = None,
    plan_id: str | None = None,
    step_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """查询执行日志（按时间倒序）。"""
    from app.models.agent import AgentExecutionLog
    from sqlalchemy import select, and_

    conditions = []
    if goal_id:
        conditions.append(AgentExecutionLog.goal_id == goal_id)
    if plan_id:
        conditions.append(AgentExecutionLog.plan_id == plan_id)
    if step_id:
        conditions.append(AgentExecutionLog.step_id == step_id)

    query = select(AgentExecutionLog).order_by(
        AgentExecutionLog.created_at.desc()
    ).limit(limit)
    if conditions:
        query = query.where(and_(*conditions))

    rows = db.scalars(query).all()
    return [
        {
            "id": r.id,
            "goal_id": r.goal_id,
            "plan_id": r.plan_id,
            "step_id": r.step_id,
            "tool_name": r.tool_name,
            "status": r.status,
            "error_code": r.error_code,
            "error_message": (r.error_message or "")[:200],
            "duration_ms": r.duration_ms,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
