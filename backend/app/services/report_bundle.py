"""P1-九：剧本评估三份独立报告聚合层。

将 evaluate_script 原来混在一起的两类内容拆为三个独立报告，
同时保留原有顶层字段以保证三个调用方零改动兼容：

- quality_report（剧本质量）：评分、优点、问题、建议、剧情漏洞
- integrity_report（解析完整性）：复用 CoverageReport 的 episodes/scenes/dialogues/characters/props 对账，
  外加批次统计、completion_status、parse_gate 结果
- feasibility_report（制作可行性）：原 producibility 五维 + production_risks

每份报告统一骨架：summary（human-readable 一句话）、status（passed/partial/failed）、
items（结构化指标对前端表格/指标卡渲染友好）。外层 to_reports_dict 返回 {"reports": {...}}。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_INDEX = "items"


@dataclass
class _ReportBase:
    """报告统一骨架。"""
    status: str = "passed"  # passed / partial / failed
    summary: str = ""
    items: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"status": self.status, "summary": self.summary, "items": self.items}


def _pick(v: Any, default: Any = None) -> Any:
    return v if v is not None else default


def build_quality_report(eval_result: dict) -> dict:
    """从评估结果中提取剧本质量报告（评分/优点/问题/建议/剧情漏洞）。"""
    issues = _pick(eval_result.get("issues"), [])
    score = _pick(eval_result.get("overall_score"), 0)
    # severity 统计
    sev: dict[str, int] = {}
    for i in issues:
        if isinstance(i, dict):
            s = (i.get("severity") or "minor").strip().lower()
            sev[s] = sev.get(s, 0) + 1
    n_crit = sev.get("critical", 0)
    n_maj = sev.get("major", 0)
    n_min = sev.get("minor", 0)
    if n_crit:
        status = "failed"
    elif n_maj > 2 or (score and score < 5):
        status = "partial"
    else:
        status = "passed"
    return {
        "status": status,
        "summary": (
            f"总体评分 {score}/10，发现 {len(issues)} 个问题"
            f"（critical {n_crit} / major {n_maj} / minor {n_min}）"
        ),
        "items": {
            "overall_score": score,
            "issues_count": len(issues),
            "severity": {"critical": n_crit, "major": n_maj, "minor": n_min},
            "strengths": _pick(eval_result.get("strengths"), []),
            "issues": issues,
            "suggestions": _pick(eval_result.get("suggestions"), []),
            "plot_holes": _pick(eval_result.get("plot_holes"), []),
            "summary_text": _pick(eval_result.get("summary"), ""),
        },
    }


def build_feasibility_report(eval_result: dict) -> dict:
    """从评估结果中提取制作可行性报告（可制作性五维 + production_risks）。"""
    risks = _pick(eval_result.get("production_risks"), [])
    n_risk = len(risks)
    # 五维收敛统计
    conv: dict[str, Any] = {}
    for dim in ("character_convergence", "scene_convergence", "shot_feasibility",
                 "dialogue_feasibility", "prop_risks"):
        block = eval_result.get(dim) if isinstance(eval_result.get(dim), dict) else None
        conv[dim] = block or {}
    # 总风险条数
    risk_items: list[dict] = []
    for dim, block in conv.items():
        for sub in ["alias_conflicts", "walk_on_risks", "appearance_volatility",
                     "duplicate_locations", "purity_risks", "oversize_duration",
                     "compound_actions", "abstract_shots", "crowd_or_complex",
                     "long_lines", "rapid_turns", "transient_props",
                     "state_changing_props", "scene_embedded_props"]:
            entries = block.get(sub) if isinstance(block, dict) else None
            if entries and isinstance(entries, list):
                for it in entries:
                    risk_items.append({"dimension": dim, "category": sub, "detail": it})
    n_detail = len(risk_items)
    total = n_risk + n_detail
    if any(r.get("severity") == "critical" for r in risks if isinstance(r, dict)):
        status = "failed"
    elif total > 5:
        status = "partial"
    else:
        status = "passed"
    return {
        "status": status,
        "summary": f"制作风险 {n_risk} 项 + 细节拆解 {n_detail} 项，共 {total} 条",
        "items": {
            "production_risks": risks,
            "dimensions": conv,
            "detail_items": risk_items,
            "total_alerts": total,
            "producibility_missing": _pick(eval_result.get("producibility_missing"), []),
        },
    }


def build_integrity_report(coverage_report: dict, parse_version_meta: dict | None = None,
                            gate_result: dict | None = None) -> dict:
    """聚合解析完整性报告（覆盖对账 + 批次 + Gate 准入）。

    coverage_report: CoverageReport.to_dict() 或 None（空值退化）
    parse_version_meta: 可选 {completion_status, expected_episodes, parsed_episodes,
                      successful_batches, failed_batches}
    gate_result: 可选 ParseGateResult.to_dict()
    """
    cov = coverage_report or {}
    meta = parse_version_meta or {}
    gate = gate_result or {}
    epi = cov.get("episodes") or {}
    # 三个来源拼出最终状态
    missing_eps = len(epi.get("missing") or [])
    failed_batches = meta.get("failed_batches") or 0
    gate_blocked = gate.get("overall") == "BLOCKED"
    if gate_blocked or missing_eps > 0 or failed_batches > 0:
        status = "failed"
    elif cov.get("overall") == "partial" or gate.get("overall") == "NEEDS_REVIEW":
        status = "partial"
    else:
        status = "passed"
    return {
        "status": status,
        "summary": (
            f"集 {epi.get('parsed', 0)}/{epi.get('source', 0)}、"
            f"场景 {cov.get('scenes', {}).get('parsed', 0)}/{cov.get('scenes', {}).get('source', 0)}、"
            f"对白匹配 {cov.get('dialogues', {}).get('matched', 0)}/{cov.get('dialogues', {}).get('source', 0)}、"
            f"角色 {cov.get('characters', {}).get('matched', 0)}/{cov.get('characters', {}).get('source', 0)}"
        ),
        "items": {
            "coverage": cov,
            "completion_status": meta.get("completion_status"),
            "expected_episodes": meta.get("expected_episodes"),
            "parsed_episodes": meta.get("parsed_episodes"),
            "successful_batches": meta.get("successful_batches"),
            "failed_batches": failed_batches,
            "gate": gate,
        },
    }


def to_reports_dict(eval_result: dict, coverage_report: dict | None = None,
                    parse_version_meta: dict | None = None,
                    gate_result: dict | None = None) -> dict:
    """统一聚合入口：返回三份报告 dict，同时保留原有顶层字段。

    评估如果为空（默认关闭，eval_skipped）时返回退化结构：
    - quality_report / feasibility_report 的 items 全部空字典、status=skipped
    - integrity_report 仍可用 coverage + Gate 给出有意义的结论
    """
    if not eval_result:
        report = {
            "quality_report": {"status": "skipped", "summary": "剧本评估阶段默认关闭", "items": {}},
            "feasibility_report": {"status": "skipped", "summary": "剧本评估阶段默认关闭", "items": {}},
            "integrity_report": build_integrity_report(coverage_report,
                                                        parse_version_meta, gate_result),
        }
    else:
        report = {
            "quality_report": build_quality_report(eval_result),
            "feasibility_report": build_feasibility_report(eval_result),
            "integrity_report": build_integrity_report(coverage_report,
                                                        parse_version_meta, gate_result),
        }
    return report
