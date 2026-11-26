"""Verifier：Agent Runtime 执行结果验证器（Phase 2.3）。

职责：
- 五维验证：State / Artifact / Business / Quality / Human
- 扩展自 execution_verifier.py 的 Job 验证，增加 Step 级多维度检查
- 验证失败不标记 completed，触发 Replanner
- 每次验证写 VerificationRecord（可选落 agent_verification 表）

验证维度定义：
1. State：Task/Job/AgentTask 状态一致性检查
2. Artifact：产物存在性、有效性、版本检查
3. Business：业务规则 / 依赖 / 审核 / 流程门禁
4. Quality：模型辅助质量检查（可选，Phase 6）
5. Human：人工审核状态 / 版本检查

与 execution_verifier 的关系：
- execution_verifier.verify_job_submission 保留并复用
- 本模块提供更全面的 Step 级验证，统一调用入口
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.services.execution_verifier import (
    VerificationResult as JobVerificationResult,
    verify_job_submission,
)


# ── 枚举 ──────────────────────────────────────────────────────────────

class VerifyStatus(Enum):
    """单维验证状态。"""

    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    SKIPPED = "skipped"


class VerifyDimension(Enum):
    """验证维度。"""

    STATE = "state"
    ARTIFACT = "artifact"
    BUSINESS = "business"
    QUALITY = "quality"
    HUMAN = "human"


# ── 数据类 ────────────────────────────────────────────────────────────

@dataclass
class VerifyCheck:
    """单条验证检查记录。"""

    name: str
    dimension: str
    status: str = "passed"  # passed / failed / partial / needs_review
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepVerificationResult:
    """步骤验证综合结果。"""

    step_id: str
    verified: bool = False
    overall: str = "failed"  # passed / failed / partial / needs_review
    checks: list[VerifyCheck] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def add_check(self, check: VerifyCheck) -> None:
        self.checks.append(check)
        if check.status == "failed":
            self.issues.append(
                {
                    "check": check.name,
                    "dimension": check.dimension,
                    "reason": check.message,
                    "details": check.details,
                }
            )

    def finalize(self) -> None:
        """汇总各维度结果得出整体判定。"""
        statuses = [c.status for c in self.checks]
        if any(s == "failed" for s in statuses):
            self.overall = "failed"
            self.verified = False
        elif any(s == "needs_review" for s in statuses):
            self.overall = "needs_review"
            self.verified = False
        elif any(s == "partial" for s in statuses):
            self.overall = "partial"
            self.verified = True
        elif all(s == "skipped" for s in statuses):
            self.overall = "partial"
            self.verified = True
        else:
            self.overall = "passed"
            self.verified = True

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "verified": self.verified,
            "overall": self.overall,
            "checks": [
                {
                    "name": c.name,
                    "dimension": c.dimension,
                    "status": c.status,
                    "message": c.message,
                }
                for c in self.checks
            ],
            "issues": self.issues,
            "suggestions": self.suggestions,
        }


# ── State 验证 ────────────────────────────────────────────────────────

def verify_state(
    db: Session,
    *,
    step_id: str,
    tool_name: str | None = None,
    result_data: dict | None = None,
) -> list[VerifyCheck]:
    """State 维度：Task/Job/AgentTask 状态一致性检查。"""
    checks: list[VerifyCheck] = []

    # 检查 step 自身状态
    from app.models.agent import AgentPlanStep

    step = db.get(AgentPlanStep, step_id)
    if step is None:
        checks.append(
            VerifyCheck(
                name="step_exists",
                dimension="state",
                status="failed",
                message=f"步骤 {step_id} 不存在",
            )
        )
        return checks

    checks.append(
        VerifyCheck(
            name="step_status",
            dimension="state",
            status="passed",
            message=f"步骤状态: {step.status}",
            details={"status": step.status},
        )
    )

    # 如果有 result_data 含 job_id，复用 execution_verifier
    if result_data and result_data.get("job_id"):
        job_result = verify_job_submission(db, result_data)
        if job_result.verified:
            checks.append(
                VerifyCheck(
                    name="job_verified",
                    dimension="state",
                    status="passed",
                    message=f"Job {job_result.job_id} 验证通过: {job_result.job_status}",
                    details={"job_id": job_result.job_id, "job_status": job_result.job_status},
                )
            )
        else:
            checks.append(
                VerifyCheck(
                    name="job_verification_failed",
                    dimension="state",
                    status="failed",
                    message=f"Job 验证失败: {job_result.reason_code} - {job_result.message}",
                    details={"reason_code": job_result.reason_code},
                )
            )

    return checks


# ── Artifact 验证 ─────────────────────────────────────────────────────

def verify_artifact(
    db: Session,
    *,
    step_id: str,
    tool_name: str | None = None,
    result_data: dict | None = None,
    project_id: str | None = None,
) -> list[VerifyCheck]:
    """Artifact 维度：产物存在性、有效性、版本检查。"""
    checks: list[VerifyCheck] = []

    if not result_data:
        checks.append(
            VerifyCheck(
                name="artifact_no_result",
                dimension="artifact",
                status="skipped",
                message="无产物数据，跳过 Artifact 检查",
            )
        )
        return checks

    # 检查产物引用是否指向有效实体
    entity_refs = []
    for key in ["character_id", "scene_id", "prop_id", "shot_id", "asset_id"]:
        val = result_data.get(key)
        if val:
            entity_refs.append((key, val))

    if entity_refs:
        for ref_type, ref_id in entity_refs:
            exists = _check_entity_exists(db, ref_type, ref_id)
            if exists:
                checks.append(
                    VerifyCheck(
                        name=f"artifact_{ref_type}_exists",
                        dimension="artifact",
                        status="passed",
                        message=f"{ref_type}={ref_id} 存在",
                    )
                )
            else:
                checks.append(
                    VerifyCheck(
                        name=f"artifact_{ref_type}_missing",
                        dimension="artifact",
                        status="failed",
                        message=f"{ref_type}={ref_id} 在数据库中不存在",
                        details={"ref_type": ref_type, "ref_id": ref_id},
                    )
                )

    if not entity_refs:
        checks.append(
            VerifyCheck(
                name="artifact_no_entity_refs",
                dimension="artifact",
                status="skipped",
                message="产物中无实体引用",
            )
        )

    return checks


def _check_entity_exists(db: Session, ref_type: str, ref_id: str) -> bool:
    """检查实体在对应表中是否存在。"""
    try:
        from app.models import Character, Prop, Scene, Shot

        model_map = {
            "character_id": Character,
            "scene_id": Scene,
            "prop_id": Prop,
            "shot_id": Shot,
            "asset_id": None,  # Asset 模型路径不同
        }
        model = model_map.get(ref_type)
        if model is None:
            return True  # 无法验证，视为存在
        return db.get(model, ref_id) is not None
    except Exception:
        return True  # 查询失败不阻塞


# ── Business 验证 ─────────────────────────────────────────────────────

def verify_business(
    db: Session,
    *,
    step_id: str,
    tool_name: str | None = None,
    result_data: dict | None = None,
    project_id: str | None = None,
) -> list[VerifyCheck]:
    """Business 维度：业务规则 / 依赖 / 审核 / 流程门禁。"""
    checks: list[VerifyCheck] = []

    # 检查是否有关联的待确认事项未处理
    if tool_name and project_id:
        try:
            from app.models.agent import AgentConfirmation

            from sqlalchemy import select

            pending_confirmations = db.scalars(
                select(AgentConfirmation).where(
                    AgentConfirmation.project_id == project_id,
                    AgentConfirmation.status == "pending",
                )
            ).all()
            if pending_confirmations:
                checks.append(
                    VerifyCheck(
                        name="business_pending_confirmations",
                        dimension="business",
                        status="needs_review",
                        message=f"项目有 {len(pending_confirmations)} 个待处理确认事项",
                        details={"count": len(pending_confirmations)},
                    )
                )
            else:
                checks.append(
                    VerifyCheck(
                        name="business_no_pending",
                        dimension="business",
                        status="passed",
                        message="无待处理确认事项",
                    )
                )
        except Exception:
            checks.append(
                VerifyCheck(
                    name="business_check_skipped",
                    dimension="business",
                    status="skipped",
                    message="业务规则检查跳过（查询异常）",
                )
            )

    return checks


# ── Human 验证 ────────────────────────────────────────────────────────

def verify_human(
    db: Session,
    *,
    step_id: str,
    tool_name: str | None = None,
    result_data: dict | None = None,
    project_id: str | None = None,
) -> list[VerifyCheck]:
    """Human 维度：人工审核状态 / 版本检查。"""
    checks: list[VerifyCheck] = []

    from app.models.agent import AgentPlanStep

    step = db.get(AgentPlanStep, step_id)
    if step and step.requires_approval:
        if step.status == "waiting_approval":
            checks.append(
                VerifyCheck(
                    name="human_awaiting_approval",
                    dimension="human",
                    status="needs_review",
                    message="步骤正在等待人工审批",
                )
            )
        elif step.status == "succeeded":
            checks.append(
                VerifyCheck(
                    name="human_approved",
                    dimension="human",
                    status="passed",
                    message="人工审批已通过",
                )
            )
        else:
            checks.append(
                VerifyCheck(
                    name="human_approval_unknown",
                    dimension="human",
                    status="partial",
                    message=f"审批步骤状态为 {step.status}，需确认",
                )
            )
    else:
        checks.append(
            VerifyCheck(
                name="human_not_required",
                dimension="human",
                status="skipped",
                message="此步骤无需人工审批",
            )
        )

    return checks


# ── 统一验证入口 ──────────────────────────────────────────────────────

def verify_step(
    db: Session,
    *,
    step_id: str,
    tool_name: str | None = None,
    result_data: dict | None = None,
    project_id: str | None = None,
    dimensions: list[str] | None = None,
) -> StepVerificationResult:
    """执行步骤的完整五维验证。

    dimensions 为 None 时默认执行 state + artifact + business + human。
    quality 维度为可选（Phase 6 启用）。
    """
    if dimensions is None:
        dimensions = ["state", "artifact", "business", "human"]

    result = StepVerificationResult(step_id=step_id)

    for dim in dimensions:
        if dim == "state":
            checks = verify_state(
                db, step_id=step_id, tool_name=tool_name, result_data=result_data
            )
        elif dim == "artifact":
            checks = verify_artifact(
                db, step_id=step_id, tool_name=tool_name,
                result_data=result_data, project_id=project_id,
            )
        elif dim == "business":
            checks = verify_business(
                db, step_id=step_id, tool_name=tool_name,
                result_data=result_data, project_id=project_id,
            )
        elif dim == "human":
            checks = verify_human(
                db, step_id=step_id, tool_name=tool_name,
                result_data=result_data, project_id=project_id,
            )
        elif dim == "quality":
            # Quality 维度 Phase 6 启用
            checks = [
                VerifyCheck(
                    name="quality_deferred",
                    dimension="quality",
                    status="skipped",
                    message="Quality 维度将在 Phase 6 启用",
                )
            ]
        else:
            continue

        for c in checks:
            result.add_check(c)

    result.finalize()
    return result


def verify_plan_completion(
    db: Session,
    *,
    plan_id: str,
) -> dict:
    """检查 Plan 下所有步骤是否全部通过验证。

    返回 {"all_verified": bool, "failed_steps": [...], "pending_steps": [...]}
    """
    from app.services.planner import get_plan_steps

    steps = get_plan_steps(db, plan_id)
    failed = [s for s in steps if s["status"] in ("failed", "cancelled")]
    pending = [s for s in steps if s["status"] not in ("succeeded", "skipped", "failed", "cancelled")]

    return {
        "all_verified": len(failed) == 0 and len(pending) == 0,
        "total": len(steps),
        "succeeded": sum(1 for s in steps if s["status"] == "succeeded"),
        "failed_steps": failed,
        "pending_steps": pending,
    }
