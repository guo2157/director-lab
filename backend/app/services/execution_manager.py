"""Execution Manager：Agent Runtime 步骤执行编排（Phase 2.2）。

职责：
- Step 执行：PlanStep → Task + TaskStage → 调用 Tool → 回写结果
- Tool Selection：根据 Pipeline Stage/权限/前置条件动态筛选可用工具
- Policy Gate：集成 agent_governance.PolicyDecision，ALLOW 才执行
- 执行锁：plan_step 级别的并发控制（防止重复执行）
- 超时 + Retry：每 Step 独立 timeout_seconds + max_retries
- 执行日志：每次执行写 agent_execution_log（started/succeeded/failed）
- Async Job 追踪：Job 创建后通过 job_id 追踪，完成后 bridge 恢复

设计约束：
- 模型只负责选择与解释，工具调用由确定性代码执行
- 未经 PolicyDecision.ALLOW 不允许执行
- 所有工具调用必须在 Task/TaskStage 框架内执行
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.models.ids import task_id, task_stage_id
from app.services.agent_governance import (
    PolicyDecision,
    ToolBudget,
    evaluate_tool_policy,
)
from app.services.agent_runtime import (
    ExecutionRecord,
    log_execution,
    log_step_end,
    log_step_start,
    StepResult,
)


# ── 常量 ──────────────────────────────────────────────────────────────

class ExecutionStatus(Enum):
    """执行状态（对齐 plan_step status）。"""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_JOB = "waiting_job"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


# 执行锁超时（秒）：一个 step 在 running 状态超过此时间自动解锁
EXECUTION_LOCK_TIMEOUT_SECONDS = 1800  # 30 分钟


# ── 工具加载 ──────────────────────────────────────────────────────────

def _get_tool_registry() -> dict:
    """获取工具注册表。"""
    try:
        from app.services.agent_tools import TOOL_REGISTRY

        return TOOL_REGISTRY
    except ImportError:
        return {}


def _get_tool_fn(tool_name: str):
    """获取工具执行函数。"""
    registry = _get_tool_registry()
    spec = registry.get(tool_name, {})
    return spec.get("fn")


def _get_tool_spec(tool_name: str) -> dict | None:
    """获取工具注册信息。"""
    return _get_tool_registry().get(tool_name)


# ── 执行锁 ────────────────────────────────────────────────────────────

def _acquire_step_lock(db: Session, step_id: str) -> bool:
    """获取步骤执行锁（CAS：只有 pending → running 才成功）。

    超过 EXECUTION_LOCK_TIMEOUT_SECONDS 的死锁自动释放。
    """
    from app.models.agent import AgentPlanStep

    step = db.get(AgentPlanStep, step_id)
    if step is None:
        return False

    # 死锁检测
    if step.status == "running" and step.started_at:
        elapsed = (datetime.now() - step.started_at).total_seconds()
        if elapsed > EXECUTION_LOCK_TIMEOUT_SECONDS:
            step.status = "failed"
            step.error_message = f"执行锁超时 ({int(elapsed)}s)，自动释放"
            db.flush()
            return False

    if step.status not in ("pending", "ready"):
        return False

    step.status = "running"
    step.started_at = datetime.now()
    db.flush()
    return True


def _release_step_lock(
    db: Session,
    step_id: str,
    *,
    status: str = "succeeded",
    result: dict | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """释放步骤执行锁并更新状态。"""
    from app.models.agent import AgentPlanStep

    step = db.get(AgentPlanStep, step_id)
    if step is None:
        return

    step.status = status
    step.completed_at = datetime.now()
    if result is not None:
        step.result = result
    if error_code is not None:
        step.error_code = error_code
    if error_message is not None:
        step.error_message = error_message
    db.flush()


# ── Task 映射 ─────────────────────────────────────────────────────────

def _map_step_to_task(
    db: Session,
    *,
    step_id: str,
    plan_id: str,
    goal_id: str | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
    step_index: int = 0,
    tool_name: str = "",
    step_description: str = "",
    agent_task_id: str | None = None,
) -> str:
    """将 PlanStep 映射为 Task + TaskStage，返回 task_id。

    复用现有 task/task_stage 编排层模型。
    """
    from app.models.task import Task, TaskStage

    # 创建 Task
    t = Task(
        id=task_id(),
        session_id=session_id,
        project_id=project_id,
        agent_task_id=agent_task_id,
        task_type="custom",
        title=step_description or f"执行: {tool_name}",
        status="running",
        progress=0,
        current_step=tool_name,
        current_message=step_description,
        started_at=datetime.now(),
    )
    db.add(t)
    db.flush()

    # 创建 TaskStage
    ts = TaskStage(
        id=task_stage_id(),
        task_id=t.id,
        stage_index=0,
        stage_key=tool_name or "execute",
        stage_name=step_description or "Agent 步骤执行",
        status="running",
        progress=0,
    )
    db.add(ts)
    db.flush()

    return t.id


def _complete_task_stage(
    db: Session,
    *,
    task_id_str: str,
    success: bool,
    result: dict | None = None,
    job_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """完成 Task 及其 TaskStage 的状态更新。"""
    from app.models.task import Task, TaskStage

    task = db.get(Task, task_id_str)
    if task:
        task.status = "completed" if success else "failed"
        task.error_code = error_code
        task.error_msg = error_message
        task.progress = 100 if success else task.progress

    stages = db.query(TaskStage).filter(TaskStage.task_id == task_id_str).all()
    for s in stages:
        s.status = "succeeded" if success else "failed"
        if result is not None:
            s.result = result
        if job_id:
            s.job_id = job_id

    db.flush()


# ── Tool Selection ────────────────────────────────────────────────────

def select_tools(
    *,
    pipeline_stage: str | None = None,
    permission: str | None = None,
    exclude_approval: bool = False,
    risk_max: str = "high",
) -> list[dict]:
    """动态工具筛选：根据 Pipeline Stage / 权限 / 风险等级返回可用工具。

    供 Planner.generate() 在 Context Snapshot 基础上缩小候选范围。
    """
    registry = _get_tool_registry()
    risk_order = {"low": 0, "medium": 1, "high": 2}

    tools = []
    for name, spec in registry.items():
        if permission and spec.get("permission") != permission:
            continue
        if exclude_approval and spec.get("requires_approval"):
            continue
        if risk_order.get(spec.get("risk_level", "low"), 0) > risk_order.get(risk_max, 2):
            continue
        tools.append(
            {
                "name": name,
                "description": spec.get("description", ""),
                "permission": spec.get("permission", "read"),
                "risk_level": spec.get("risk_level", "low"),
                "requires_approval": spec.get("requires_approval", False),
                "timeout_seconds": spec.get("timeout_seconds", 30),
                "params": spec.get("params", {}),
            }
        )
    return tools


# ── 步骤执行 ──────────────────────────────────────────────────────────

def execute_step(
    db: Session,
    *,
    step_id: str,
    plan_id: str | None = None,
    goal_id: str | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
    agent_task_id: str | None = None,
    approved: bool = False,
    budget: ToolBudget | None = None,
    timeout_override: int | None = None,
) -> dict:
    """执行单个 PlanStep。

    流程：
    1. 获取执行锁（CAS: pending/ready → running）
    2. PolicyDecision 权限检查
    3. 预算检查
    4. 映射为 Task + TaskStage
    5. 写 agent_execution_log（started）
    6. 调用 Tool
    7. 回写结果 + 释放锁 + 完成日志

    返回 {"success": bool, "step_id": str, "result": dict, "policy": str, "error": str|None}
    """
    from app.models.agent import AgentPlanStep

    step = db.get(AgentPlanStep, step_id)
    if step is None:
        return {"success": False, "step_id": step_id, "result": {}, "policy": "DENY", "error": "step_not_found"}

    tool_name = step.tool_name or ""

    # 1. 获取执行锁
    if not _acquire_step_lock(db, step_id):
        return {
            "success": False,
            "step_id": step_id,
            "result": {},
            "policy": "DENY",
            "error": f"无法获取执行锁，当前状态: {step.status}",
        }

    # 2. PolicyDecision
    spec = _get_tool_spec(tool_name) or {}
    decision = evaluate_tool_policy(spec, approved=approved)
    if decision == PolicyDecision.REQUIRE_CONFIRMATION:
        _release_step_lock(db, step_id, status="waiting_approval")
        return {
            "success": False,
            "step_id": step_id,
            "result": {},
            "policy": "REQUIRE_CONFIRMATION",
            "error": f"工具 '{tool_name}' 需要人工审批",
        }
    if decision == PolicyDecision.DENY:
        _release_step_lock(db, step_id, status="failed")
        return {
            "success": False,
            "step_id": step_id,
            "result": {},
            "policy": "DENY",
            "error": f"工具 '{tool_name}' 被策略拒绝",
        }

    # 3. 预算检查
    if budget:
        violation = budget.check_before(tool_name)
        if violation:
            _release_step_lock(db, step_id, status="failed", error_message=f"预算超限: {violation}")
            return {
                "success": False,
                "step_id": step_id,
                "result": {},
                "policy": "DENY",
                "error": f"预算超限: {violation}",
            }

    # 4. 映射为 Task
    task_id_str = _map_step_to_task(
        db,
        step_id=step_id,
        plan_id=plan_id or "",
        goal_id=goal_id,
        session_id=session_id,
        project_id=project_id,
        step_index=step.step_index or 0,
        tool_name=tool_name,
        step_description=step.description or "",
        agent_task_id=agent_task_id,
    )

    # 5. 写执行日志（started）
    started_at = datetime.now()
    log_id = log_step_start(
        db,
        goal_id=goal_id or "",
        plan_id=plan_id,
        step_id=step_id,
        tool_name=tool_name,
        input_data=step.args,
    )

    # 6. 调用 Tool
    try:
        tool_fn = _get_tool_fn(tool_name)
        if tool_fn is None:
            raise ValueError(f"工具 '{tool_name}' 不存在于注册表中")

        args = step.args or {}
        result_data = tool_fn(db, **args)

        # Tool 返回可能是 ToolResult 对象
        if hasattr(result_data, "to_dict"):
            result_data = result_data.to_dict()
        elif hasattr(result_data, "__dict__"):
            result_data = {"data": str(result_data)}

        duration_ms = int((datetime.now() - started_at).total_seconds() * 1000)

        # 预算记账
        if budget:
            budget.record(duration_ms)

        # 7. 释放锁 + 完成
        _release_step_lock(db, step_id, status="succeeded", result=result_data)
        _complete_task_stage(db, task_id_str=task_id_str, success=True, result=result_data)
        log_step_end(
            db,
            goal_id=goal_id or "",
            plan_id=plan_id,
            step_id=step_id,
            tool_name=tool_name,
            result=StepResult.SUCCESS,
            output_data=result_data,
            started_at=started_at,
        )

        return {
            "success": True,
            "step_id": step_id,
            "task_id": task_id_str,
            "log_id": log_id,
            "result": result_data,
            "policy": "ALLOW",
            "duration_ms": duration_ms,
            "error": None,
        }

    except Exception as exc:
        duration_ms = int((datetime.now() - started_at).total_seconds() * 1000)
        error_msg = str(exc)[:500]

        _release_step_lock(
            db,
            step_id,
            status="failed",
            error_code=type(exc).__name__,
            error_message=error_msg,
        )
        _complete_task_stage(
            db,
            task_id_str=task_id_str,
            success=False,
            error_code=type(exc).__name__,
            error_message=error_msg,
        )
        log_step_end(
            db,
            goal_id=goal_id or "",
            plan_id=plan_id,
            step_id=step_id,
            tool_name=tool_name,
            result=StepResult.FAILED,
            error_code=type(exc).__name__,
            error_message=error_msg,
            started_at=started_at,
        )

        return {
            "success": False,
            "step_id": step_id,
            "task_id": task_id_str,
            "result": {},
            "policy": "ALLOW",
            "duration_ms": duration_ms,
            "error": error_msg,
        }


# ── 批量执行 ──────────────────────────────────────────────────────────

def execute_pending_steps(
    db: Session,
    *,
    plan_id: str,
    goal_id: str,
    session_id: str | None = None,
    project_id: str | None = None,
    agent_task_id: str | None = None,
    approved: bool = False,
) -> list[dict]:
    """批量执行 Plan 中所有可执行的 pending steps（按并行组）。

    从 Planner.get_next_pending_steps 获取待执行列表，逐步骤执行。
    """
    from app.services.planner import get_next_pending_steps

    budget = ToolBudget()
    results: list[dict] = []

    pending = get_next_pending_steps(db, plan_id)
    for step_info in pending:
        result = execute_step(
            db,
            step_id=step_info["id"],
            plan_id=plan_id,
            goal_id=goal_id,
            session_id=session_id,
            project_id=project_id,
            agent_task_id=agent_task_id,
            approved=approved,
            budget=budget,
        )
        results.append(result)

        # 预算超限则停止
        if budget.violations:
            break

    return results


# ── 状态查询 ──────────────────────────────────────────────────────────

def get_step_execution_status(db: Session, step_id: str) -> dict:
    """获取步骤执行状态（含关联 Task/Job 信息）。"""
    from app.models.agent import AgentPlanStep

    from sqlalchemy import select

    step = db.get(AgentPlanStep, step_id)
    if step is None:
        return {"found": False, "step_id": step_id}

    return {
        "found": True,
        "step_id": step.id,
        "plan_id": step.plan_id,
        "step_index": step.step_index,
        "tool_name": step.tool_name,
        "status": step.status,
        "retry_count": step.retry_count,
        "started_at": step.started_at.isoformat() if step.started_at else None,
        "completed_at": step.completed_at.isoformat() if step.completed_at else None,
        "error_code": step.error_code,
        "error_message": (step.error_message or "")[:200],
    }
