"""Execution Verifier：执行结果验证器（意图识别改造 Phase 3 收尾）。
设计约束（AGENTS.md P0 / 意图识别架构方案 §12）：
- Agent 不能只根据工具返回成功消息就认为任务成功，必须验证真实结果
- 验证规则确定性执行，不调用 LLM，不依赖模型自述
- 生成类执行的真实结果 = 数据库中的 Job 记录：
  1. Job 真实存在（工具返回的 job_id 能在库中查到）
  2. Job 状态合法（queued/running/succeeded 等已知状态集）
  3. Job 归属正确（project_id 与审批参数一致）
- 验证失败返回结构化原因（verified=False + reason_code），调用方决定报错或降级
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job

# Job 已知合法状态（与 models/production.py Job.status 取值一致）
_JOB_VALID_STATUSES = {
    "queued", "running", "succeeded", "failed", "cancelled",
}


@dataclass
class VerificationResult:
    """确定性验证结果：verified=False 时携带原因码与可读信息。"""

    verified: bool
    reason_code: str | None = None
    message: str | None = None
    job_id: str | None = None
    job_status: str | None = None


def verify_job_submission(
    db: Session, result_data: dict | None, *, expected_project_id: str | None = None
) -> VerificationResult:
    """验证生成执行结果：工具返回的 job_id 必须对应库中真实 Job。

    - result_data：run_generate / 返工 / 流程工具返回的 data（含 job_id）
    - expected_project_id：审批参数中的 project_id（归属校验）
    """
    if not isinstance(result_data, dict) or not result_data.get("job_id"):
        return VerificationResult(
            False, "job_id_missing", "执行结果缺少 job_id，无法验证任务已创建"
        )
    job_id = str(result_data["job_id"])
    job = db.get(Job, job_id)
    if job is None:
        return VerificationResult(
            False, "job_not_found", f"工具返回成功但库中不存在任务 {job_id}"
        )
    if job.status not in _JOB_VALID_STATUSES:
        return VerificationResult(
            False,
            "job_invalid_status",
            f"任务 {job_id} 状态非法：{job.status}",
            job_id=job_id,
            job_status=job.status,
        )
    if expected_project_id and job.project_id != expected_project_id:
        return VerificationResult(
            False,
            "job_project_mismatch",
            f"任务 {job_id} 归属项目 {job.project_id} 与预期 {expected_project_id} 不一致",
            job_id=job_id,
            job_status=job.status,
        )
    return VerificationResult(True, job_id=job_id, job_status=job.status)


def find_active_job_by_request_key(db: Session, request_key: str) -> Job | None:
    """按幂等键查询任务（审批执行幂等复用时的辅助查询）。"""
    return db.scalars(
        select(Job).where(Job.request_key == request_key).limit(1)
    ).first()


def verify_execution_result(
    db: Session, result_data: dict | None, *, expected_project_id: str | None = None
) -> VerificationResult:
    """执行结果验证统一入口（execute_approval 端点使用）。

    区分两类执行结果：
    - 异步 Job 类（run_generate / 返工 / 解析 / 合成 / 导出）：返回含 job_id，
      必须验证库中真实 Job（存在/状态合法/项目归属）
    - 同步状态变更类（create_project / save_script / review_assets / confirm_keyframes）：
      直接变更数据库状态后返回，不含 job_id，透传不误报
    """
    if isinstance(result_data, dict) and result_data.get("job_id"):
        return verify_job_submission(db, result_data, expected_project_id=expected_project_id)
    return VerificationResult(True)


# ── 聚合完整性校验（Phase 2） ──────────────────────────────────────


def verify_aggregate_completeness(db: Session, *, task_id: str) -> VerificationResult:
    """聚合完整性校验（数量校验 + 关联关系校验）。

    - 任务必须存在（否则 task_not_found）
    - TaskResult(result_type="aggregate") 必须存在（否则 aggregate_missing）
    - 数量守恒：聚合 total 必须等于库中 TaskStage 实际数量
    - 关联关系：聚合 items/errors/skipped/cancelled 必须与各阶段状态计数一致
    - 终态一致性：任务已闭环（completed/failed）但存在 pending/running 阶段则
      标记 terminal_mismatch，防止前端展示与实际状态不一致
    """
    from sqlalchemy import select, func

    from app.models.task import Task, TaskResult, TaskStage

    task = db.get(Task, task_id)
    if task is None:
        return VerificationResult(False, "task_not_found", f"聚合校验：任务 {task_id} 不存在")
    result = db.scalars(
        select(TaskResult)
        .where(TaskResult.task_id == task_id, TaskResult.result_type == "aggregate")
        .order_by(TaskResult.created_at.desc())
        .limit(1)
    ).first()
    if result is None or not isinstance(result.data, dict):
        return VerificationResult(False, "aggregate_missing", f"任务 {task_id} 缺少聚合结果卡片")
    agg = result.data
    total = db.scalar(
        select(func.count(TaskStage.id)).where(TaskStage.task_id == task_id)
    ) or 0
    if agg.get("total") != total:
        return VerificationResult(
            False,
            "aggregate_count_mismatch",
            f"聚合总数 {agg.get('total')} 与库中阶段数 {total} 不一致",
        )
    counted = (
        sum(1 for s in db.query(TaskStage).filter(TaskStage.task_id == task_id).all()
            if s.status == "succeeded"),
        sum(1 for s in db.query(TaskStage).filter(TaskStage.task_id == task_id).all()
            if s.status == "failed"),
        sum(1 for s in db.query(TaskStage).filter(TaskStage.task_id == task_id).all()
            if s.status == "skipped"),
        sum(1 for s in db.query(TaskStage).filter(TaskStage.task_id == task_id).all()
            if s.status == "cancelled"),
    )
    if (
        agg.get("items") != counted[0]
        or agg.get("errors") != counted[1]
        or agg.get("skipped") != counted[2]
        or agg.get("cancelled") != counted[3]
    ):
        return VerificationResult(
            False,
            "aggregate_status_mismatch",
            f"聚合状态计数与实际不一致：items/errors/skipped/cancelled="
            f"{agg.get('items')}/{agg.get('errors')}/{agg.get('skipped')}/{agg.get('cancelled')} "
            f"实为 {counted[0]}/{counted[1]}/{counted[2]}/{counted[3]}",
        )
    non_terminal = (
        sum(1 for s in db.query(TaskStage).filter(TaskStage.task_id == task_id).all()
            if s.status in ("pending", "running"))
    )
    if task.status in ("completed", "failed", "cancelled") and non_terminal:
        return VerificationResult(
            False,
            "terminal_mismatch",
            f"任务已终态 {task.status} 但仍有 {non_terminal} 个阶段未到终态",
        )
    return VerificationResult(True, message="聚合完整")


def verify_stage_dependencies(db: Session, *, task_id: str) -> VerificationResult:
    """DAG 依赖关系校验：depends_on 引用的阶段必须存在且为前置阶段。

    - 依赖缺失：depends_on 中的 stage_key 在任务内不存在 → dependency_missing
    - 依赖倒置：被依赖阶段 stage_index >= 依赖者 stage_index → dependency_inversion
    - 死锁：存在依赖环（拓扑排序可达性检测）→ dependency_cycle
    """
    from app.models.task import Task, TaskStage

    if db.get(Task, task_id) is None:
        return VerificationResult(False, "task_not_found", f"任务 {task_id} 不存在")
    stages = db.query(TaskStage).filter(TaskStage.task_id == task_id).all()
    if not stages:
        return VerificationResult(True, message="无阶段，依赖校验通过")
    by_key = {s.stage_key: s for s in stages}
    for s in stages:
        for dep_key in (s.depends_on or []):
            dep = by_key.get(dep_key)
            if dep is None:
                return VerificationResult(
                    False, "dependency_missing",
                    f"阶段 {s.stage_key} 依赖 {dep_key} 不存在",
                )
            if dep.stage_index >= s.stage_index:
                return VerificationResult(
                    False, "dependency_inversion",
                    f"阶段 {s.stage_key} 依赖 {dep_key} 为后置阶段（索引倒置）",
                )
    # 环检测：DFS 可达性验证（确定性实现，不依赖拓扑排序库）
    visited: set[str] = set()
    stack: set[str] = set()

    def dfs(key: str) -> bool:
        if key in stack:
            return False
        if key in visited:
            return True
        stack.add(key)
        for dep_key in (by_key[key].depends_on or []):
            if dep_key in by_key and not dfs(dep_key):
                return False
        stack.discard(key)
        visited.add(key)
        return True

    for key in by_key:
        if key not in visited and not dfs(key):
            return VerificationResult(False, "dependency_cycle", f"检测到依赖环（涉及阶段 {key}）")
    return VerificationResult(True, message="依赖关系合法")
