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
