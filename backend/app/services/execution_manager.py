"""Execution Manager：Agent Runtime 步骤执行编排（Phase 2.2）。

职责：
- Step 执行：PlanStep → 服务端参数注入 → agent_tools.execute_tool → 回写结果
- Tool Selection：根据 Pipeline Stage/权限/前置条件动态筛选可用工具
- Policy Gate：工具审批 / 高风险门禁统一由 execute_tool 内的策略引擎判定
- 执行锁：plan_step 级别的并发控制（防止重复执行）
- 超时 + Retry：每 Step 独立 timeout_seconds + max_retries
- 执行日志：每次执行写 agent_execution_log（started/succeeded/failed）
- Async Job 追踪：Job 创建后通过 job_id 追踪，完成后 bridge 恢复

设计约束：
- 模型只负责选择与解释，工具调用由确定性代码执行
- 工具执行只能走 agent_tools.execute_tool，禁止直接调用注册表里的 fn
  （直接调用会跳过 input_model 校验、preconditions 与错误分类）
- 同步步骤不再创建 Task/TaskStage：AgentPlanStep 是计划状态，
  Task/TaskStage/Job 只在真正产生生产任务（Job）时由 task_orchestrator 关联
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

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


# ── 结果分流（A5 / P0-6）──────────────────────────────────────────────

def _ensure_task_stage_for_job(
    db: Session,
    *,
    step,
    job_id: str,
    session_id: str | None,
    project_id: str | None,
):
    """把步骤产生的 Job 挂成统一 Task 的阶段，让 bridge_job_event 能闭环。

    幂等：同一 job_id 已有 TaskStage 时直接复用，不重复建 Task。
    """
    from sqlalchemy import select

    from app.models.task import TaskStage
    from app.services import task_orchestrator

    existing = db.scalars(select(TaskStage).where(TaskStage.job_id == job_id)).first()
    if existing is not None:
        return existing

    title = (step.description or step.tool_name or "Agent 步骤")[:200]
    task = task_orchestrator.create_task(
        db,
        session_id=session_id,
        task_type="agent_step",
        title=title,
        project_id=project_id,
    )
    return task_orchestrator.attach_job_stage(
        db,
        task,
        job_id=job_id,
        stage_key=(step.step_key or step.tool_name or "agent_step")[:64],
        stage_name=title,
    )


def _route_job_result(
    db: Session,
    *,
    step,
    job_id: str,
    result_payload: dict,
    session_id: str | None,
    project_id: str | None,
) -> str:
    """Job 结果 → 步骤状态。Job 创建成功只代表进入 waiting_job（除非已终态）。

    返回最终写入的 step.status。
    """
    from app.models import Job

    step.job_id = job_id
    _ensure_task_stage_for_job(
        db,
        step=step,
        job_id=job_id,
        session_id=session_id,
        project_id=project_id,
    )

    job = db.get(Job, job_id)
    now = datetime.now()
    if job is None:
        step.status = "failed"
        step.error_code = "job_not_found"
        step.error_message = f"工具返回的 Job {job_id} 不存在"
        step.completed_at = now
        step.result = result_payload
        db.flush()
        return step.status

    if job.status == "succeeded":
        step.status = "succeeded"
        step.completed_at = now
    elif job.status in ("failed", "cancelled"):
        step.status = "failed"
        step.error_code = "job_failed" if job.status == "failed" else "job_cancelled"
        step.error_message = f"Job {job_id} 状态为 {job.status}"
        step.completed_at = now
    else:
        step.status = "waiting_job"
    step.result = result_payload
    db.flush()
    return step.status


def _route_confirmation_result(
    db: Session,
    *,
    step,
    data: dict,
    result_payload: dict,
) -> str | None:
    """工具返回 confirmation_id 时进入 waiting_approval（审批与执行分离）。

    批量工具可能返回 confirmation_ids：全部存入 result，step.confirmation_id 取第一个。
    返回写入的 step.status，未命中返回 None。
    """
    from app.models.agent import AgentConfirmation

    ids: list[str] = []
    raw_ids = data.get("confirmation_ids")
    if isinstance(raw_ids, list):
        ids.extend([str(i) for i in raw_ids if i])
    single = data.get("confirmation_id")
    if single:
        ids.append(str(single))
    if not ids:
        return None

    pending = [
        cid
        for cid in ids
        if (db.get(AgentConfirmation, cid) is not None
            and db.get(AgentConfirmation, cid).status == "pending")
    ]
    if not pending:
        return None

    result_payload["confirmation_ids"] = ids
    step.confirmation_id = ids[0]
    step.status = "waiting_approval"
    step.result = result_payload
    db.flush()
    return step.status


# ── Task 映射 ─────────────────────────────────────────────────────────

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
        # A5(c)：工具需要审批但当前没有可用批准记录 → waiting_approval + approval_required
        _release_step_lock(
            db, step_id, status="waiting_approval", error_code="approval_required"
        )
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

    # 4. 服务端参数注入 + 只走网关执行（A4/P0-5）
    from app.services.agent_tools import execute_tool, inject_server_args

    args = inject_server_args(
        spec,
        step.args,
        session_id=session_id,
        project_id=project_id,
    )

    # 5. 写执行日志（started）
    started_at = datetime.now()
    log_id = log_step_start(
        db,
        goal_id=goal_id or "",
        plan_id=plan_id,
        step_id=step_id,
        tool_name=tool_name,
        input_data=args,
    )

    # 6. 执行（唯一合法入口：agent_tools.execute_tool）
    tool_result = execute_tool(db, tool_name, approved=approved, **args)
    duration_ms = int((datetime.now() - started_at).total_seconds() * 1000)
    if budget:
        budget.record(duration_ms)

    result_payload = {
        "ok": bool(tool_result.ok),
        "data": tool_result.data,
        "error_code": tool_result.error_code,
        "error_message": tool_result.error_message,
        "duration_ms": tool_result.duration_ms,
    }

    if tool_result.ok:
        data = tool_result.data if isinstance(tool_result.data, dict) else {}
        routed_status = None

        # a. 产生 Job → waiting_job（Job 已终态则直接收敛）
        job_id = data.get("job_id")
        if job_id:
            step.result = result_payload
            routed_status = _route_job_result(
                db,
                step=step,
                job_id=str(job_id),
                result_payload=result_payload,
                session_id=session_id,
                project_id=project_id,
            )
        else:
            # b. 产生审批记录 → waiting_approval
            routed_status = _route_confirmation_result(
                db, step=step, data=data, result_payload=result_payload
            )
            if routed_status is None:
                # d. 其他同步工具 → succeeded
                step.status = "succeeded"
                step.completed_at = datetime.now()
                step.result = result_payload
                db.flush()
                routed_status = "succeeded"

        is_waiting = routed_status in ("waiting_job", "waiting_approval")
        log_step_end(
            db,
            goal_id=goal_id or "",
            plan_id=plan_id,
            step_id=step_id,
            tool_name=tool_name,
            result=StepResult.WAITING if is_waiting else StepResult.SUCCESS,
            output_data=result_payload,
            started_at=started_at,
        )
        return {
            "success": routed_status == "succeeded",
            "step_id": step_id,
            "step_status": routed_status,
            "task_id": None,
            "log_id": log_id,
            "result": result_payload,
            "policy": "ALLOW",
            "duration_ms": duration_ms,
            "error": None,
        }

    # 失败分流：审批等待 / 其余一律 failed，错误码原样写入
    if tool_result.error_code == "approval_required":
        _release_step_lock(
            db,
            step_id,
            status="waiting_approval",
            result=result_payload,
            error_code="approval_required",
            error_message=tool_result.error_message,
        )
        log_step_end(
            db,
            goal_id=goal_id or "",
            plan_id=plan_id,
            step_id=step_id,
            tool_name=tool_name,
            result=StepResult.WAITING,
            output_data=result_payload,
            error_code="approval_required",
            error_message=tool_result.error_message,
            started_at=started_at,
        )
        return {
            "success": False,
            "step_id": step_id,
            "task_id": None,
            "log_id": log_id,
            "result": result_payload,
            "policy": "REQUIRE_CONFIRMATION",
            "duration_ms": duration_ms,
            "error": tool_result.error_message or "需要人工审批",
        }

    _release_step_lock(
        db,
        step_id,
        status="failed",
        result=result_payload,
        error_code=tool_result.error_code,
        error_message=tool_result.error_message,
    )
    log_step_end(
        db,
        goal_id=goal_id or "",
        plan_id=plan_id,
        step_id=step_id,
        tool_name=tool_name,
        result=StepResult.FAILED,
        output_data=result_payload,
        error_code=tool_result.error_code,
        error_message=tool_result.error_message,
        started_at=started_at,
    )
    return {
        "success": False,
        "step_id": step_id,
        "task_id": None,
        "log_id": log_id,
        "result": result_payload,
        "policy": "ALLOW",
        "duration_ms": duration_ms,
        "error": tool_result.error_message or tool_result.error_code,
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
