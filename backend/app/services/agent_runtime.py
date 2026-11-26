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
- 只有 Runtime 对外入口负责提交：run_agent_loop、tick、resume_after_job、resume_after_approval、
  run_agent_loop_in_new_session。
- tick 是幂等的「单次推进」：自建 Session、自管 Goal 锁、每次最多推进
  agent_max_steps_per_tick 步且不超过 agent_tick_timeout_seconds；/chat、Worker、
  调试接口共用它（Phase B B1）。
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
    get_goal_by_id,
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
    # Phase B B6：与 TickResult 对齐的字段（/chat 的 tick 载荷与确定性回复共用）
    lifecycle_state: str = ""
    advanced_steps: list[str] = field(default_factory=list)
    waiting: dict = field(default_factory=dict)
    failed: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)

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
            "lifecycle_state": self.lifecycle_state,
            "advanced_steps": self.advanced_steps,
            "waiting": self.waiting,
            "failed": self.failed,
            "counts": self.counts,
        }


@dataclass
class TickResult:
    """单次 tick 的推进结果（Phase B B1）。

    幂等：对同一 Goal 重复调用不会重复执行已终态步骤；
    一次最多推进 agent_max_steps_per_tick 步，遇等待态立即返回。
    """

    goal_id: str | None = None
    plan_id: str | None = None
    lifecycle_state: str = "unknown"
    advanced_steps: list[str] = field(default_factory=list)
    waiting: dict = field(
        default_factory=lambda: {"job": [], "approval": [], "user": []}
    )
    failed: list[dict] = field(default_factory=list)
    counts: dict = field(
        default_factory=lambda: {"succeeded": 0, "failed": 0, "waiting": 0, "pending": 0}
    )
    message: str = ""
    busy: bool = False
    trigger: str = ""
    # 兼容 AgentLoopResult 的字段（run_agent_loop 映射用）
    status: str = ""
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    skipped_steps: int = 0
    next_action: str | None = None
    wait_step_id: str | None = None
    errors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "goal_id": self.goal_id,
            "plan_id": self.plan_id,
            "lifecycle_state": self.lifecycle_state,
            "advanced_steps": self.advanced_steps,
            "waiting": self.waiting,
            "failed": self.failed,
            "counts": self.counts,
            "message": self.message,
            "busy": self.busy,
            "trigger": self.trigger,
            "status": self.status,
            "total_steps": self.total_steps,
            "completed_steps": self.completed_steps,
            "failed_steps": self.failed_steps,
            "skipped_steps": self.skipped_steps,
            "next_action": self.next_action,
            "wait_step_id": self.wait_step_id,
            "errors": self.errors,
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


def _derive_loop_status(steps: list[dict]) -> str:
    """按步骤真实状态推导 Loop 终态（A10，集中一处便于测试）。

    规则（优先级从上到下）：
    1. 全部 succeeded/skipped → completed
    2. 存在 waiting_user → waiting_user
    3. 存在 waiting_approval → waiting_approval
    4. 存在 waiting_job → waiting_job
    5. 有 pending/ready/running → running（还可继续推进）
    6. 无可执行步骤：有失败且有成功 → partially_completed；全部失败 → failed
    """
    if not steps:
        return "failed"
    statuses = [s.get("status") for s in steps]
    if all(st in ("succeeded", "skipped") for st in statuses):
        return "completed"
    for waiting in ("waiting_user", "waiting_approval", "waiting_job"):
        if waiting in statuses:
            return waiting
    if any(st in ("pending", "ready", "running") for st in statuses):
        return "running"
    if "failed" in statuses or "cancelled" in statuses:
        if any(st == "succeeded" for st in statuses):
            return "partially_completed"
        return "failed"
    return "failed"


def _skip_steps_with_failed_upstream(db: Session, plan_id: str) -> list[str]:
    """把依赖链上游已 failed/cancelled 的 pending 步骤标记为 skipped（A10）。

    返回被跳过的步骤 id 列表。
    """
    from app.models.agent import AgentPlanStep
    from sqlalchemy import select

    rows = db.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id == plan_id)
    ).all()
    by_id = {r.id: r for r in rows}
    dead = {r.id for r in rows if r.status in ("failed", "cancelled")}
    if not dead:
        return []

    changed: list[str] = []
    # 依赖是 DAG，按 step_index 顺序单遍传播即可覆盖整条下游链
    for row in sorted(rows, key=lambda r: r.step_index or 0):
        if row.status not in ("pending", "ready"):
            continue
        deps = row.depends_on or []
        if any(d in dead for d in deps):
            row.status = "skipped"
            row.error_code = "upstream_failed"
            row.error_message = "上游步骤失败，本步骤已跳过"
            dead.add(row.id)
            changed.append(row.id)
    if changed:
        db.flush()
    return changed


