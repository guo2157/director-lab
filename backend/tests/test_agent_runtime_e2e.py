"""Agent Loop 端到端功能验证（Phase 1+2 编排组件串联）。

原 tests/test_agent_loop_e2e.py 在模块顶层连真实库、用 print/check 计数，
pytest 收集时就会污染生产数据。这里改为 tmp 库 + 真断言，覆盖同样的链路：
Goal → Context → Constraint → Plan → ExecutionLog → Verifier → Replanner → 状态查询。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from tests.agent_runtime_fixtures import mk_project


@pytest.fixture()
def project(db):
    return mk_project(db, with_script=True, with_parse=True, with_assets=2)


@pytest.fixture()
def goal(db, project):
    from app.services.goal_manager import get_or_create_goal

    return get_or_create_goal(
        db,
        session_id="ags_testloop2026",
        description="测试 Agent Loop 闭环：检查项目进度并生成概览报告",
        project_id=project.id,
    )


@pytest.fixture()
def plan(db, goal):
    from app.services.planner import build_and_save_plan

    p, report = build_and_save_plan(
        db,
        goal_id=goal.goal_id,
        description="检查项目进度概览",
        project_id=goal.project_id,
    )
    assert report.passed, report.errors
    return p


def test_goal_create_and_reuse(db, project):
    from app.services.goal_manager import get_active_goal, get_or_create_goal

    goal = get_or_create_goal(
        db, session_id="ags_testloop2026", description="初始描述", project_id=project.id
    )
    assert goal.goal_id and goal.status == "active"

    again = get_or_create_goal(
        db, session_id="ags_testloop2026", description="更新的描述", project_id=project.id
    )
    assert again.goal_id == goal.goal_id
    # A8：同一意图复用目标时不再覆盖 description，最新描述进入 constraints["history"]
    assert again.description == "初始描述"
    assert (again.constraints or {}).get("history", [])[-1] == "更新的描述"
    assert get_active_goal(db, "ags_testloop2026").goal_id == goal.goal_id


def test_context_snapshot_and_prompt(db, project, goal):
    from app.services.context_resolver import build_context_prompt, snapshot

    ctx = snapshot(db, session_id="ags_testloop2026", project_id=project.id)
    assert ctx.user.session_id == "ags_testloop2026"
    assert isinstance(build_context_prompt(ctx), str)


def test_constraint_report(db, goal):
    from app.services.constraint_manager import check_constraints

    report = check_constraints(db, goal_constraints=goal.constraints, project_id=goal.project_id)
    assert isinstance(report.passed, bool)


def test_plan_persisted_with_steps(db, plan):
    from app.services.planner import get_next_pending_steps, get_plan_steps

    assert plan.plan_id.startswith("pln_")
    assert plan.steps

    steps = get_plan_steps(db, plan.plan_id)
    assert len(steps) == len(plan.steps)
    assert all(s["tool_name"] for s in steps)
    assert get_next_pending_steps(db, plan.plan_id)


def test_execution_log_roundtrip(db, goal, plan):
    from app.services.agent_runtime import (
        StepResult,
        get_execution_logs,
        log_step_end,
        log_step_start,
    )
    from app.services.planner import get_plan_steps

    steps = get_plan_steps(db, plan.plan_id)
    step_id = steps[0]["id"]
    tool_name = steps[0]["tool_name"]
    start = datetime.now()

    log_id = log_step_start(
        db,
        goal_id=goal.goal_id,
        plan_id=plan.plan_id,
        step_id=step_id,
        tool_name=tool_name,
        input_data={"description": "e2e"},
    )
    assert log_id

    end_log = log_step_end(
        db,
        goal_id=goal.goal_id,
        plan_id=plan.plan_id,
        step_id=step_id,
        tool_name=tool_name,
        result=StepResult.SUCCESS,
        output_data={"status": "mock_success"},
        started_at=start,
    )
    assert end_log

    logs = get_execution_logs(db, goal_id=goal.goal_id, limit=10)
    assert len(logs) >= 2


def test_verifier_runs_on_step_and_plan(db, goal, plan):
    from app.services.planner import get_plan_steps
    from app.services.verifier import verify_plan_completion, verify_step

    steps = get_plan_steps(db, plan.plan_id)
    v = verify_step(
        db, step_id=steps[0]["id"], tool_name=steps[0]["tool_name"], result_data={"status": "ok"}
    )
    assert isinstance(v.verified, bool)

    pc = verify_plan_completion(db, plan_id=plan.plan_id)
    assert isinstance(pc["all_verified"], bool)
    assert pc["total"] == len(steps)


def test_replanner_error_classification():
    """A9 后的分类语义：优先按 error_code 映射，重试耗尽即失败。

    旧断言（ToolNotFound → REPLAN、重试耗尽 → REPLAN）固化了 A9 要修的行为：
    规则计划对 tool_not_found 无法自动修正，重试耗尽后再 Replan 只会重复失败。
    """
    from app.services.replanner import ErrorContext, ReplanAction, classify_error

    assert classify_error(
        ErrorContext(step_id="fake", error_code="TimeoutError", retry_count=1)
    ) == ReplanAction.RETRY
    assert classify_error(
        ErrorContext(step_id="fake", error_code="tool_not_found")
    ) == ReplanAction.FAIL
    assert classify_error(
        ErrorContext(step_id="fake", error_code="TimeoutError", retry_count=5)
    ) == ReplanAction.FAIL
    assert classify_error(
        ErrorContext(step_id="fake", error_code="precondition_failed")
    ) == ReplanAction.WAIT_USER


def test_goal_status_query(db, project, goal):
    from app.services.agent_runtime import get_goal_status

    status = get_goal_status(db, "ags_testloop2026")
    assert status["has_active_goal"]
