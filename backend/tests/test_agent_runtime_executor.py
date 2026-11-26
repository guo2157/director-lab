"""A4 验收测试：Executor 只走 execute_tool 网关（P0-5）。"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.services.execution_manager import execute_step
from tests.agent_runtime_fixtures import fake_tool, mk_project
from tests.test_agent_runtime_tx import _fixed_plan


@pytest.fixture()
def project(db):
    return mk_project(db, with_script=True, with_parse=True)


def _first_step(db, plan):
    from app.models.agent import AgentPlanStep

    return db.scalars(
        select(AgentPlanStep)
        .where(AgentPlanStep.plan_id == plan.plan_id)
        .order_by(AgentPlanStep.step_index)
    ).first()


def _plan_with(db, monkeypatch, tools, project):
    from app.services.goal_manager import get_or_create_goal

    goal = get_or_create_goal(
        db, session_id="a4", description="执行测试步骤", project_id=project.id
    )
    _fixed_plan(monkeypatch, "", tools)
    from app.services.planner import build_and_save_plan

    plan, report = build_and_save_plan(
        db, goal_id=goal.goal_id, description="执行测试步骤", project_id=project.id
    )
    assert report.passed, report.errors
    return goal, plan


def test_uses_execute_tool_gateway(db, project, monkeypatch):
    from app.services import agent_tools

    calls = {"n": 0}
    real = agent_tools.execute_tool

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    fake_tool(monkeypatch, "fake_a4", returns={"ok": 1})
    monkeypatch.setattr(agent_tools, "execute_tool", _counting)

    goal, plan = _plan_with(db, monkeypatch, ["fake_a4"], project)
    step = _first_step(db, plan)
    result = execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)

    assert result["success"] is True
    assert calls["n"] == 1, "必须经 execute_tool 网关执行"


def test_precondition_failure_marks_failed(db, project, monkeypatch):
    goal, plan = _plan_with(db, monkeypatch, ["asset_stats"], project)
    step = _first_step(db, plan)
    step.args = {"project_id": "prj_not_exist"}

    result = execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    assert result["success"] is False
    assert step.status == "failed"
    assert step.error_code in {"project_not_found", "precondition_failed"}


def test_invalid_args_marks_failed(db, project, monkeypatch):
    class _StrictInput(BaseModel):
        required_field: str = Field(min_length=1)

    fake_tool(monkeypatch, "fake_strict", returns={"ok": 1}, spec_overrides={"input_model": _StrictInput})
    goal, plan = _plan_with(db, monkeypatch, ["fake_strict"], project)
    step = _first_step(db, plan)
    step.args = {}

    result = execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    assert result["success"] is False
    assert step.error_code == "tool_invalid_args"


def test_server_injects_project_and_session(db, project, monkeypatch):
    class _WithIds(BaseModel):
        session_id: str | None = None
        project_id: str | None = None

    seen = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return {"ok": 1}

    fake_tool(
        monkeypatch, "fake_ids", returns=_capture, spec_overrides={"input_model": _WithIds}
    )
    goal, plan = _plan_with(db, monkeypatch, ["fake_ids"], project)
    step = _first_step(db, plan)
    step.args = {"project_id": "prj_other", "session_id": "ags_other"}

    execute_step(
        db,
        step_id=step.id,
        plan_id=plan.plan_id,
        goal_id=goal.goal_id,
        session_id="ags_real",
        project_id=project.id,
    )
    assert seen["project_id"] == project.id
    assert seen["session_id"] == "ags_real"


def test_sync_step_creates_no_task(db, project, monkeypatch):
    from app.models.task import Task

    before = db.scalar(select(Task.id).limit(1))
    fake_tool(monkeypatch, "fake_notask", returns={"ok": 1})
    goal, plan = _plan_with(db, monkeypatch, ["fake_notask"], project)
    step = _first_step(db, plan)

    result = execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    assert result["success"] is True
    assert db.scalar(select(Task.id).limit(1)) == before
