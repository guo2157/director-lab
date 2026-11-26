"""Constraint Manager：Agent Runtime 约束管理与合规门禁（Phase 1）。

职责：
- Goal 创建时注入约束（allow_framework/require_human_review 等）
- Plan 执行前按 Constraint spec 检查合规（资源/权限/预算/安全）
- 提供约束检查报告，作为 Planner 的 validate_plan 前置门禁
- 不修改系统状态，只提供判定结果

约束分类：
1. Resource 约束：实体计数上限、存储限额、ComfyUI Worker 并发
2. Permission 约束：工具调用权限、项目归属
3. Budget 约束：Token/时间/成本预算
4. Safety 约束：危险操作拦截（delete/overwrite/format）
5. Pipeline 约束：阶段门禁、依赖完整性
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session


# ── 枚举与常量 ───────────────────────────────────────────────────────

class ConstraintSeverity(Enum):
    """约束违规严重程度。"""

    BLOCK = "block"       # 阻止执行
    WARN = "warn"         # 告警但允许继续
    INFO = "info"         # 提示信息


class CheckResult(Enum):
    """单条约束检查结果。"""

    ALLOWED = "allowed"
    DENIED = "denied"
    WARNING = "warning"
    SKIPPED = "skipped"


# 工具黑名单：明确不可调用的危险工具/操作
DANGEROUS_TOOLS: set[str] = {
    "delete_project",
    "drop_database",
    "format_disk",
    "clear_all_tasks",
}

# 需要人工审批的工具
APPROVAL_REQUIRED_TOOLS: set[str] = {
    "delete_character",
    "delete_scene",
    "delete_prop",
    "delete_shot",
    "overwrite_script",
    "batch_delete_assets",
}


# ── 数据类 ────────────────────────────────────────────────────────────

@dataclass
class ConstraintCheck:
    """单条约束检查记录。"""

    constraint_name: str
    category: str
    result: CheckResult = CheckResult.ALLOWED
    severity: ConstraintSeverity = ConstraintSeverity.INFO
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConstraintReport:
    """约束检查综合报告。"""

    passed: bool = True
    checks: list[ConstraintCheck] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    def add(self, check: ConstraintCheck) -> None:
        self.checks.append(check)
        if check.result == CheckResult.DENIED:
            self.passed = False
            self.blocked_by.append(check.constraint_name)
        elif check.result == CheckResult.WARNING:
            self.warnings.append(check.constraint_name)
        elif check.result == CheckResult.ALLOWED and check.severity == ConstraintSeverity.INFO:
            self.info.append(check.constraint_name)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "blocked_by": self.blocked_by,
            "warnings": self.warnings,
            "info": self.info,
            "checks": [
                {
                    "name": c.constraint_name,
                    "category": c.category,
                    "result": c.result.value,
                    "severity": c.severity.value,
                    "message": c.message,
                }
                for c in self.checks
            ],
        }


@dataclass
class GoalConstraints:
    """目标级约束容器。"""

    allow_framework: set[str] = field(default_factory=set)
    require_human_review: bool = False
    require_role_audit: bool = False
    require_scene_audit: bool = False
    require_prop_audit: bool = False
    require_shot_audit: bool = False
    max_retries_per_step: int = 3
    max_replan_count: int = 3
    token_budget: int | None = None
    time_budget_seconds: int | None = None
    allowed_tools: list[str] | None = None
    blocked_tools: list[str] | None = None

    def to_dict(self) -> dict:
        return {
            "allow_framework": list(self.allow_framework),
            "require_human_review": self.require_human_review,
            "require_role_audit": self.require_role_audit,
            "require_scene_audit": self.require_scene_audit,
            "require_prop_audit": self.require_prop_audit,
            "require_shot_audit": self.require_shot_audit,
            "max_retries_per_step": self.max_retries_per_step,
            "max_replan_count": self.max_replan_count,
            "token_budget": self.token_budget,
            "time_budget_seconds": self.time_budget_seconds,
            "allowed_tools": self.allowed_tools,
            "blocked_tools": self.blocked_tools,
        }

    @classmethod
    def from_goal_constraints_json(
        cls, constraints: dict | None
    ) -> GoalConstraints:
        """从 agent_goal.constraints JSON 字段构建约束对象。"""
        if not constraints:
            return cls()
        return cls(
            allow_framework=set(constraints.get("allow_framework", []) or []),
            require_human_review=bool(constraints.get("require_human_review", False)),
            require_role_audit=bool(constraints.get("require_role_audit", False)),
            require_scene_audit=bool(constraints.get("require_scene_audit", False)),
            require_prop_audit=bool(constraints.get("require_prop_audit", False)),
            require_shot_audit=bool(constraints.get("require_shot_audit", False)),
            max_retries_per_step=int(constraints.get("max_retries_per_step", 3)),
            max_replan_count=int(constraints.get("max_replan_count", 3)),
            token_budget=constraints.get("token_budget"),
            time_budget_seconds=constraints.get("time_budget_seconds"),
            allowed_tools=constraints.get("allowed_tools"),
            blocked_tools=constraints.get("blocked_tools"),
        )

    def merge(self, other: GoalConstraints) -> GoalConstraints:
        """合并约束（other 优先级更高）。"""
        merged = GoalConstraints()
        merged.allow_framework = self.allow_framework | other.allow_framework
        merged.require_human_review = self.require_human_review or other.require_human_review
        merged.require_role_audit = self.require_role_audit or other.require_role_audit
        merged.require_scene_audit = self.require_scene_audit or other.require_scene_audit
        merged.require_prop_audit = self.require_prop_audit or other.require_prop_audit
        merged.require_shot_audit = self.require_shot_audit or other.require_shot_audit
        merged.max_retries_per_step = min(self.max_retries_per_step, other.max_retries_per_step)
        merged.max_replan_count = min(self.max_replan_count, other.max_replan_count)
        merged.token_budget = other.token_budget or self.token_budget
        merged.time_budget_seconds = other.time_budget_seconds or self.time_budget_seconds
        merged.allowed_tools = other.allowed_tools or self.allowed_tools
        merged.blocked_tools = other.blocked_tools or self.blocked_tools
        return merged


# ── Constraint Manager ─────────────────────────────────────────────────

def check_constraints(
    db: Session,
    *,
    goal_constraints: dict | None = None,
    project_id: str | None = None,
    step_tool_name: str | None = None,
    step_args: dict | None = None,
) -> ConstraintReport:
    """执行约束检查（Goal 级 + Project 级 + Step 级）。

    这是 Planner.validate_plan / ExecutionManager.execute 的前置门禁。
    """
    report = ConstraintReport()
    gc = GoalConstraints.from_goal_constraints_json(goal_constraints)

    # ── 1. 危险工具拦截 ──
    if step_tool_name and step_tool_name in DANGEROUS_TOOLS:
        report.add(
            ConstraintCheck(
                constraint_name="dangerous_tool_blocked",
                category="safety",
                result=CheckResult.DENIED,
                severity=ConstraintSeverity.BLOCK,
                message=f"工具 {step_tool_name} 已被系统列入黑名单，不允许通过 Agent Runtime 调用",
                details={"tool_name": step_tool_name},
            )
        )

    # ── 2. 人工审批要求 ──
    if step_tool_name and step_tool_name in APPROVAL_REQUIRED_TOOLS:
        report.add(
            ConstraintCheck(
                constraint_name="approval_required",
                category="permission",
                result=CheckResult.WARNING,
                severity=ConstraintSeverity.WARN,
                message=f"工具 {step_tool_name} 需要人工审批后才能执行",
                details={"tool_name": step_tool_name},
            )
        )

    # ── 3. Goal 级约束：允许框架 ──
    if gc.allow_framework and step_tool_name:
        if step_tool_name not in gc.allow_framework:
            report.add(
                ConstraintCheck(
                    constraint_name="framework_not_allowed",
                    category="permission",
                    result=CheckResult.DENIED,
                    severity=ConstraintSeverity.BLOCK,
                    message=f"工具 {step_tool_name} 不在 Goal 允许的框架列表 {gc.allow_framework} 中",
                    details={"tool_name": step_tool_name, "allowed": list(gc.allow_framework)},
                )
            )

    # ── 4. Goal 级约束：显式黑名单工具 ──
    if gc.blocked_tools and step_tool_name and step_tool_name in gc.blocked_tools:
        report.add(
            ConstraintCheck(
                constraint_name="tool_blocked_by_goal",
                category="permission",
                result=CheckResult.DENIED,
                severity=ConstraintSeverity.BLOCK,
                message=f"工具 {step_tool_name} 被 Goal 级约束明确禁止",
                details={"tool_name": step_tool_name},
            )
        )

    # ── 5. Goal 级约束：审计要求 ──
    if gc.require_role_audit:
        report.add(
            ConstraintCheck(
                constraint_name="role_audit_required",
                category="pipeline",
                result=CheckResult.INFO,
                severity=ConstraintSeverity.INFO,
                message="Goal 要求角色资产需人工审计后才能进入分镜阶段",
            )
        )
    if gc.require_scene_audit:
        report.add(
            ConstraintCheck(
                constraint_name="scene_audit_required",
                category="pipeline",
                result=CheckResult.INFO,
                severity=ConstraintSeverity.INFO,
                message="Goal 要求场景资产需人工审计后才能进入分镜阶段",
            )
        )
    if gc.require_human_review:
        report.add(
            ConstraintCheck(
                constraint_name="human_review_required",
                category="pipeline",
                result=CheckResult.INFO,
                severity=ConstraintSeverity.INFO,
                message="Goal 要求人工审核所有生成产物",
            )
        )

    # ── 6. Project 级资源约束 ──
    if project_id:
        _check_project_resource_limits(db, project_id, report)

    return report


def _check_project_resource_limits(
    db: Session, project_id: str, report: ConstraintReport
) -> None:
    """检查项目级资源上限。"""
    try:
        from sqlalchemy import text

        counts = db.execute(
            text(
                "SELECT "
                "(SELECT COUNT(*) FROM `character` WHERE project_id = :pid) as cc, "
                "(SELECT COUNT(*) FROM scene WHERE project_id = :pid) as sc, "
                "(SELECT COUNT(*) FROM prop WHERE project_id = :pid) as pc, "
                "(SELECT COUNT(*) FROM shot WHERE project_id = :pid) as shc"
            ),
            {"pid": project_id},
        ).first()

        if not counts:
            return

        limits = {
            "character": (int(counts[0] or 0), 200),
            "scene": (int(counts[1] or 0), 100),
            "prop": (int(counts[2] or 0), 500),
            "shot": (int(counts[3] or 0), 2000),
        }

        for entity, (current, limit) in limits.items():
            if current >= limit:
                report.add(
                    ConstraintCheck(
                        constraint_name=f"{entity}_count_limit",
                        category="resource",
                        result=CheckResult.WARNING,
                        severity=ConstraintSeverity.WARN,
                        message=f"{entity} 数量已达上限 {limit}（当前 {current}）",
                        details={"current": current, "limit": limit},
                    )
                )
    except Exception:
        # 资源检查失败不阻塞主流程
        pass


def validate_tool_call(
    *,
    tool_name: str,
    goal_constraints: dict | None = None,
) -> ConstraintReport:
    """工具调用前的快速合规检查（轻量版，无需 DB）。"""
    report = ConstraintReport()

    # 危险工具
    if tool_name in DANGEROUS_TOOLS:
        report.add(
            ConstraintCheck(
                constraint_name="dangerous_tool_blocked",
                category="safety",
                result=CheckResult.DENIED,
                severity=ConstraintSeverity.BLOCK,
                message=f"工具 {tool_name} 已被系统列入黑名单",
                details={"tool_name": tool_name},
            )
        )

    # 审批工具
    if tool_name in APPROVAL_REQUIRED_TOOLS:
        report.add(
            ConstraintCheck(
                constraint_name="approval_required",
                category="permission",
                result=CheckResult.WARNING,
                severity=ConstraintSeverity.WARN,
                message=f"工具 {tool_name} 需要人工审批",
                details={"tool_name": tool_name},
            )
        )

    # Goal 级黑名单
    if goal_constraints:
        blocked = goal_constraints.get("blocked_tools", []) or []
        if tool_name in blocked:
            report.add(
                ConstraintCheck(
                    constraint_name="tool_blocked_by_goal",
                    category="permission",
                    result=CheckResult.DENIED,
                    severity=ConstraintSeverity.BLOCK,
                    message=f"工具 {tool_name} 被 Goal 约束禁止",
                    details={"tool_name": tool_name},
                )
            )

    return report


def build_constraint_report(
    db: Session,
    *,
    goal_constraints: dict | None = None,
    project_id: str | None = None,
    steps: list[dict] | None = None,
) -> ConstraintReport:
    """对计划步骤列表执行批量约束检查。

    供 Planner.validate_plan() 使用。
    """
    report = ConstraintReport()

    # Goal 级检查
    if goal_constraints or project_id:
        goal_report = check_constraints(
            db,
            goal_constraints=goal_constraints,
            project_id=project_id,
        )
        for check in goal_report.checks:
            report.add(check)

    # 逐步骤检查
    if steps:
        for step in steps:
            tool_name = step.get("tool_name") if isinstance(step, dict) else getattr(step, "tool_name", None)
            if tool_name:
                tool_report = validate_tool_call(
                    tool_name=tool_name,
                    goal_constraints=goal_constraints,
                )
                for check in tool_report.checks:
                    report.add(check)

    return report
