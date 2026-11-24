"""解析结果生产准入门禁：在正式生成角色、场景、道具资产和分镜之前，
对解析结果进行统一准入判断。

三个准入级别：
- BLOCKED：不可进入资产准备，必须修复
- NEEDS_REVIEW：需要人工审核确认
- READY：可以进入资产准备阶段

注意：READY 只表示剧本解析数据可以进入资产准备阶段，
不代表角色、场景、道具图片已经审核通过。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# 准入级别
GATE_BLOCKED = "BLOCKED"
GATE_NEEDS_REVIEW = "NEEDS_REVIEW"
GATE_READY = "READY"


@dataclass
class ParseGateItem:
    """单个门禁判定项的明细。"""

    category: str  # batch / coverage / validation / hash / completeness
    severity: str  # blocker / warning / info
    code: str
    message: str
    detail: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class ParseGateResult:
    """准入判定结果。"""

    level: str  # BLOCKED / NEEDS_REVIEW / READY
    items: list[ParseGateItem] = field(default_factory=list)
    blocked: bool = False
    needs_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "blocked": self.blocked,
            "needs_review": self.needs_review,
            "items": [i.to_dict() for i in self.items],
        }


def evaluate_parse_gate(
    job_result: dict,
    parse_version_diff_summary: dict | None = None,
    script_hash_current: str | None = None,
    script_hash_parse: str | None = None,
    parse_version_completion: dict | None = None,
) -> ParseGateResult:
    """对解析结果进行生产准入判断。

    Args:
        job_result: 解析任务的 job.result
        parse_version_diff_summary: ParseVersion.diff_summary（旧数据兼容来源）
        script_hash_current: 当前剧本版本的 content_hash
        script_hash_parse: ParseVersion.script_hash
        parse_version_completion: ParseVersion 独立完整性列（权威来源，非 None 时优先）

    Returns:
        ParseGateResult: 准入判定结果
    """
    items: list[ParseGateItem] = []
    blockers: list[ParseGateItem] = []
    reviews: list[ParseGateItem] = []

    job_result = job_result or {}
    summary = parse_version_diff_summary or {}
    # 独立列（Schema 迁移后的权威来源）优先，diff_summary 兼容旧数据，job.result 兜底
    completion_cols = parse_version_completion or {}

    def _completion_field(name: str, default=None):
        val = completion_cols.get(name)
        if val is not None:
            return val
        val = summary.get(name)
        if val is not None:
            return val
        return job_result.get(name, default)

    # === 1. 批次完整性检查（P0） ===
    completion = _completion_field("completion_status", "unknown")
    failed_batches = _completion_field("failed_batches", 0) or 0
    failed_ranges = job_result.get("failed_ranges") or []

    if completion == "partial" or failed_batches > 0:
        item = ParseGateItem(
            category="batch",
            severity="blocker",
            code="batch_incomplete",
            message=f"解析存在未完成的批次（失败批次: {failed_batches}，失败区间: {failed_ranges}）",
            detail={"failed_ranges": failed_ranges, "failed_batches": failed_batches},
        )
        blockers.append(item)
    elif completion == "unknown":
        item = ParseGateItem(
            category="batch",
            severity="warning",
            code="batch_status_unknown",
            message="批次完整性状态未知（可能是旧版本解析结果）",
        )
        reviews.append(item)

    # === 2. 剧本版本一致性检查（P0） ===
    if script_hash_current and script_hash_parse:
        if script_hash_current != script_hash_parse:
            item = ParseGateItem(
                category="hash",
                severity="blocker",
                code="script_hash_mismatch",
                message="解析时的剧本版本与当前版本不一致，解析口径可能已失效",
                detail={
                    "current_hash": script_hash_current[:16],
                    "parse_hash": script_hash_parse[:16],
                },
            )
            blockers.append(item)
    elif not script_hash_parse:
        items.append(ParseGateItem(
            category="hash",
            severity="info",
            code="script_hash_missing",
            message="解析版本未记录剧本 hash（旧版本或接口未更新）",
        ))

    # === 3. 覆盖率检查（P0） ===
    coverage = job_result.get("coverage") or {}
    cov_overall = _completion_field("coverage_status") or coverage.get("overall", "unknown")

    if cov_overall == "failed":
        item = ParseGateItem(
            category="coverage",
            severity="blocker",
            code="coverage_failed",
            message="解析覆盖率不达标，存在严重的内容丢失",
            detail=coverage,
        )
        blockers.append(item)
    elif cov_overall == "partial":
        item = ParseGateItem(
            category="coverage",
            severity="warning",
            code="coverage_partial",
            message="解析覆盖率不完整，部分原文内容可能丢失",
            detail=coverage,
        )
        reviews.append(item)

    # 检查对白覆盖率
    dialogues_cov = coverage.get("dialogues", {})
    dlg_missing = dialogues_cov.get("missing", 0)
    dlg_source = dialogues_cov.get("source", 0)
    if dlg_source > 0 and dlg_missing > 0:
        dlg_ratio = dlg_missing / dlg_source
        if dlg_ratio > 0.1:
            item = ParseGateItem(
                category="coverage",
                severity="blocker",
                code="dialogue_coverage_low",
                message=f"对白覆盖率过低：缺少 {dlg_missing}/{dlg_source} 条（{dlg_ratio:.0%}）",
                detail={"missing": dlg_missing, "source": dlg_source, "ratio": round(dlg_ratio, 3)},
            )
            blockers.append(item)
        else:
            items.append(ParseGateItem(
                category="coverage",
                severity="warning",
                code="dialogue_coverage_warn",
                message=f"少量对白缺失：{dlg_missing}/{dlg_source} 条",
                detail={"missing": dlg_missing, "source": dlg_source},
            ))

    # 检查角色覆盖率
    chars_cov = coverage.get("characters", {})
    chars_missing = chars_cov.get("missing", [])
    if chars_missing:
        item = ParseGateItem(
            category="coverage",
            severity="warning",
            code="character_coverage_warn",
            message=f"部分角色未被解析覆盖: {chars_missing[:5]}",
            detail={"missing_characters": chars_missing},
        )
        reviews.append(item)

    # === 4. 解析结果结构校验 ===
    rejected = job_result.get("rejected", False)
    if rejected:
        item = ParseGateItem(
            category="validation",
            severity="blocker",
            code="parse_rejected",
            message="解析结果业务校验被拒绝落库",
            detail={"problems": job_result.get("problems", [])},
        )
        blockers.append(item)

    # === 5. 核心数据完整性 ===
    scenes = job_result.get("scenes") or []
    characters = job_result.get("characters") or []
    episodes = job_result.get("episodes") or []

    if not scenes:
        item = ParseGateItem(
            category="completeness",
            severity="blocker",
            code="no_scenes",
            message="解析结果中没有任何场景数据",
        )
        blockers.append(item)

    if not characters:
        item = ParseGateItem(
            category="completeness",
            severity="warning",
            code="no_characters",
            message="解析结果中没有任何角色数据",
        )
        reviews.append(item)

    if not episodes:
        item = ParseGateItem(
            category="completeness",
            severity="blocker",
            code="no_episodes",
            message="解析结果中没有剧集信息",
        )
        blockers.append(item)

    # === 6. 解析警告与审核问题 ===
    parse_warnings = job_result.get("parse_warnings") or []
    review_issues = job_result.get("review_issues") or []

    if review_issues:
        item = ParseGateItem(
            category="validation",
            severity="warning",
            code="review_issues_present",
            message=f"存在 {len(review_issues)} 个需要人工审核的问题",
            detail={"issues_count": len(review_issues), "sample": review_issues[:3]},
        )
        reviews.append(item)

    # === 判定逻辑 ===
    has_blockers = len(blockers) > 0
    has_reviews = len(reviews) > 0

    items = blockers + reviews + items

    if has_blockers:
        level = GATE_BLOCKED
        blocked = True
        needs_review = True
    elif has_reviews:
        level = GATE_NEEDS_REVIEW
        blocked = False
        needs_review = True
    else:
        level = GATE_READY
        blocked = False
        needs_review = False

    return ParseGateResult(
        level=level,
        items=items,
        blocked=blocked,
        needs_review=needs_review,
    )
