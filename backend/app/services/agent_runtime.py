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

事务边界（Phase A A1，硬性约定）：
- 内部函数（goal_manager / planner / execution_manager / verifier / replanner）只 flush，不 commit。
- 只有 Runtime 对外入口负责提交：run_agent_loop、resume_after_job、resume_after_approval、
  run_agent_loop_in_new_session。
- _execute_plan_loop 在每个步骤完成「执行 → 验证 → 重规划」后提交一次，
  进程崩溃时已完成的步骤不会丢失；未捕获异常时先 rollback，再在新事务中把步骤标记为 failed。
- 禁止把请求作用域的 Session 传入其他线程；后台执行请用 run_agent_loop_in_new_session。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from collections.abc import Callable
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


# ── 事件名（A3：集中定义，agent_loop 的终止集合引用这里） ──────────────

class LoopEvent(str, Enum):
    """Agent Loop 进度事件名。"""

    GOAL_CREATED = "goal_created"
    GOAL_RESUMED = "goal_resumed"
    PLAN_GENERATED = "plan_generated"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    STEP_SKIPPED = "step_skipped"
    STEP_WAITING = "step_waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    WAITING = "waiting"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_JOB = "waiting_job"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    CANCELLED = "cancelled"


TERMINAL_EVENTS = frozenset(
    {
        LoopEvent.COMPLETED.value,
        LoopEvent.FAILED.value,
        LoopEvent.BLOCKED.value,
        LoopEvent.WAITING.value,
        LoopEvent.WAITING_APPROVAL.value,
        LoopEvent.WAITING_JOB.value,
        LoopEvent.WAITING_USER.value,
        LoopEvent.PAUSED.value,
        LoopEvent.CANCELLED.value,
    }
)


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
    progress_callback: Callable[[str, dict], None] | None = None,
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

    progress_callback(event_type, payload) 可选，用于 SSE 实时推送。
    event_type: goal_created | plan_generated | step_started | step_completed |
                step_failed | step_skipped | loop_completed | loop_failed |
                waiting_approval | waiting_job | blocked
    """
    def _emit(ev_type: str, payload: dict) -> None:
        if progress_callback:
            try:
                progress_callback(ev_type, payload)
            except Exception:
                pass  # 回调异常不影响主流程

    result = AgentLoopResult()

    # ── A. Goal 解析 ──
    from app.services.goal_manager import get_active_goal as _get_active_goal

    resumed = _get_active_goal(db, session_id) is not None
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
    db.commit()  # A1：Goal 创建/复用立即落库，后续崩溃不会丢目标
    _emit(
        LoopEvent.GOAL_RESUMED.value if resumed else LoopEvent.GOAL_CREATED.value,
        {"goal_id": goal.goal_id, "description": goal.description},
    )

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
    db.commit()  # A1：Plan 与其步骤落库后再执行
    _emit(
        LoopEvent.PLAN_GENERATED.value,
        {
            "goal_id": goal.goal_id,
            "plan_id": plan.plan_id,
            "revision": getattr(plan, "revision", 1),
            "steps": [
                {"step_key": st.step_key, "tool_name": st.tool_name, "description": st.description}
                for st in plan.steps
            ],
        },
    )

    # ── F. 执行循环 ──
    loop_result = _execute_plan_loop(
        db,
        goal=goal,
        plan=plan,
        session_id=session_id,
        project_id=project_id,
        progress_callback=progress_callback,
    )

    # ── G. 汇总结果（先写全字段，再发最终事件，A3/P0-4） ──
    result.status = loop_result["status"]
    result.message = loop_result["message"]
    result.total_steps = loop_result["total_steps"]
    result.completed_steps = loop_result["completed_steps"]
    result.failed_steps = loop_result["failed_steps"]
    result.skipped_steps = loop_result["skipped_steps"]
    result.next_action = loop_result["next_action"]
    result.wait_step_id = loop_result.get("wait_step_id")
    result.errors = loop_result["errors"]
    _emit(result.status, result.to_dict())

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


_TERMINAL_STEP_STATUSES = ("succeeded", "failed", "skipped", "cancelled")


def _plan_all_terminal(db: Session, plan_id: str) -> bool:
    """Plan 的所有步骤是否都已终态（A8：终态计划要生成新 revision，而不是直接返回已完成）。"""
    from app.services.planner import get_plan_steps

    steps = get_plan_steps(db, plan_id)
    if not steps:
        return False
    return all(s["status"] in _TERMINAL_STEP_STATUSES for s in steps)


def _get_or_create_plan(
    db: Session,
    *,
    goal_id: str,
    description: str,
    project_id: str | None = None,
) -> Any | None:
    """获取或创建执行计划。

    A8 / P0-10：
    - 有 active Plan 且仍有非终态步骤 → 直接复用
    - 有 active Plan 但所有步骤都已终态 → 生成新 revision（previous_plan=旧计划），
      不再把「上次已跑完」当成「本次已完成」
    """
    from app.services.planner import build_and_save_plan, get_latest_plan

    existing = get_latest_plan(db, goal_id)
    previous = None
    if existing:
        if not _plan_all_terminal(db, existing.plan_id):
            return existing
        previous = existing

    plan, report = build_and_save_plan(
        db,
        goal_id=goal_id,
        description=description,
        project_id=project_id,
        previous_plan=previous,
    )
    if report.passed and previous is not None:
        # A8/A9：新 revision 关联父计划，便于按链统计 replan 次数
        from app.models.agent import AgentPlan

        row = db.get(AgentPlan, plan.plan_id)
        if row is not None:
            row.parent_plan_id = previous.plan_id
            db.flush()

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
    progress_callback: Callable[[str, dict], None] | None = None,
) -> dict:
    """执行 Plan 步骤循环（崩溃隔离包装）。

    任何未捕获异常都不会向上抛：先 rollback，再把当前步骤标记为 failed 并提交，
    最后返回 status="failed" 的执行摘要。
    """
    try:
        return _run_plan_loop(
            db,
            goal=goal,
            plan=plan,
            session_id=session_id,
            project_id=project_id,
            progress_callback=progress_callback,
        )
    except Exception as exc:  # noqa: BLE001 - 崩溃隔离
        db.rollback()
        error_code = type(exc).__name__
        error_message = (str(exc) or error_code)[:1000]
        log_execution(
            db,
            ExecutionRecord(
                goal_id=goal.goal_id,
                plan_id=plan.plan_id,
                status="failed",
                error_code=error_code,
                error_message=error_message,
            ),
        )
        db.commit()
        return {
            "status": "failed",
            "message": f"执行异常已中止：{error_code}: {error_message}",
            "total_steps": len(plan.steps),
            "completed_steps": 0,
            "failed_steps": 1,
            "skipped_steps": 0,
            "next_action": "none",
            "wait_step_id": None,
            "errors": [
                {"step_id": None, "tool_name": None, "error": f"{error_code}: {error_message}"}
            ],
        }


def _run_plan_loop(
    db: Session,
    *,
    goal: Goal,
    plan: Any,
    session_id: str,
    project_id: str | None = None,
    progress_callback: Callable[[str, dict], None] | None = None,
) -> dict:
    """执行 Plan 步骤循环主体。每个步骤的「执行 → 验证 → 重规划」结束后提交一次。"""
    from app.services.planner import get_next_pending_steps
    from app.services.execution_manager import execute_step
    from app.services.verifier import verify_step
    from app.services.replanner import (
        ReplanAction,
        replan_from_verification,
    )

    def _emit(ev_type: str, payload: dict) -> None:
        if progress_callback:
            try:
                progress_callback(ev_type, payload)
            except Exception:
                pass

    from app.services.planner import get_next_pending_steps, get_latest_plan
    from app.services.execution_manager import execute_step
    from app.services.verifier import verify_step
    from app.services.replanner import (
        ReplanAction,
        replan_from_verification,
    )

    def _emit(ev_type: str, payload: dict) -> None:
        if progress_callback:
            try:
                progress_callback(ev_type, payload)
            except Exception:
                pass

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

            db.commit()  # 进入判定前先落库，保证判定看到的是已提交状态
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
            # 有失败且无可执行步骤：有成功 → partially_completed；全失败 → failed
            if plan_status.get("partial"):
                return {
                    "status": "partially_completed",
                    "message": f"部分步骤完成（{completed} 成功 / {failed} 失败）",
                    "total_steps": total_steps,
                    "completed_steps": completed,
                    "failed_steps": failed,
                    "skipped_steps": skipped,
                    "next_action": "none",
                    "errors": errors,
                }
            return {
                "status": "failed",
                "message": "无可执行步骤，且存在失败步骤",
                "total_steps": total_steps,
                "completed_steps": completed,
                "failed_steps": failed,
                "skipped_steps": skipped,
                "next_action": "none",
                "errors": errors,
            }

        # 2. 逐步骤执行
        waiting_hit: dict | None = None  # A5：本批出现的等待态（不阻塞同批其他分支）
        for step_info in pending:
            step_id = step_info["id"]
            tool_name = step_info.get("tool_name", "")

            # 2a. 执行
            _emit("step_started", {"step_id": step_id, "tool_name": tool_name,
                   "description": step_info.get("description", "")})
            exec_result = execute_step(
                db,
                step_id=step_id,
                plan_id=current_plan.plan_id,
                goal_id=goal.goal_id,
                session_id=session_id,
                project_id=project_id,
            )

            # 2b. 等待态（A5 / P0-6）：Job 或审批 → 记录后继续执行同批其他可执行分支
            step_status = exec_result.get("step_status")
            if step_status in ("waiting_job", "waiting_approval") or (
                exec_result.get("policy") == "REQUIRE_CONFIRMATION"
            ):
                waiting_kind = (
                    step_status
                    if step_status in ("waiting_job", "waiting_approval")
                    else "waiting_approval"
                )
                db.commit()  # 等待态必须持久化，否则新连接看不到
                _emit(
                    LoopEvent.STEP_WAITING.value,
                    {"step_id": step_id, "tool_name": tool_name,
                     "waiting": "job" if waiting_kind == "waiting_job" else "approval"},
                )
                if waiting_hit is None:
                    waiting_hit = {
                        "kind": waiting_kind,
                        "step_id": step_id,
                        "tool_name": tool_name,
                    }
                continue

            # 2c. 验证
            verification = verify_step(
                db,
                step_id=step_id,
                tool_name=tool_name,
                result_data=exec_result.get("result"),
                project_id=project_id,
            )

            # 2c-1. 等待态验证（A6）：pending 表示步骤/Job 尚未终态，按等待处理不触发 Replan
            if verification.overall == "pending":
                db.commit()
                step_status_now = exec_result.get("step_status") or "waiting_job"
                wait_kind = (
                    step_status_now
                    if step_status_now in ("waiting_job", "waiting_approval", "waiting_user")
                    else "waiting_job"
                )
                _emit(
                    LoopEvent.STEP_WAITING.value,
                    {"step_id": step_id, "tool_name": tool_name,
                     "waiting": "job" if wait_kind == "waiting_job" else "approval"},
                )
                if waiting_hit is None:
                    waiting_hit = {
                        "kind": wait_kind,
                        "step_id": step_id,
                        "tool_name": tool_name,
                    }
                continue

            if verification.verified:
                completed += 1
                db.commit()  # A1：状态转换点提交，崩溃时不丢已完成步骤
                _emit("step_completed", {"step_id": step_id, "tool_name": tool_name})
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
                db.commit()  # 重试计数与状态重置需落库
                _emit(
                    LoopEvent.STEP_SKIPPED.value,
                    {"step_id": step_id, "tool_name": tool_name, "reason": "retry"},
                )
                continue  # 步骤已重置为 pending，下次循环重试

            elif decision.action == ReplanAction.REPLAN:
                if decision.updated_plan:
                    current_plan = decision.updated_plan
                    total_steps = len(current_plan.steps)
                db.commit()  # 新 Plan 与其步骤必须落库，否则重启后丢失
                break  # 退出内层 for，用新 Plan 重新开始

            elif decision.action == ReplanAction.WAIT:
                db.commit()
                _emit(
                    LoopEvent.STEP_WAITING.value,
                    {"step_id": step_id, "tool_name": tool_name, "waiting": "job", "reason": decision.reason},
                )
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

            elif decision.action == ReplanAction.WAIT_USER:
                db.commit()
                _emit(
                    LoopEvent.STEP_WAITING.value,
                    {"step_id": step_id, "tool_name": tool_name, "waiting": "user",
                     "reason": decision.reason},
                )
                return {
                    "status": "waiting_user",
                    "message": decision.suggestion or decision.reason,
                    "total_steps": total_steps,
                    "completed_steps": completed,
                    "failed_steps": failed,
                    "skipped_steps": skipped,
                    "next_action": "wait_user",
                    "wait_step_id": step_id,
                    "errors": errors,
                }

            elif decision.action == ReplanAction.SKIP:
                skipped += 1
                db.commit()
                _emit(
                    LoopEvent.STEP_SKIPPED.value,
                    {"step_id": step_id, "tool_name": tool_name, "reason": decision.reason},
                )
                continue

            else:  # FAIL / CANCEL
                _emit("step_failed", {"step_id": step_id, "tool_name": tool_name,
                       "error": exec_result.get("error", "未知错误")})
                failed += 1
                errors.append({
                    "step_id": step_id,
                    "tool_name": tool_name,
                    "error": exec_result.get("error", "未知错误"),
                    "verification": verification.to_dict(),
                })
                db.commit()  # 失败状态必须落库，否则新连接看不到
                continue

        # 2e. 本批有等待态：其他分支已推进完毕，返回等待
        if waiting_hit is not None:
            kind = waiting_hit["kind"]
            is_job = kind == "waiting_job"
            return {
                "status": kind,
                "message": (
                    f"步骤 '{waiting_hit['tool_name']}' 已提交生成任务，等待 Job 完成"
                    if is_job
                    else f"步骤 '{waiting_hit['tool_name']}' 需要人工审批"
                ),
                "total_steps": total_steps,
                "completed_steps": completed,
                "failed_steps": failed,
                "skipped_steps": skipped,
                "next_action": "wait_job" if is_job else "wait_approval",
                "wait_step_id": waiting_hit["step_id"],
                "errors": errors,
            }

        # 如果 Plan 被替换，继续外层循环
        continue

    # 超过最大迭代次数
    db.commit()
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


def run_agent_loop_in_new_session(
    session_factory: Callable[[], Session] | None = None,
    **kwargs: Any,
) -> AgentLoopResult:
    """在自建 Session 中运行 Agent Loop（供后台线程 / 调试接口使用）。

    禁止把 FastAPI 请求作用域的 Session 传入线程：调用方只传参数，
    Session 的创建、提交、关闭都在这里完成。
    """
    if session_factory is None:
        from app.core.database import SessionLocal as session_factory  # noqa: N813

    db = session_factory()
    try:
        return run_agent_loop(db, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 后台执行不允许把异常抛给线程
        db.rollback()
        return AgentLoopResult(
            status="failed",
            message=f"执行异常：{type(exc).__name__}: {exc}",
            errors=[{"error": f"{type(exc).__name__}: {exc}"}],
        )
    finally:
        db.close()


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

    db.commit()  # A1：Runtime 对外入口负责提交
    return {
        "resumed": True,
        "step_id": step_id,
        "verification": verification.to_dict(),
    }


def resume_after_approval(
    db: Session,
    *,
    goal_id: str,
    step_id: str,
    confirmation_id: str,
    project_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """审批完成后恢复 Agent Loop（bridge_approval_resolved 回调）。

    A7 / P0-8：审批按步骤绑定，裁决结果直接读 AgentConfirmation.status，
    不再接收「一次审批放行整个循环」的 approved 布尔值。

    - step.confirmation_id != confirmation_id → {"resumed": False, "error": "confirmation_mismatch"}
    - 审批 approved → step 重置为 pending（保留 requires_approval）
    - 审批 rejected / revision_requested → step=waiting_user, error_code="approval_rejected"
    - 审批仍为 pending → {"resumed": False, "error": "confirmation_pending"}
    - 否则继续推进循环（不传 approved，后续步骤各自按自己的绑定审批判定）

    返回 dict（便于 Phase B 的 Worker 消费）：
    {"resumed": bool, "error": str|None, "status": str, "goal_id", "plan_id", ...}
    """
    from app.models.agent import AgentConfirmation, AgentPlanStep

    step = db.get(AgentPlanStep, step_id)
    if step is None:
        return {"resumed": False, "error": "step_not_found", "goal_id": goal_id}

    if step.confirmation_id != confirmation_id:
        return {
            "resumed": False,
            "error": "confirmation_mismatch",
            "goal_id": goal_id,
            "step_id": step_id,
            "expected_confirmation_id": step.confirmation_id,
            "confirmation_id": confirmation_id,
        }

    confirmation = db.get(AgentConfirmation, confirmation_id)
    if confirmation is None:
        return {"resumed": False, "error": "confirmation_not_found", "goal_id": goal_id}

    if confirmation.status == "pending":
        return {"resumed": False, "error": "confirmation_pending", "goal_id": goal_id}

    if step.status != "waiting_approval":
        # 幂等：已经推进过（或已被其他路径处理），不重复执行
        return {
            "resumed": False,
            "error": "step_not_waiting",
            "goal_id": goal_id,
            "step_id": step_id,
            "step_status": step.status,
        }

    if confirmation.status == "approved":
        step.status = "pending"
        step.error_code = None
        step.error_message = None
    else:  # rejected / revision_requested / expired / cancelled
        step.status = "waiting_user"
        step.error_code = "approval_rejected"
        step.error_message = confirmation.comment or f"审批结果：{confirmation.status}"
    db.flush()

    log_execution(
        db,
        ExecutionRecord(
            goal_id=goal_id,
            step_id=step_id,
            tool_name="bridge_approval_resolved",
            status=f"approval_{confirmation.status}",
        ),
    )
    db.commit()  # A1：审批裁决带来的步骤状态变化先落库

    if confirmation.status != "approved":
        return {
            "resumed": True,
            "error": None,
            "status": "waiting_user",
            "goal_id": goal_id,
            "step_id": step_id,
            "message": f"步骤 {step_id} 审批未通过（{confirmation.status}），等待用户处理",
        }

    from app.services.goal_manager import get_goal_by_id

    goal = get_goal_by_id(db, goal_id)
    if goal is None:
        return {"resumed": False, "error": "goal_not_found", "goal_id": goal_id}

    from app.services.planner import get_latest_plan

    plan = get_latest_plan(db, goal_id)
    if plan is None:
        return {"resumed": False, "error": "plan_not_found", "goal_id": goal_id}

    loop_result = _execute_plan_loop(
        db,
        goal=goal,
        plan=plan,
        session_id=session_id or "",
        project_id=project_id,
    )

    db.commit()  # A1：循环内的写入已按步提交，这里兜底提交收尾状态
    return {
        "resumed": True,
        "error": None,
        "status": loop_result["status"],
        "goal_id": goal_id,
        "plan_id": plan.plan_id,
        "step_id": step_id,
        "message": loop_result["message"],
        "total_steps": loop_result["total_steps"],
        "completed_steps": loop_result["completed_steps"],
        "failed_steps": loop_result["failed_steps"],
        "skipped_steps": loop_result["skipped_steps"],
        "next_action": loop_result["next_action"],
        "wait_step_id": loop_result.get("wait_step_id"),
    }


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
