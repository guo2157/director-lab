"""A9 验收测试：Replan 产生新 revision 并复用已完成步骤（P0-11）。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.services import planner as planner_mod
from app.services.agent_runtime import run_agent_loop
from app.services.replanner import (
    ERROR_CODE_ACTIONS,
    ErrorContext,
    ReplanAction,
    classify_error,
)
from tests.agent_runtime_fixtures import fake_tool, mk_project
from tests.test_agent_runtime_tx import _fixed_plan


@pytest.fixture()
def project(db):
    return mk_project(db, with_script=True, with_parse=True)


def _two_step_plan(db, monkeypatch, first, second, project):
    """两个步骤：s0 → s1（串行，便于验证复用）。"""
    from app.services.goal_manager import get_or_create_goal

    goal = get_or_create_goal(
        db, session_id="a9", description="重规划测试", project_id=project.id
    )

    def _build(db_, *, goal_id, description, project_id=None, previous_plan=None):
        plan = planner_mod.Plan(
            plan_id=planner_mod.agent_plan_id(),
            goal_id=goal_id,
            revision=(previous_plan.revision + 1) if previous_plan else 1,
            metadata={"description": description, "project_id": project_id},
        )
        plan.steps.append(
            planner_mod.PlanStep(
                step_key="s0", tool_name=first, description="步骤 0",
                args={}, estimated_timeout=30,
            )
        )
        plan.steps.append(
            planner_mod.PlanStep(
                step_key="s1", tool_name=second, description="步骤 1",
                args={}, depends_on=["s0"], estimated_timeout=30,
            )
        )
        report = planner_mod.validate_plan(plan, project_id=project_id)
        if report.passed:
            planner_mod.save_plan(db_, plan, supersede_previous=True)
            planner_mod.save_plan_steps(db_, plan)
        return plan, report

    monkeypatch.setattr(planner_mod, "build_and_save_plan", _build)
    plan, report = planner_mod.build_and_save_plan(
        db, goal_id=goal.goal_id, description="重规划测试", project_id=project.id
    )
    assert report.passed, report.errors
    return goal, plan


def test_replan_increments_revision_and_links_parent(db, project, monkeypatch):
    from app.services.replanner import ErrorContext, replan

    fake_tool(monkeypatch, "fake_a9_ok", returns={"ok": 1})
    fake_tool(monkeypatch, "fake_a9_bad", raises=RuntimeError("boom"))
    goal, plan = _two_step_plan(db, monkeypatch, "fake_a9_ok", "fake_a9_bad", project)

    from app.models.agent import AgentPlanStep

    step1 = db.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id == plan.plan_id)
    ).all()[1]
    step1.status = "failed"
    db.flush()

    decision = replan(
        db,
        error_ctx=ErrorContext(
            step_id=step1.id, step_key="s1", tool_name="fake_a9_bad",
            error_code="verification_failed", error_message="验证失败",
        ),
        plan_id=plan.plan_id,
        goal_id=goal.goal_id,
        project_id=project.id,
    )
    assert decision.action == ReplanAction.REPLAN, decision.reason
    new_plan = decision.updated_plan
    assert new_plan is not None
    assert new_plan.revision == plan.revision + 1

    from app.models.agent import AgentPlan

    row = db.get(AgentPlan, new_plan.plan_id)
    assert row.parent_plan_id == plan.plan_id


def test_carry_over_succeeded_steps_not_reexecuted(db, project, monkeypatch):
    from app.services.planner import carry_over_steps

    calls = {"s0": 0, "s1": 0}

    def _s0(**kwargs):
        calls["s0"] += 1
        return {"ok": 1}

    def _s1(**kwargs):
        calls["s1"] += 1
        return {"ok": 1}

    fake_tool(monkeypatch, "fake_a9_c0", returns=_s0)
    fake_tool(monkeypatch, "fake_a9_c1", returns=_s1)
    goal, plan = _two_step_plan(db, monkeypatch, "fake_a9_c0", "fake_a9_c1", project)

    from app.models.agent import AgentPlanStep

    steps = db.scalars(
        select(AgentPlanStep)
        .where(AgentPlanStep.plan_id == plan.plan_id)
        .order_by(AgentPlanStep.step_index)
    ).all()
    steps[0].status = "succeeded"
    steps[0].result = {"ok": True, "data": {"done": 1}}
    db.flush()

    # 生成新 revision（模拟 Replan 的产物）
    new_plan, report = planner_mod.build_and_save_plan(
        db, goal_id=goal.goal_id, description="重规划测试",
        project_id=project.id, previous_plan=plan,
    )
    assert report.passed
    carried = carry_over_steps(db, plan.plan_id, new_plan)
    assert len(carried) == 1

    new_steps = db.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id == new_plan.plan_id)
    ).all()
    by_key = {s.step_key: s for s in new_steps}
    assert by_key["s0"].status == "succeeded"
    assert by_key["s0"].result["carried_over_from"] == steps[0].id
    assert by_key["s1"].status == "pending"
    assert calls == {"s0": 0, "s1": 0}, "复用步骤不应触发工具调用"


def test_identical_plan_fails_fast(db, project, monkeypatch):
    """参数错误 → 一次后立即 FAIL，工具调用计数为 1，不是 4。"""
    from app.services.agent_tools import ToolError

    calls = {"n": 0}

    def _bad(**kwargs):
        calls["n"] += 1
        raise ToolError("tool_invalid_args", "参数错误：缺少 shot_id")

    fake_tool(monkeypatch, "fake_a9_args", returns=_bad)
    _fixed_plan(monkeypatch, "", ["fake_a9_args"])

    result = run_agent_loop(
        db, session_id="a9fail", description="重规划失败测试", project_id=project.id
    )
    assert result.status in ("failed", "partially_completed"), (result.status, result.message)
    assert calls["n"] == 1, f"参数错误不应重复执行，实际 {calls['n']} 次"


def test_precondition_failed_waits_user(db, project, monkeypatch):
    from app.services.agent_tools import ToolError

    fake_tool(monkeypatch, "fake_a9_pre", raises=ToolError("precondition_failed", "缺少剧本"))
    _fixed_plan(monkeypatch, "", ["fake_a9_pre"])

    result = run_agent_loop(
        db, session_id="a9pre", description="前置条件缺失测试", project_id=project.id
    )
    assert result.status == "waiting_user", (result.status, result.message)
    assert "缺少剧本" in (result.message or "")


def test_error_code_mapping_table():
    """ERROR_CODE_ACTIONS 的每个键都能被 classify_error 正确识别。"""
    for code, expected in ERROR_CODE_ACTIONS.items():
        ctx = ErrorContext(
            step_id="aps_x", error_code=code, retry_count=0, max_retries=3,
        )
        assert classify_error(ctx) == expected, code


def test_retry_exhausted_fails():
    ctx = ErrorContext(step_id="aps_x", error_code="tool_internal_error", retry_count=3, max_retries=3)
    assert classify_error(ctx) == ReplanAction.FAIL


def test_unknown_error_defaults_to_fail():
    ctx = ErrorContext(step_id="aps_x", error_code="some_business_error", error_message="资产不存在")
    assert classify_error(ctx) == ReplanAction.FAIL


def test_supersede_not_counted_as_replan(db, project, monkeypatch):
    """A8 的 supersede 不应计入 replan 次数。"""
    from app.services.replanner import _count_goal_replans

    fake_tool(monkeypatch, "fake_a9_sup", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_a9_sup"])

    first = run_agent_loop(
        db, session_id="a9sup", description="检查项目进度", project_id=project.id
    )
    assert _count_goal_replans(db, first.goal_id, first.plan_id) == 0
