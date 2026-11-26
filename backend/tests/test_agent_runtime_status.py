"""A10 验收测试：状态接口、终态与词表收敛（P0-12 及相关）。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.services import planner as planner_mod
from app.services.agent_runtime import _derive_loop_status, run_agent_loop
from tests.agent_runtime_fixtures import fake_tool, mk_project
from tests.test_agent_runtime_tx import _fixed_plan


@pytest.fixture()
def project(db):
    return mk_project(db, with_script=True, with_parse=True)


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["succeeded", "skipped"], "completed"),
        (["succeeded", "waiting_user"], "waiting_user"),
        (["waiting_approval", "waiting_job"], "waiting_approval"),
        (["waiting_job", "succeeded"], "waiting_job"),
        (["pending"], "running"),
        (["succeeded", "failed"], "partially_completed"),
        (["failed", "failed"], "failed"),
        (["succeeded", "cancelled"], "partially_completed"),
        (["cancelled"], "failed"),
        ([], "failed"),
    ],
)
def test_derive_loop_status(statuses, expected):
    steps = [{"status": s} for s in statuses]
    assert _derive_loop_status(steps) == expected


def test_status_endpoint_by_goal_id(db, project, monkeypatch, patch_runtime_sessionmaker):
    """P0-12 回归：GET /status/{goal_id} 必须按 goal_id 查到，而不是恒 404。"""
    from fastapi.testclient import TestClient

    from app.main import app

    fake_tool(monkeypatch, "fake_a10_ok", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_a10_ok"])
    result = run_agent_loop(
        db, session_id="a10api", description="状态接口测试", project_id=project.id
    )
    db.commit()

    client = TestClient(app)
    resp = client.get(f"/api/agent/loop/status/{result.goal_id}")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["goal"]["goal_id"] == result.goal_id
    assert payload["latest_plan"]["plan_id"] == result.plan_id
    assert payload["steps"] and payload["steps"][0]["status"] == "succeeded"
    assert payload["loop_status"] == "completed"
    assert "execution_logs" in payload

    missing = client.get("/api/agent/loop/status/gol_notexist")
    assert missing.status_code == 404


def test_upstream_failed_skips_dependents(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a10_bad", raises=RuntimeError("boom"))
    fake_tool(monkeypatch, "fake_a10_down", returns={"ok": 1})

    def _build(db_, *, goal_id, description, project_id=None, previous_plan=None):
        plan = planner_mod.Plan(
            plan_id=planner_mod.agent_plan_id(),
            goal_id=goal_id,
            revision=(previous_plan.revision + 1) if previous_plan else 1,
            metadata={"description": description, "project_id": project_id},
        )
        plan.steps.append(
            planner_mod.PlanStep(
                step_key="a", tool_name="fake_a10_bad", description="上游",
                args={}, estimated_timeout=30,
            )
        )
        plan.steps.append(
            planner_mod.PlanStep(
                step_key="b", tool_name="fake_a10_down", description="下游",
                args={}, depends_on=["a"], estimated_timeout=30,
            )
        )
        report = planner_mod.validate_plan(plan, project_id=project_id)
        if report.passed:
            planner_mod.save_plan(db_, plan, supersede_previous=True)
            planner_mod.save_plan_steps(db_, plan)
        return plan, report

    monkeypatch.setattr(planner_mod, "build_and_save_plan", _build)
    result = run_agent_loop(
        db, session_id="a10up", description="上游失败测试", project_id=project.id
    )
    assert result.status == "failed", (result.status, result.message)

    from app.models.agent import AgentPlanStep

    steps = {
        s.step_key: s
        for s in db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == result.plan_id)
        ).all()
    }
    assert steps["a"].status == "failed"
    assert steps["b"].status == "skipped"
    assert steps["b"].error_code == "upstream_failed"


def test_partially_completed_keeps_goal_active(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a10_p_ok", returns={"ok": 1})
    fake_tool(monkeypatch, "fake_a10_p_bad", raises=RuntimeError("boom"))

    def _build(db_, *, goal_id, description, project_id=None, previous_plan=None):
        plan = planner_mod.Plan(
            plan_id=planner_mod.agent_plan_id(),
            goal_id=goal_id,
            revision=(previous_plan.revision + 1) if previous_plan else 1,
            metadata={"description": description, "project_id": project_id},
        )
        for name in ("fake_a10_p_ok", "fake_a10_p_bad"):
            plan.steps.append(
                planner_mod.PlanStep(
                    step_key=name, tool_name=name, description=name,
                    args={}, estimated_timeout=30,
                )
            )
        report = planner_mod.validate_plan(plan, project_id=project_id)
        if report.passed:
            planner_mod.save_plan(db_, plan, supersede_previous=True)
            planner_mod.save_plan_steps(db_, plan)
        return plan, report

    monkeypatch.setattr(planner_mod, "build_and_save_plan", _build)
    result = run_agent_loop(
        db, session_id="a10part", description="部分完成测试", project_id=project.id
    )
    assert result.status == "partially_completed", (result.status, result.message)

    from app.models.agent import AgentGoal

    goal = db.get(AgentGoal, result.goal_id)
    assert goal.status == "active", "部分完成时目标必须保持 active，允许用户继续补做"


def test_completed_sets_goal_and_plan_status(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a10_done", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_a10_done"])
    result = run_agent_loop(
        db, session_id="a10done", description="完成测试", project_id=project.id
    )
    assert result.status == "completed"

    from app.models.agent import AgentGoal, AgentPlan

    assert db.get(AgentGoal, result.goal_id).status == "completed"
    assert db.get(AgentPlan, result.plan_id).status == "completed"


def test_steps_never_use_completed_status(db, project, monkeypatch):
    """词表收敛：步骤状态不允许出现 completed。"""
    fake_tool(monkeypatch, "fake_a10_vocab", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_a10_vocab"])
    result = run_agent_loop(
        db, session_id="a10vocab", description="词表测试", project_id=project.id
    )

    from app.models.agent import AgentPlanStep

    steps = db.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id == result.plan_id)
    ).all()
    assert all(s.status != "completed" for s in steps)
    assert all(s.status == "succeeded" for s in steps)

def test_completed_goal_reused_keeps_completion_history(db, project, monkeypatch):
    """A8 + A10：同一意图在目标完成后再次请求 → 复用同一 goal、生成新 revision，
    且完成事实写入 completion_history（不被 supersede 改写）。"""
    fake_tool(monkeypatch, "fake_a10_hist", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_a10_hist"])

    first = run_agent_loop(
        db, session_id="a10hist", description="检查项目进度", project_id=project.id
    )
    assert first.status == "completed"

    from app.models.agent import AgentGoal

    assert db.get(AgentGoal, first.goal_id).status == "completed"

    second = run_agent_loop(
        db, session_id="a10hist", description="检查项目进度", project_id=project.id
    )
    assert second.goal_id == first.goal_id
    assert second.plan_id != first.plan_id

    goal = db.get(AgentGoal, first.goal_id)
    history = (goal.constraints or {}).get("completion_history") or []
    assert history, "复用已完成目标时必须保留完成事实"
