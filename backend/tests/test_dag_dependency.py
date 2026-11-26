"""DAG 依赖解析与分层就绪验证（Executor 集成）。

原 tests/test_dag_dependency.py 在模块顶层连真实库。这里改为 tmp 库，
断言意图不变：depends_on 落库为步骤 id、按层解锁、依赖满足后下游就绪。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.agent_runtime_fixtures import mk_project


DESC = "检查项目进度概览并分析生成分镜图"


@pytest.fixture()
def ctx(db):
    from app.models.agent import AgentPlanStep
    from app.services.goal_manager import get_or_create_goal
    from app.services.planner import build_and_save_plan

    project = mk_project(db, with_script=True, with_parse=True)
    goal = get_or_create_goal(db, session_id="ags_dagtest2026", description=DESC, project_id=project.id)
    plan, report = build_and_save_plan(db, goal_id=goal.goal_id, description=DESC, project_id=project.id)
    db.commit()

    rows = db.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id == plan.plan_id)
    ).all()
    return project, goal, plan, report, list(rows)


def test_plan_generated_with_six_steps(ctx):
    _project, _goal, plan, report, rows = ctx
    assert report.passed, report.errors
    assert len(plan.steps) == 6
    assert len(rows) == 6


def test_depends_on_persisted_as_step_ids(ctx):
    _project, _goal, _plan, _report, rows = ctx
    all_ids = {r.id for r in rows}
    for r in rows:
        for dep in (r.depends_on or []):
            assert dep in all_ids, f"{r.tool_name}.depends_on={dep} 不是步骤 id"


def test_layer1_is_all_root_steps(ctx, db):
    from app.services.planner import get_next_pending_steps

    _project, _goal, plan, _report, rows = ctx
    root_ids = {r.id for r in rows if not r.depends_on}
    layer1 = get_next_pending_steps(db, plan.plan_id)
    layer1_ids = {s["id"] for s in layer1}
    assert len(layer1) == 2
    assert layer1_ids == root_ids


def test_downstream_unlocks_layer_by_layer(ctx, db):
    from app.services.planner import get_next_pending_steps

    _project, _goal, plan, _report, rows = ctx
    root_ids = {r.id for r in rows if not r.depends_on}

    po_step = next(r for r in rows if r.tool_name == "project_overview" and r.depends_on is None)
    po_step.status = "succeeded"
    db.flush()

    layer2 = get_next_pending_steps(db, plan.plan_id)
    layer2_ids = {s["id"] for s in layer2}
    layer2_tools = sorted(s["tool_name"] for s in layer2)
    assert "asset_stats" in layer2_tools and "list_jobs" in layer2_tools
    assert (root_ids - {po_step.id}).issubset(layer2_ids)

    oc_step = next(
        r for r in rows
        if r.tool_name == "project_overview" and r.depends_on is None and r.status == "pending"
    )
    oc_step.status = "succeeded"
    as_step = next(r for r in rows if r.tool_name == "asset_stats")
    as_step.status = "succeeded"
    db.flush()

    layer3_tools = sorted(s["tool_name"] for s in get_next_pending_steps(db, plan.plan_id))
    assert "list_jobs" in layer3_tools
    assert "get_parse_status" in layer3_tools
    assert "request_generate" in layer3_tools
