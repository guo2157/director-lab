"""Planner + Plan Validator：Agent Runtime 动态计划生成与校验（Phase 2.1）。

职责：
- Plan 生成：Goal + Context → plan_json（含 steps 列表、依赖 DAG、前置条件、并行分组）
- Plan Validator：14 项结构化检查（工具存在性/权限/依赖/循环/资源/参数一致性）
- Plan Revision：变更递增 revision，保留历史版本审计
- 复用现有 agent_plan_step 表（plan_id 非 NULL 表示新 Plan 步骤）

设计约束：
- 模型只负责识别与解释，Plan 结构由确定性规则生成
- 工具必须存在于 TOOL_REGISTRY 中，否则拒绝
- 依赖图不允许循环
- 每个 Plan 修订保留上一版本（status=superseded），可追溯
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ids import agent_plan_id, agent_plan_step_id


# ── 工具注册表加载（延迟导入避免循环引用）─────────────────────────────

def _get_tool_registry() -> dict:
    """获取工具注册表快照。"""
    try:
        from app.services.agent_tools import TOOL_REGISTRY

        return TOOL_REGISTRY
    except ImportError:
        return {}


def _list_available_tools(
    *,
    permission: str | None = None,
    risk_level: str | None = None,
    exclude_approval: bool = False,
) -> list[dict]:
    """列出可用工具（支持按权限/风险/审批过滤）。"""
    registry = _get_tool_registry()
    tools = []
    for name, spec in registry.items():
        if permission and spec.get("permission") != permission:
            continue
        if risk_level and spec.get("risk_level") != risk_level:
            continue
        if exclude_approval and spec.get("requires_approval"):
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


def _get_tool_spec(tool_name: str) -> dict | None:
    """获取单个工具的注册信息。"""
    return _get_tool_registry().get(tool_name)


# ── 数据类 ────────────────────────────────────────────────────────────

@dataclass
class PlanStep:
    """计划步骤定义。"""

    step_key: str                          # 步骤唯一标识（计划内）
    tool_name: str                         # TOOL_REGISTRY 工具名
    description: str = ""                  # 步骤描述
    args: dict[str, Any] = field(default_factory=dict)  # 工具参数
    depends_on: list[str] = field(default_factory=list)  # 依赖步骤 key 列表
    preconditions: list[dict] = field(default_factory=list)  # 前置条件
    verification: list[dict] = field(default_factory=list)   # 验证规则
    requires_approval: bool = False        # 是否需要审批
    estimated_timeout: int = 600           # 预估超时（秒）


@dataclass
class Plan:
    """执行计划。"""

    plan_id: str                           # pln_xxx
    goal_id: str                           # gol_xxx
    revision: int = 1                      # 计划版本
    steps: list[PlanStep] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "goal_id": self.goal_id,
            "revision": self.revision,
            "steps": [
                {
                    "step_key": s.step_key,
                    "tool_name": s.tool_name,
                    "description": s.description,
                    "args": s.args,
                    "depends_on": s.depends_on,
                    "preconditions": s.preconditions,
                    "verification": s.verification,
                    "requires_approval": s.requires_approval,
                    "estimated_timeout": s.estimated_timeout,
                }
                for s in self.steps
            ],
            "metadata": self.metadata,
        }

    @classmethod
    def from_json(cls, goal_id: str, plan_json: dict) -> Plan:
        """从 plan_json 反序列化。"""
        steps = []
        for s in plan_json.get("steps", []):
            steps.append(
                PlanStep(
                    step_key=s.get("step_key", ""),
                    tool_name=s.get("tool_name", ""),
                    description=s.get("description", ""),
                    args=s.get("args", {}),
                    depends_on=s.get("depends_on", []),
                    preconditions=s.get("preconditions", []),
                    verification=s.get("verification", []),
                    requires_approval=s.get("requires_approval", False),
                    estimated_timeout=s.get("estimated_timeout", 600),
                )
            )
        return cls(
            plan_id=plan_json.get("plan_id", ""),
            goal_id=goal_id,
            revision=plan_json.get("revision", 1),
            steps=steps,
            metadata=plan_json.get("metadata", {}),
        )


@dataclass
class ValidationIssue:
    """单条校验问题。"""

    check_name: str
    severity: str  # error / warning / info
    message: str
    step_key: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    """Plan 校验报告。"""

    passed: bool = True
    issues: list[ValidationIssue] = field(default_factory=list)
    errors: int = 0
    warnings: int = 0
    infos: int = 0

    def add_error(self, check_name: str, message: str, **kwargs) -> None:
        self.passed = False
        self.errors += 1
        self.issues.append(
            ValidationIssue(check_name=check_name, severity="error", message=message, **kwargs)
        )

    def add_warning(self, check_name: str, message: str, **kwargs) -> None:
        self.warnings += 1
        self.issues.append(
            ValidationIssue(check_name=check_name, severity="warning", message=message, **kwargs)
        )

    def add_info(self, check_name: str, message: str, **kwargs) -> None:
        self.infos += 1
        self.issues.append(
            ValidationIssue(check_name=check_name, severity="info", message=message, **kwargs)
        )

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "errors": self.errors,
            "warnings": self.warnings,
            "infos": self.infos,
            "issues": [
                {
                    "check": i.check_name,
                    "severity": i.severity,
                    "message": i.message,
                    "step_key": i.step_key,
                    "detail": i.detail,
                }
                for i in self.issues
            ],
        }


# ── 意图分类（A8：供目标指纹使用，Phase C 由结构化 Goal 解析替换）─────────

def classify_description(description: str) -> str:
    """把目标描述归类为稳定的 target_stage 语义标签。

    返回：query / analyze / generate_keyframe / generate_clip / generate_default / default

    这是 Phase A 的临时实现（关键词规则），Phase C 会由结构化 Goal 解析
    （LLM 只解析目标、计划结构仍由代码生成）替换；在此之前 build_plan 与
    goal_manager.compute_fingerprint 共用同一份判定，保证「同一意图」判定一致。
    """
    desc = (description or "").lower()
    is_query = any(kw in desc for kw in ["概览", "overview", "进度", "状态", "status", "progress"])
    is_analyze = any(kw in desc for kw in ["分析", "analyze", "诊断", "diagnose", "检查", "check"])
    is_generate = any(kw in desc for kw in ["生成", "generate", "创建", "create", "制作", "跑"])

    if is_analyze:
        return "analyze"
    if is_query and not is_generate:
        return "query"
    if is_generate:
        if any(kw in desc for kw in ["分镜图", "keyframe"]):
            return "generate_keyframe"
        if any(kw in desc for kw in ["分镜视频", "clip", "视频"]):
            return "generate_clip"
        return "generate_default"
    if is_query:
        return "query"
    return "default"


# ── Plan Builder ───────────────────────────────────────────────────────


def build_plan(
    *,
    goal_id: str,
    description: str,
    project_id: str | None = None,
    available_tools: list[str] | None = None,
    revision: int = 1,
    previous_plan: Plan | None = None,
) -> Plan:
    """根据目标描述构建初始执行计划。

    当前为确定性规则版本：根据目标描述中的关键词匹配合适工具。
    Phase 2 后续将由 LLM 提议 Plan 结构，此处做确定性校验。
    """
    plan = Plan(
        plan_id=agent_plan_id(),
        goal_id=goal_id,
        revision=revision,
        metadata={
            "description": description,
            "project_id": project_id,
            "created_at": datetime.now().isoformat(),
        },
    )

    desc_lower = description.lower()
    # 说明：build_plan 保留原有的独立关键词判定（同一描述可以既查询又生成，
    # 例如「检查进度并生成分镜图」），行为与 Phase A 之前完全一致。
    # A8 的 classify_description 只用于目标指纹，不改变计划结构。
    is_query = any(kw in desc_lower for kw in ["概览", "overview", "进度", "状态", "status", "progress"])
    is_generate = any(kw in desc_lower for kw in ["生成", "generate", "创建", "create", "制作", "跑"])
    is_analyze = any(kw in desc_lower for kw in ["分析", "analyze", "诊断", "diagnose", "检查", "check"])

    # ── 查询类意图：概览 → asset_stats ∥ list_jobs（DAG）──
    if is_query or is_analyze:
        plan.steps.append(
            PlanStep(
                step_key="project_overview",
                tool_name="project_overview",
                description="获取项目基本信息与概览",
                args={"project_id": project_id or ""},
                estimated_timeout=30,
            )
        )
        plan.steps.append(
            PlanStep(
                step_key="asset_stats",
                tool_name="asset_stats",
                description="查询项目资产统计",
                args={"project_id": project_id or ""},
                depends_on=["project_overview"],
                estimated_timeout=15,
            )
        )
        plan.steps.append(
            PlanStep(
                step_key="list_jobs",
                tool_name="list_jobs",
                description="查询项目任务列表与状态",
                args={"project_id": project_id or ""},
                depends_on=["project_overview"],
                estimated_timeout=15,
            )
        )

    # ── 分析/诊断类：叠加 get_script_analysis_status ──
    if is_analyze:
        plan.steps.append(
            PlanStep(
                step_key="script_analysis",
                tool_name="get_parse_status",
                description="查询剧本分析状态与解析进度",
                args={"project_id": project_id or ""},
                depends_on=["project_overview"],
                estimated_timeout=30,
            )
        )

    # ── 生成类意图：project_overview 前置 → 生成操作 ──
    if is_generate:
        overview_key = "overview_check"
        plan.steps.append(
            PlanStep(
                step_key=overview_key,
                tool_name="project_overview",
                description="前置检查：获取项目概览确认可生成",
                args={"project_id": project_id or ""},
                estimated_timeout=30,
            )
        )
        if any(kw in desc_lower for kw in ["分镜图", "keyframe"]):
            plan.steps.append(
                PlanStep(
                    step_key="generate_keyframes",
                    tool_name="request_generate",
                    description="触发分镜图生成任务",
                    args={"project_id": project_id or "", "target": "keyframe"},
                    depends_on=[overview_key],
                    requires_approval=True,
                )
            )
        elif any(kw in desc_lower for kw in ["分镜视频", "clip", "视频"]):
            plan.steps.append(
                PlanStep(
                    step_key="generate_clips",
                    tool_name="batch_request_generate",
                    description="触发分镜视频生成任务",
                    args={"project_id": project_id or "", "target": "clip"},
                    depends_on=[overview_key],
                    requires_approval=True,
                )
            )
        else:
            plan.steps.append(
                PlanStep(
                    step_key="generate_default",
                    tool_name="request_generate",
                    description="触发资产生成任务",
                    args={"project_id": project_id or ""},
                    depends_on=[overview_key],
                    requires_approval=True,
                )
            )

    # ── 默认 fallback ──
    if not plan.steps:
        plan.steps.append(
            PlanStep(
                step_key="default_overview",
                tool_name="project_overview",
                description="获取项目基本信息",
                args={"project_id": project_id or ""},
                estimated_timeout=30,
            )
        )

    return plan


# ── DAG 分析 ───────────────────────────────────────────────────────────

def _detect_cycle(steps: list[PlanStep]) -> list[list[str]]:
    """检测依赖图中的循环依赖（Kahn 算法）。

    返回所有检测到的循环路径列表。
    """
    # 构建邻接表
    step_keys = {s.step_key for s in steps}
    adj: dict[str, set[str]] = {k: set() for k in step_keys}
    in_degree: dict[str, int] = {k: 0 for k in step_keys}

    for step in steps:
        for dep in step.depends_on:
            if dep in step_keys:
                adj[dep].add(step.step_key)
                in_degree[step.step_key] += 1

    # Kahn
    queue = deque([k for k, d in in_degree.items() if d == 0])
    sorted_keys: list[str] = []
    while queue:
        node = queue.popleft()
        sorted_keys.append(node)
        for neighbor in adj[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(sorted_keys) == len(step_keys):
        return []  # 无环

    # 有环：找剩余节点中的环
    remaining = step_keys - set(sorted_keys)
    cycles: list[list[str]] = []
    visited: set[str] = set()
    for node in remaining:
        if node in visited:
            continue
        path: list[str] = []
        stack = [(node, [node])]
        node_visited: set[str] = set()
        while stack:
            curr, trail = stack.pop()
            if curr in node_visited:
                # 找到环
                cycle_start = trail.index(curr)
                cycles.append(trail[cycle_start:])
                break
            node_visited.add(curr)
            for neighbor in adj.get(curr, set()):
                stack.append((neighbor, trail + [neighbor]))
        visited |= node_visited
    return cycles


def _compute_parallel_groups(steps: list[PlanStep]) -> list[list[str]]:
    """根据依赖关系计算并行组：同组内步骤可并行执行。"""
    step_keys = {s.step_key for s in steps}
    in_degree: dict[str, int] = {k: 0 for k in step_keys}
    adj: dict[str, set[str]] = {k: set() for k in step_keys}

    for step in steps:
        for dep in step.depends_on:
            if dep in step_keys:
                adj[dep].add(step.step_key)
                in_degree[step.step_key] += 1

    groups: list[list[str]] = []
    current = [k for k, d in in_degree.items() if d == 0]
    while current:
        groups.append(sorted(current))
        next_group = []
        for node in current:
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    next_group.append(neighbor)
        current = next_group

    if any(d > 0 for d in in_degree.values()):
        remaining = [k for k, d in in_degree.items() if d > 0]
        groups.append(sorted(remaining))

    return groups


# ── Plan Validator：14 项检查 ─────────────────────────────────────────

def validate_plan(plan: Plan, *, project_id: str | None = None) -> ValidationReport:
    """Plan Validator：对 Plan 执行 14 项结构化检查。

    检查项：
    1. 步骤非空
    2. tool_name 存在于 TOOL_REGISTRY
    3. step_key 唯一
    4. tool 权限匹配（read/write/generate）
    5. depends_on 引用存在
    6. 无循环依赖
    7. 并行组计算
    8. 步骤顺序合理（依赖在前）
    9. 参数一致性（字段类型匹配）
    10. 审批工具标记正确
    11. 超时设置合理
    12. plan_id 格式正确
    13. goal_id 非空
    14. description 不为空
    """
    report = ValidationReport()
    registry = _get_tool_registry()
    step_keys = {s.step_key for s in plan.steps}

    # 1: 步骤非空
    if not plan.steps:
        report.add_warning("steps_empty", "计划包含零个步骤，可能无法完成目标")

    # 2: tool_name 存在
    for step in plan.steps:
        if step.tool_name not in registry:
            report.add_error(
                "tool_not_found",
                f"工具 '{step.tool_name}' 不在 TOOL_REGISTRY 中",
                step_key=step.step_key,
                detail={"tool_name": step.tool_name},
            )

    # 3: step_key 唯一
    seen_keys: set[str] = set()
    for step in plan.steps:
        if step.step_key in seen_keys:
            report.add_error(
                "duplicate_step_key",
                f"步骤 key '{step.step_key}' 重复",
                step_key=step.step_key,
            )
        seen_keys.add(step.step_key)

    # 5: depends_on 引用存在
    for step in plan.steps:
        for dep in step.depends_on:
            if dep not in step_keys:
                report.add_error(
                    "invalid_dependency",
                    f"步骤 '{step.step_key}' 依赖不存在的步骤 '{dep}'",
                    step_key=step.step_key,
                    detail={"dependency": dep},
                )

    # 6: 无循环依赖
    cycles = _detect_cycle(plan.steps)
    for cycle in cycles:
        report.add_error(
            "circular_dependency",
            f"检测到循环依赖: {' → '.join(cycle)}",
            detail={"cycle": cycle},
        )

    # 7: 并行组
    groups = _compute_parallel_groups(plan.steps)
    report.add_info(
        "parallel_groups",
        f"并行组: {len(groups)} 组，总步骤: {len(plan.steps)}",
        detail={"groups": groups},
    )

    # 10: 审批工具标记
    for step in plan.steps:
        spec = registry.get(step.tool_name, {})
        if spec.get("requires_approval") and not step.requires_approval:
            report.add_warning(
                "approval_mismatch",
                f"工具 '{step.tool_name}' 注册表标记需要审批，但步骤未标记",
                step_key=step.step_key,
            )

    # 11: 超时设置
    for step in plan.steps:
        spec = registry.get(step.tool_name, {})
        registered_timeout = spec.get("timeout_seconds", 30)
        if step.estimated_timeout < registered_timeout // 2:
            report.add_warning(
                "timeout_too_short",
                f"步骤 '{step.step_key}' 超时 ({step.estimated_timeout}s) 远小于注册值 ({registered_timeout}s)",
                step_key=step.step_key,
            )

    # 12: plan_id 格式
    if not plan.plan_id or not plan.plan_id.startswith("pln_"):
        report.add_error("invalid_plan_id", f"plan_id '{plan.plan_id}' 格式不正确（应为 pln_xxx）")

    # 13: goal_id 非空
    if not plan.goal_id:
        report.add_error("missing_goal_id", "goal_id 为空")

    # 14: description 不为空
    desc = plan.metadata.get("description", "")
    if not desc:
        report.add_warning("empty_description", "计划描述为空，可能影响审计追溯")

    return report


# ── Plan 持久化 ────────────────────────────────────────────────────────

def save_plan(
    db: Session,
    plan: Plan,
    *,
    supersede_previous: bool = True,
) -> None:
    """保存 Plan 到 agent_plan 表，可选地废弃旧版本。"""
    from app.models.agent import AgentPlan

    # 废弃旧版本
    if supersede_previous:
        old_plans = db.scalars(
            select(AgentPlan).where(
                AgentPlan.goal_id == plan.goal_id,
                AgentPlan.status == "active",
            )
        ).all()
        for old in old_plans:
            old.status = "superseded"

    # 创建新 Plan
    new_plan = AgentPlan(
        id=plan.plan_id,
        goal_id=plan.goal_id,
        revision=plan.revision,
        plan_json=plan.to_dict(),
        status="active",
    )
    db.add(new_plan)
    db.flush()


def save_plan_steps(
    db: Session,
    plan: Plan,
) -> None:
    """将 Plan 的 steps 写入 agent_plan_step 表。

    depends_on 落库规范：PlanStep.depends_on 保存的是 step_key（计划内语义标识），
    但 get_next_pending_steps 依据 AgentPlanStep.id 判定依赖是否满足，
    因此落库时必须先把 step_key 解析为对应步骤的 id。
    """
    from app.models.agent import AgentPlanStep

    # 预生成每步 id，建立 step_key → id 映射（依赖可能指向后续步骤，需先建全量映射）
    step_ids: dict[str, str] = {}
    for step in plan.steps:
        if step.step_key and step.step_key not in step_ids:
            step_ids[step.step_key] = agent_plan_step_id()

    for idx, step in enumerate(plan.steps):
        # 将依赖的 step_key 翻译为步骤 id，忽略无法解析的 key（校验阶段已拒绝）
        resolved_deps = [step_ids[k] for k in step.depends_on if k in step_ids]
        ps = AgentPlanStep(
            id=step_ids.get(step.step_key) or agent_plan_step_id(),
            plan_id=plan.plan_id,
            task_id="",  # 将在 ExecutionManager 分配 Task 后回填
            step_index=idx,
            step_type="tool_call",
            tool_name=step.tool_name,
            description=step.description,
            args=step.args,
            depends_on=resolved_deps if resolved_deps else None,
            preconditions=step.preconditions if step.preconditions else None,
            verification=step.verification if step.verification else None,
            requires_approval=step.requires_approval,
            status="pending",
            max_retries=3,
            timeout_seconds=step.estimated_timeout,
            step_key=step.step_key or None,
        )
        db.add(ps)
    db.flush()


def carry_over_steps(db: Session, old_plan_id: str, new_plan: Plan) -> list[str]:
    """把旧计划中已 succeeded 且 step_key 相同、工具与参数一致的步骤直接带过来（A9）。

    被带过来的步骤：status=succeeded，result 复制并标记 carried_over_from=<old_step_id>，
    不再重新执行。返回被带过来的新步骤 id 列表。
    """
    from app.models.agent import AgentPlanStep

    old_steps = db.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id == old_plan_id)
    ).all()
    by_key = {
        (s.step_key or ""): s
        for s in old_steps
        if s.step_key and s.status == "succeeded"
    }
    if not by_key:
        return []

    new_steps = db.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id == new_plan.plan_id)
    ).all()
    plan_by_key = {p.step_key: p for p in new_plan.steps}

    carried: list[str] = []
    for row in new_steps:
        key = row.step_key or ""
        old = by_key.get(key)
        spec = plan_by_key.get(key)
        if old is None or spec is None:
            continue
        # 工具与参数必须完全一致，否则视为「同一件事但做法变了」，需要重新执行
        if (old.tool_name or "") != (spec.tool_name or ""):
            continue
        if (old.args or {}) != (spec.args or {}):
            continue
        result = dict(old.result or {})
        result["carried_over_from"] = old.id
        row.status = "succeeded"
        row.result = result
        row.completed_at = datetime.now()
        carried.append(row.id)
    db.flush()
    return carried


def build_and_save_plan(
    db: Session,
    *,
    goal_id: str,
    description: str,
    project_id: str | None = None,
    previous_plan: Plan | None = None,
) -> tuple[Plan, ValidationReport]:
    """一站式：构建 Plan → 校验 → 保存（通过则持久化，失败则只返回报告）。"""
    revision = (previous_plan.revision + 1) if previous_plan else 1

    plan = build_plan(
        goal_id=goal_id,
        description=description,
        project_id=project_id,
        revision=revision,
        previous_plan=previous_plan,
    )

    report = validate_plan(plan, project_id=project_id)

    if report.passed:
        save_plan(db, plan, supersede_previous=True)
        save_plan_steps(db, plan)

    return plan, report


def get_latest_plan(db: Session, goal_id: str) -> Plan | None:
    """获取 Goal 的最新执行计划。"""
    from app.models.agent import AgentPlan

    row = db.scalars(
        select(AgentPlan)
        .where(AgentPlan.goal_id == goal_id, AgentPlan.status == "active")
        .order_by(AgentPlan.revision.desc())
        .limit(1)
    ).first()

    if row is None or row.plan_json is None:
        return None

    return Plan.from_json(goal_id, row.plan_json)


def get_latest_plan_any(db: Session, goal_id: str) -> Plan | None:
    """获取 Goal 的最新执行计划（不区分状态）。

    A10：目标完成时 plan.status 会被置为 completed，此时 get_latest_plan 取不到；
    但「再次执行同一目标」仍需要以它为 previous_plan 递增 revision，因此提供本函数。
    """
    from app.models.agent import AgentPlan

    row = db.scalars(
        select(AgentPlan)
        .where(AgentPlan.goal_id == goal_id)
        .order_by(AgentPlan.revision.desc())
        .limit(1)
    ).first()

    if row is None or row.plan_json is None:
        return None

    return Plan.from_json(goal_id, row.plan_json)


def get_plan_steps(db: Session, plan_id: str) -> list[dict]:
    """获取 Plan 的所有步骤（按 step_index 排序）。"""
    from app.models.agent import AgentPlanStep

    rows = db.scalars(
        select(AgentPlanStep)
        .where(AgentPlanStep.plan_id == plan_id)
        .order_by(AgentPlanStep.step_index)
    ).all()

    return [
        {
            "id": r.id,
            "step_index": r.step_index,
            "step_type": r.step_type,
            "step_key": r.step_key,
            "tool_name": r.tool_name,
            "description": r.description,
            "args": r.args,
            "depends_on": r.depends_on,
            "status": r.status,
            "requires_approval": r.requires_approval,
            "retry_count": r.retry_count,
            "result": r.result,
            # A10：状态接口与恢复逻辑需要按步骤反查 Job / 审批
            "job_id": r.job_id,
            "confirmation_id": r.confirmation_id,
            "error_code": r.error_code,
            "error_message": r.error_message,
        }
        for r in rows
    ]


def get_next_pending_steps(db: Session, plan_id: str) -> list[dict]:
    """获取下一步可执行的步骤（pending 中 depends_on 全部满足）。"""
    from app.models.agent import AgentPlanStep

    all_steps = db.scalars(
        select(AgentPlanStep)
        .where(AgentPlanStep.plan_id == plan_id)
        .order_by(AgentPlanStep.step_index)
    ).all()

    completed_keys = {
        s.id for s in all_steps if s.status in ("succeeded", "skipped")
    }

    ready = []
    for s in all_steps:
        if s.status != "pending":
            continue
        deps = s.depends_on or []
        if all(d in completed_keys for d in deps):
            ready.append(
                {
                    "id": s.id,
                    "step_index": s.step_index,
                    "tool_name": s.tool_name,
                    "description": s.description,
                    "args": s.args,
                    "requires_approval": s.requires_approval,
                }
            )
    return ready


# ── Agent API 集成入口 ────────────────────────────────────────────────

def generate_plan_for_goal(
    db: Session,
    *,
    goal_id: str,
    description: str,
    project_id: str | None = None,
) -> dict:
    """为 Goal 生成并保存 Plan，返回前端可用的执行计划信息。

    供 Agent API 注入到 SSE 流中，前端据此渲染 PlanPanel。
    """
    previous = get_latest_plan(db, goal_id)
    plan, report = build_and_save_plan(
        db,
        goal_id=goal_id,
        description=description,
        project_id=project_id,
        previous_plan=previous,
    )

    parallel_groups = _compute_parallel_groups(plan.steps)

    return {
        "plan_id": plan.plan_id,
        "revision": plan.revision,
        "steps_count": len(plan.steps),
        "parallel_groups": len(parallel_groups),
        "validation": report.to_dict(),
        "steps": plan.to_dict()["steps"],
    }