def _sync_plan_goal_status(db: Session, goal_id: str, plan_id: str, loop_status: str) -> None:
    """按 Loop 终态同步 plan.status 与 goal.status（A10）。"""
    from app.models.agent import AgentPlan

    plan_row = db.get(AgentPlan, plan_id)
    if plan_row is not None:
        if loop_status == "completed":
            plan_row.status = "completed"
        elif loop_status in ("failed", "partially_completed"):
            plan_row.status = "failed"
    db.flush()

    if loop_status == "completed":
        finalize_goal(db, goal_id)
    elif loop_status == "failed":
        fail_goal(db, goal_id)
    # partially_completed：goal 保持 active（用户可以继续补做）


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
    agent_task_id: str | None = None,
    task_id: str | None = None,
    max_steps: int | None = None,
    deadline: float | None = None,
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

    Phase B B6：/chat 传入 agent_task_id / task_id，把 Goal 绑定到已有 AgentTask 与
    统一 Task（Job 阶段都挂同一个 Task）；max_steps / deadline 用于把单次推进限制在
    agent_max_steps_per_tick 与 agent_tick_timeout_seconds 之内（默认不限，调试接口行为不变）。
    """
    def _emit(ev_type: str, payload: dict) -> None:
        if progress_callback:
            try:
                progress_callback(ev_type, payload)
            except Exception:
                pass  # 回调异常不影响主流程

    result = AgentLoopResult()

    # ── A. Goal 解析 ──
    from app.services.goal_manager import (
        compute_fingerprint,
        derive_target_stage,
    )

    # A10：resumed 判定按指纹——同会话中同指纹的 active 或 completed 目标都算「恢复」
    # （completed 目标会在 get_or_create_goal 中被重新激活，见该函数注释）
    from app.models.agent import AgentGoal as _AgentGoal
    from sqlalchemy import and_ as _and_, select as _select

    _fingerprint = compute_fingerprint(
        project_id=project_id,
        goal_type=goal_type or "user_request",
        target_stage=derive_target_stage(description),
        scope=str((constraints or {}).get("scope") or ""),
    )
    resumed = (
        db.scalars(
            _select(_AgentGoal.id).where(
                _and_(
                    _AgentGoal.session_id == session_id,
                    _AgentGoal.fingerprint == _fingerprint,
                    _AgentGoal.status.in_(("active", "completed")),
                )
            )
        ).first()
        is not None
    )
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
    # B6：把 Goal 绑定到 /chat 已创建的 AgentTask 与统一 Task（幂等：已有绑定不覆盖）
    if agent_task_id or task_id:
        from app.models.agent import AgentGoal as _GoalRow

        _row = db.get(_GoalRow, goal.goal_id)
        if _row is not None:
            if agent_task_id and not _row.agent_task_id:
                _row.agent_task_id = agent_task_id
            if task_id and not _row.task_id:
                _row.task_id = task_id
            goal.agent_task_id = _row.agent_task_id
            goal.task_id = _row.task_id
            db.flush()
    db.commit()  # A1：Goal 创建/复用立即落库，后续崩溃不会丢目标

    # A11：Goal 级锁——同一 Goal 并发调用时只有一个能推进，其余返回 busy 且不执行任何步骤
    from app.services.execution_manager import _acquire_goal_lock, _release_goal_lock

    if not _acquire_goal_lock(db, goal.goal_id):
        db.commit()
        result.status = "busy"
        result.message = f"Goal {goal.goal_id} 正在由其他执行者推进，请稍后重试"
        result.next_action = "retry_later"
        return result

    _emit(
        LoopEvent.GOAL_RESUMED.value if resumed else LoopEvent.GOAL_CREATED.value,
        {"goal_id": goal.goal_id, "description": goal.description},
    )
    try:
        from app.services.agent_events import publish_goal

        publish_goal(
            db,
            goal,
            "agent.goal.updated" if resumed else "agent.goal.created",
        )
        db.commit()
    except Exception:  # noqa: BLE001 - 事件旁路失败不影响主流程
        pass

    try:
        return _run_agent_loop_locked(
            db,
            result=result,
            goal=goal,
            session_id=session_id,
            description=description,
            project_id=project_id,
            resumed=resumed,
            progress_callback=progress_callback,
            emit=_emit,
            max_steps=max_steps,
            deadline=deadline,
        )
    finally:
        _release_goal_lock(db, goal.goal_id)
        try:
            db.commit()
        except Exception:
            db.rollback()


def _run_agent_loop_locked(
    db: Session,
    *,
    result: AgentLoopResult,
    goal: Goal,
    session_id: str,
    description: str,
    project_id: str | None,
    resumed: bool,
    progress_callback: Callable[[str, dict], None] | None,
    emit: Callable[[str, dict], None],
    max_steps: int | None = None,
    deadline: float | None = None,
) -> AgentLoopResult:
    """run_agent_loop 在持有 Goal 锁后的主体（A11 抽出，便于 try/finally 释放锁）。"""
    _emit = emit

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
    # A8/A10：即使上一版计划已全部完成，本次同指纹请求也应当生成新的 Plan revision
    # （用户再次提出同一目标 = 再执行一次），因此这里不再提前短路返回「已完成」。
    # 具体「是否复用旧计划 / 生成新 revision」由 _get_or_create_plan 统一判定。
    # 记录判定结果（供日志/排查），不改变控制流
    completion = check_goal_completion(db, goal.goal_id)

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
    try:
        from app.services.agent_events import publish_plan_created

        publish_plan_created(db, goal, plan)
    except Exception:  # noqa: BLE001 - 事件旁路失败不影响主流程
        pass
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

    # ── F. 执行循环（Phase B B1：与 tick 共用同一推进实现） ──
    tick_result = _tick_locked(
        db,
        goal=goal,
        plan=plan,
        trigger="manual",
        max_steps=max_steps,
        deadline=deadline,
        progress_callback=progress_callback,
    )

    # ── G. 汇总结果（先写全字段，再发最终事件，A3/P0-4） ──
    result.status = tick_result.status
    result.message = tick_result.message
    result.total_steps = tick_result.total_steps
    result.completed_steps = tick_result.completed_steps
    result.failed_steps = tick_result.failed_steps
    result.skipped_steps = tick_result.skipped_steps
    result.next_action = tick_result.next_action
    result.wait_step_id = tick_result.wait_step_id
    result.errors = tick_result.errors
    result.lifecycle_state = tick_result.lifecycle_state
    result.advanced_steps = list(tick_result.advanced_steps or [])
    result.waiting = dict(tick_result.waiting or {})
    result.failed = list(tick_result.failed or [])
    result.counts = dict(tick_result.counts or {})
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
    from app.services.planner import (
        build_and_save_plan,
        get_latest_plan,
        get_latest_plan_any,
    )

    existing = get_latest_plan(db, goal_id)
    previous = None
    if existing:
        if not _plan_all_terminal(db, existing.plan_id):
            return existing
        previous = existing
    else:
        # A10：目标完成后旧 plan.status=completed，get_latest_plan 取不到；
        # 再次执行同一目标时要基于它递增 revision，而不是回到 revision=1。
        latest_any = get_latest_plan_any(db, goal_id)
        if latest_any is not None:
            previous = latest_any

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
    max_steps: int | None = None,
    deadline: float | None = None,
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
            max_steps=max_steps,
            deadline=deadline,
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
    max_steps: int | None = None,
    deadline: float | None = None,
) -> dict:
    """执行 Plan 步骤循环主体。每个步骤的「执行 → 验证 → 重规划」结束后提交一次。

    Phase B B1：max_steps 限制单次推进的步骤数，deadline（time.monotonic 绝对时刻）
    限制墙钟；任一触达即提交当前状态并返回 status="running"（还有可推进步骤）。
    """
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

    import time as _time

    completed = 0
    failed = 0
    skipped = 0
    executed = 0
    errors: list[dict] = []
    iterations = 0
    current_plan = plan
    total_steps = len(plan.steps)

    def _budget_exhausted() -> bool:
        if max_steps is not None and executed >= max_steps:
            return True
        return deadline is not None and _time.monotonic() >= deadline

    while iterations < _MAX_LOOP_ITERATIONS:
        iterations += 1

        # 1. 获取下一步待执行步骤
        pending = get_next_pending_steps(db, current_plan.plan_id)
        if not pending:
            # A10：先把「上游失败导致依赖无法满足」的 pending 标为 skipped(upstream_failed)，
            # 再判定终态——否则会误报成「N 个步骤待执行（可能被阻塞）」。
            skipped_ids = _skip_steps_with_failed_upstream(db, current_plan.plan_id)
            if skipped_ids:
                skipped += len(skipped_ids)
                db.commit()
                pending = get_next_pending_steps(db, current_plan.plan_id)
                if pending:
                    continue  # 跳过后又有可执行步骤（依赖是 skipped 视为满足）

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
            from app.services.planner import get_plan_steps

            all_steps = get_plan_steps(db, current_plan.plan_id)
            loop_status = _derive_loop_status(all_steps)
            if loop_status == "partially_completed":
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
            if loop_status == "failed":
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
            # 理论上不会到这里（有 pending 但依赖不满足 → 已在上一步标 skipped）
            return {
                "status": loop_status,
                "message": f"无可执行步骤（{loop_status}）",
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
            if _budget_exhausted():
                db.commit()
                return {
                    "status": "running",
                    "message": (
                        f"本次推进已达上限（{max_steps} 步或时间上限），"
                        f"剩余步骤将在下一次 tick 继续"
                    ),
                    "total_steps": total_steps,
                    "completed_steps": completed,
                    "failed_steps": failed,
                    "skipped_steps": skipped,
                    "next_action": "pending",
                    "errors": errors,
                }
            step_id = step_info["id"]
            tool_name = step_info.get("tool_name", "")

            # 2a. 执行
            _emit("step_started", {"step_id": step_id, "tool_name": tool_name,
                   "description": step_info.get("description", "")})
            executed += 1
            exec_result = execute_step(
                db,
                step_id=step_id,
                plan_id=current_plan.plan_id,
                goal_id=goal.goal_id,
                goal=goal,
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


# ── Phase B B1：幂等单次推进 tick ─────────────────────────────────────

_PAUSED_LIFECYCLES = ("paused", "cancelled")


def _resume_marked_steps(db: Session, goal: Goal) -> list[str]:
    """处理带 resume_requested_at 的步骤（B1 骨架，B3/B4 细化各 reason 的语义）。

    只处理属于该 Goal 计划链的步骤；处理完一律清除标记（认领语义）。
    返回被处理的 step_id 列表。
    """
    from sqlalchemy import select

    from app.models.agent import AgentPlan, AgentPlanStep

    plan_ids = list(
        db.scalars(select(AgentPlan.id).where(AgentPlan.goal_id == goal.goal_id)).all()
    )
    if not plan_ids:
        return []
    steps = db.scalars(
        select(AgentPlanStep)
        .where(
            AgentPlanStep.plan_id.in_(plan_ids),
            AgentPlanStep.resume_requested_at.isnot(None),
        )
        .order_by(AgentPlanStep.step_index)
    ).all()
    if not steps:
        return []

    handled: list[str] = []
    for step in steps:
        reason = step.resume_reason or "manual"
        # B5：Worker 认领会把 reason 改写为 "claimed:<reason>"，这里剥掉前缀
        if reason.startswith("claimed:"):
            reason = reason[len("claimed:"):] or "manual"
        step.resume_requested_at = None
        step.resume_reason = None
        if reason == "job_terminal":
            _resume_step_after_job(db, step, goal=goal)
        elif reason == "approval_resolved":
            _resume_step_after_approval(db, step, goal=goal)
        elif reason == "sweep":
            # B5 兜底扫描补打的标记：按步骤当前等待态分派（标记来源丢失时也能收敛）
            if step.status == "waiting_job":
                _resume_step_after_job(db, step, goal=goal)
            elif step.status == "waiting_approval":
                _resume_step_after_approval(db, step, goal=goal)
        handled.append(step.id)
    db.flush()
    return handled


def _step_job_ids(step) -> list[str]:
    """步骤关联的 Job id 列表（单 Job 用 job_id，批量工具用 result.job_ids）。"""
    ids: list[str] = []
    if step.job_id:
        ids.append(str(step.job_id))
    result = step.result if isinstance(step.result, dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    for source in (result, data):
        raw = (source or {}).get("job_ids")
        if isinstance(raw, list):
            for jid in raw:
                if jid and str(jid) not in ids:
                    ids.append(str(jid))
    return ids


def _resume_step_after_job(db: Session, step, *, goal=None) -> None:
    """Job 终态后的步骤收敛（B3）。

    - 全部关联 Job 已 succeeded → 用 Verifier 的 state 维度复核（Phase A：Job succeeded 才算通过）
    - 任一 Job failed / cancelled → 步骤 waiting_user，error_code = "job_failed:<code>"，
      不自动重新提交生成（通用约束 4），由用户决定
    - 仍有 queued / running → 保持 waiting_job（标记已清除，等待下一次终态事件或兜底扫描）
    """
    from app.models import Job

    if step.status != "waiting_job":
        return
    job_ids = _step_job_ids(step)
    if not job_ids:
        step.status = "failed"
        step.error_code = "job_not_found"
        step.error_message = "恢复时找不到步骤关联的 Job"
        step.completed_at = datetime.now()
        db.flush()
        return

    jobs = [db.get(Job, jid) for jid in job_ids]
    if any(j is None for j in jobs):
        step.status = "failed"
        step.error_code = "job_not_found"
        step.error_message = "恢复时找不到步骤关联的 Job"
        step.completed_at = datetime.now()
        db.flush()
        return

    # 多 Job（批量）：全部终态才判定
    if any(j.status not in ("succeeded", "failed", "cancelled") for j in jobs):
        step.status = "waiting_job"
        db.flush()
        return

    failed_jobs = [j for j in jobs if j.status != "succeeded"]
    result = dict(step.result or {})
    if failed_jobs:
        first = failed_jobs[0]
        result["failed_job_ids"] = [j.id for j in failed_jobs]
        step.result = result
        step.status = "waiting_user"
        step.error_code = f"job_failed:{first.error_code or first.status}"
        step.error_message = first.error_msg or f"Job {first.id} 状态为 {first.status}"
        step.completed_at = datetime.now()
        step.lock_owner = None
        db.flush()
        return

    # 全部 succeeded：先落 succeeded，再用 state 维度复核 Job 归属与终态
    step.status = "succeeded"
    step.completed_at = datetime.now()
    step.error_code = None
    step.error_message = None
    db.flush()

    from app.services.verifier import verify_step

    verification = verify_step(
        db,
        step_id=step.id,
        project_id=getattr(goal, "project_id", None),
        dimensions=["state"],
    )
    if not verification.verified:
        issue = verification.issues[0] if verification.issues else {}
        step.status = "failed"
        step.error_code = "verification_failed"
        step.error_message = (issue.get("reason") or "Job 终态复核未通过")[:1000]
        step.completed_at = datetime.now()
    db.flush()


# 申请类工具：审批通过后由 Runtime 调 run_generate 自动建 Job（幂等）
_APPROVAL_REQUEST_TOOLS = ("request_generate", "batch_request_generate")


def _resume_step_after_approval(db: Session, step, *, goal=None) -> None:
    """审批裁决后的步骤收敛（B4）。

    - 仍是 pending（标记来源异常）→ 清标记保持等待
    - rejected / revision_requested → waiting_user，error_code="approval_rejected"，带审批意见
    - approved 且是申请类工具 → 调 run_generate(approved=True, confirmation_id=...) 建 Job
      （run_generate 幂等：用户已在页面点过执行会返回同一个 job_id）→ waiting_job
    - approved 且工具本身需要审批 → 交给 _execute_plan_loop 以 approved 重新执行
    """
    from app.models.agent import AgentConfirmation

    if step.status != "waiting_approval":
        return
    confirmation = (
        db.get(AgentConfirmation, step.confirmation_id) if step.confirmation_id else None
    )
    if confirmation is None or confirmation.status == "pending":
        step.status = "waiting_approval"
        db.flush()
        return

    if confirmation.status != "approved":
        step.status = "waiting_user"
        step.error_code = "approval_rejected"
        step.error_message = confirmation.comment or f"审批结果：{confirmation.status}"
        step.completed_at = datetime.now()
        step.lock_owner = None
        db.flush()
        return

    if (step.tool_name or "") not in _APPROVAL_REQUEST_TOOLS:
        # 工具本身需要审批：清空绑定前的执行痕迹，回到 pending 由循环以 approved 执行
        step.status = "pending"
        step.error_code = None
        step.error_message = None
        step.lock_owner = None
        db.flush()
        return

    _run_generate_after_approval(db, step=step, confirmation=confirmation, goal=goal)
    db.flush()


def _run_generate_after_approval(db: Session, *, step, confirmation, goal=None) -> None:
    """申请类工具获批后自动执行：run_generate → 写 job_id → 挂统一 Task → waiting_job。"""
    from app.services.agent_tools import execute_tool
    from app.services.execution_manager import _ensure_task_stage_for_job

    tool_result = execute_tool(
        db, "run_generate", approved=True, confirmation_id=confirmation.id
    )
    data = tool_result.data if isinstance(tool_result.data, dict) else {}
    result = {
        "ok": bool(tool_result.ok),
        "data": tool_result.data,
        "error_code": tool_result.error_code,
        "error_message": tool_result.error_message,
        "executed_confirmation_id": confirmation.id,
    }
    step.result = result
    step.lock_owner = None

    if not tool_result.ok:
        step.status = "waiting_user"
        step.error_code = f"approval_execute_failed:{tool_result.error_code or 'unknown'}"
        step.error_message = (tool_result.error_message or "审批后执行失败")[:1000]
        step.completed_at = datetime.now()
        return

    job_id = data.get("job_id")
    if not job_id:
        # run_generate 没返回 Job：无法继续，交给用户
        step.status = "waiting_user"
        step.error_code = "job_not_created"
        step.error_message = "审批已通过，但执行工具未返回 Job"
        step.completed_at = datetime.now()
        return

    step.job_id = str(job_id)
    _ensure_task_stage_for_job(
        db,
        step=step,
        job_id=str(job_id),
        session_id=getattr(goal, "session_id", None),
        project_id=getattr(goal, "project_id", None),
        goal=goal,
    )

    from app.models import Job

    job = db.get(Job, str(job_id))
    now = datetime.now()
    if job is not None and job.status == "succeeded":
        step.status = "succeeded"
        step.error_code = None
        step.error_message = None
        step.completed_at = now
    elif job is not None and job.status in ("failed", "cancelled"):
        step.status = "waiting_user"
        step.error_code = f"job_failed:{job.error_code or job.status}"
        step.error_message = job.error_msg or f"Job {job.id} 状态为 {job.status}"
        step.completed_at = now
    else:
        step.status = "waiting_job"
        step.error_code = None
        step.error_message = None


def _sync_agent_task_lifecycle(db: Session, goal: Goal, lifecycle: str) -> None:
    """把生命周期同步到 Goal 关联的 AgentTask（经 parent_agent 状态机，不裸赋值 status）。"""
    from app.models.agent import AgentTask
    from app.services import parent_agent

    task_id = getattr(goal, "agent_task_id", None)
    if not task_id:
        return
    task = db.get(AgentTask, task_id)
    if task is None:
        return
    task.lifecycle_state = lifecycle
    if lifecycle == "completed":
        parent_agent.complete_task(db, task, "Agent Runtime 目标完成")
    elif lifecycle == "failed":
        parent_agent.fail_task(db, task, "Agent Runtime 目标失败")
    elif lifecycle == "cancelled":
        parent_agent.cancel_task(db, task, "Agent Runtime 目标已取消")
    elif lifecycle == "waiting_approval":
        parent_agent.transition_task(db, task, "waiting_approval", result_summary="等待人工审批")
    elif lifecycle == "running" and task.status in ("pending", "waiting_approval"):
        parent_agent.transition_task(db, task, "running", result_summary="Agent Runtime 推进中")
    db.flush()


def _tick_locked(
    db: Session,
    *,
    goal: Goal,
    plan: Any | None,
    trigger: str,
    max_steps: int | None,
    deadline: float | None,
    progress_callback: Callable[[str, dict], None] | None = None,
) -> TickResult:
    """tick 主体（调用方必须已持有 Goal 锁）。

    run_agent_loop（调试接口 / 同步调用）与 tick（chat / Worker）共用同一段推进逻辑，
    保证「执行 → 验证 → 重规划 → 生命周期」只有一份实现。
    """
    from app.models.agent import AgentGoal
    from app.services.planner import get_plan_steps

    result = TickResult(goal_id=goal.goal_id, trigger=trigger)

    if plan is None:
        result.lifecycle_state = "planning"
        result.status = "planning"
        result.message = "目标还没有计划，等待 Planner 生成"
        db.flush()
        return result

    from app.services.agent_events import publish_step, publish_tick_finished
    from app.services.planner import get_plan_steps as _get_steps

    result.plan_id = plan.plan_id

    # B2：状态确实变化才发 agent.step.updated（执行前快照，执行后比对）。
    # 快照必须早于恢复标记处理，否则 Job / 审批恢复造成的状态变化不会发事件。
    status_before = {s["id"]: s["status"] for s in _get_steps(db, plan.plan_id)}

    _resume_marked_steps(db, goal)
    db.flush()

    loop_result = _execute_plan_loop(
        db,
        goal=goal,
        plan=plan,
        session_id=goal.session_id,
        project_id=goal.project_id,
        progress_callback=progress_callback,
        max_steps=max_steps,
        deadline=deadline,
    )

    result.status = loop_result["status"]
    result.message = loop_result["message"]
    result.total_steps = loop_result["total_steps"]
    result.completed_steps = loop_result["completed_steps"]
    result.failed_steps = loop_result["failed_steps"]
    result.skipped_steps = loop_result["skipped_steps"]
    result.next_action = loop_result["next_action"]
    result.wait_step_id = loop_result.get("wait_step_id")
    result.errors = loop_result["errors"]

    _sync_plan_goal_status(db, goal.goal_id, plan.plan_id, result.status)

    steps = get_plan_steps(db, plan.plan_id)
    lifecycle = _derive_loop_status(steps)
    if result.status == "running":
        lifecycle = "running"
    result.lifecycle_state = lifecycle

    row = db.get(AgentGoal, goal.goal_id)
    if row is not None:
        row.lifecycle_state = lifecycle
        row.last_tick_at = datetime.now()
    _sync_agent_task_lifecycle(db, goal, lifecycle)

    waiting: dict = {"job": [], "approval": [], "user": []}
    failed_items: list[dict] = []
    counts = {"succeeded": 0, "failed": 0, "waiting": 0, "pending": 0}
    for s in steps:
        status = s["status"]
        if status == "waiting_job":
            waiting["job"].append(s["id"])
        elif status == "waiting_approval":
            waiting["approval"].append(
                {"step_id": s["id"], "confirmation_id": s.get("confirmation_id")}
            )
        elif status == "waiting_user":
            waiting["user"].append(
                {
                    "step_id": s["id"],
                    "error_code": s.get("error_code"),
                    "message": s.get("error_message"),
                }
            )
        if status == "succeeded":
            counts["succeeded"] += 1
        elif status == "failed":
            counts["failed"] += 1
            failed_items.append(
                {
                    "step_id": s["id"],
                    "tool_name": s.get("tool_name"),
                    "error_code": s.get("error_code"),
                    "message": s.get("error_message"),
                }
            )
        elif status.startswith("waiting"):
            counts["waiting"] += 1
        elif status in ("pending", "ready", "running"):
            counts["pending"] += 1
    result.waiting = waiting
    result.failed = failed_items
    result.counts = counts

    for s in steps:
        before = status_before.get(s["id"])
        if before != s["status"]:
            from app.models.agent import AgentPlanStep

            row = db.get(AgentPlanStep, s["id"])
            if row is not None:
                publish_step(db, goal, row)

    publish_tick_finished(db, goal, result)
    db.flush()
    return result


def tick(
    session_factory: Callable[[], Session] | None = None,
    goal_id: str | None = None,
    *,
    trigger: str = "manual",
    progress_callback: Callable[[str, dict], None] | None = None,
    db: Session | None = None,
) -> TickResult:
    """幂等单次推进：抢 Goal 锁、认领恢复标记、最多推进 N 步。

    trigger 取值：chat / worker_resume / worker_sweep / debug / manual（写入执行日志）。
    db：调用方已有 Session 时传入（/chat 同步推进）；为 None 时自建并在结束时关闭。
    """
    from app.core.config import get_settings
    from app.services.execution_manager import _acquire_goal_lock, _release_goal_lock
    from app.services.goal_manager import get_goal_by_id
    from app.services.planner import get_latest_plan, get_latest_plan_any

    if session_factory is None:
        from app.core.database import SessionLocal as session_factory  # noqa: N813

    result = TickResult(goal_id=goal_id, trigger=trigger)
    if not goal_id:
        result.lifecycle_state = "failed"
        result.status = "failed"
        result.message = "缺少 goal_id"
        return result

    settings = get_settings()
    max_steps = int(getattr(settings, "agent_max_steps_per_tick", 10) or 10)
    timeout_seconds = float(getattr(settings, "agent_tick_timeout_seconds", 60.0) or 60.0)

    import time as _time

    deadline = _time.monotonic() + timeout_seconds
    own_session = db is None
    if db is None:
        db = session_factory()
    lock_held = False
    try:
        goal = get_goal_by_id(db, goal_id)
        if goal is None:
            result.lifecycle_state = "unknown"
            result.status = "failed"
            result.message = f"Goal {goal_id} 不存在"
            return result

        if goal.status != "active" or goal.lifecycle_state in _PAUSED_LIFECYCLES:
            result.lifecycle_state = goal.lifecycle_state or goal.status
            result.status = goal.lifecycle_state or goal.status
            result.message = f"目标当前状态为 {result.status}，不推进"
            return result

        if not _acquire_goal_lock(db, goal_id):
            db.commit()
            result.busy = True
            result.lifecycle_state = goal.lifecycle_state or "running"
            result.status = "busy"
            result.message = f"Goal {goal_id} 正在由其他执行者推进"
            return result
        lock_held = True

        log_execution(
            db,
            ExecutionRecord(
                goal_id=goal_id,
                status=f"tick_{trigger}",
                input_data={"trigger": trigger},
            ),
        )

        plan = get_latest_plan(db, goal_id) or get_latest_plan_any(db, goal_id)
        result = _tick_locked(
            db,
            goal=goal,
            plan=plan,
            trigger=trigger,
            max_steps=max_steps,
            deadline=deadline,
            progress_callback=progress_callback,
        )
        db.commit()
        return result
    except Exception as exc:  # noqa: BLE001 - tick 不向调用方抛异常
        db.rollback()
        error_code = type(exc).__name__
        error_message = (str(exc) or error_code)[:1000]
        try:
            log_execution(
                db,
                ExecutionRecord(
                    goal_id=goal_id,
                    status="tick_error",
                    error_code=error_code,
                    error_message=error_message,
                ),
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        result.lifecycle_state = "failed"
        result.status = "failed"
        result.message = f"tick 异常：{error_code}: {error_message}"
        result.errors = [{"error": f"{error_code}: {error_message}"}]
        return result
    finally:
        if lock_held:
            try:
                _release_goal_lock(db, goal_id)
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
        if own_session:
            db.close()


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

def _goal_payload(goal: Goal, loop_status: str) -> dict:
    """Goal 对外载荷（A10：partially_completed 通过 loop_status 展示，goal.status 保持 active）。"""
    payload = goal.to_dict()
    payload["loop_status"] = loop_status
    return payload


def get_goal_status_by_id(db: Session, goal_id: str, *, log_limit: int = 20) -> dict:
    """按 goal_id 查询目标执行状态（A10 / P0-12）。

    返回：
    {
      "has_active_goal": bool,        # 兼容旧调用方
      "goal": {..., "loop_status": ...},
      "completion": {...},
      "latest_plan": {"plan_id", "revision", "status", "parent_plan_id"},
      "steps": [{"id", "step_index", "step_key", "tool_name", "status", "job_id",
                 "confirmation_id", "error_code", "error_message"}],
      "loop_status": "completed|failed|partially_completed|waiting_*|running|unknown",
      "execution_logs": [...]
    }
    """
    from app.models.agent import AgentPlan

    goal = get_goal_by_id(db, goal_id)
    if goal is None:
        return {"has_active_goal": False, "goal_id": goal_id, "loop_status": "unknown"}

    from sqlalchemy import select

    from app.services.planner import get_plan_steps

    plan_row = db.scalars(
        select(AgentPlan)
        .where(AgentPlan.goal_id == goal_id)
        .order_by(AgentPlan.revision.desc())
        .limit(1)
    ).first()

    plan_info: dict[str, Any] | None = None
    steps: list[dict] = []
    loop_status = "unknown"
    if plan_row is not None:
        plan_info = {
            "plan_id": plan_row.id,
            "revision": plan_row.revision,
            "status": plan_row.status,
            "parent_plan_id": getattr(plan_row, "parent_plan_id", None),
        }
        all_steps = get_plan_steps(db, plan_row.id)
        steps = [
            {
                "id": s["id"],
                "step_index": s["step_index"],
                "step_key": s.get("step_key"),
                "tool_name": s["tool_name"],
                "status": s["status"],
                "requires_approval": s.get("requires_approval"),
                "job_id": s.get("job_id"),
                "confirmation_id": s.get("confirmation_id"),
                "error_code": s.get("error_code"),
                "error_message": (s.get("error_message") or "")[:200],
            }
            for s in all_steps
        ]
        loop_status = _derive_loop_status(all_steps)

    return {
        "has_active_goal": goal.status == "active",
        "goal_id": goal_id,
        "goal": _goal_payload(goal, loop_status),
        "completion": check_goal_completion(db, goal_id),
        "latest_plan": plan_info,
        "steps": steps,
        "loop_status": loop_status,
        "execution_logs": get_execution_logs(db, goal_id=goal_id, limit=log_limit),
    }


def get_goal_status(db: Session, session_id: str) -> dict:
    """获取当前会话的目标执行状态（保留旧签名，内部转调 get_goal_status_by_id）。"""
    goal = get_active_goal(db, session_id)
    if goal is None:
        return {"has_active_goal": False, "loop_status": "unknown"}
    return get_goal_status_by_id(db, goal.goal_id)


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
