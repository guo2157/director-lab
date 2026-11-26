"""A6 验收测试：Verifier 以真实状态判定（P0-7）。

覆盖：
- step=failed 不能判 verified
- waiting_job + Job=queued → pending（不触发 Replan）
- Job=failed → verified=False
- Job 归属其他项目 → failed
- 全部 skipped 不算通过
- Loop 中工具抛错时 completed_steps == 0（回归 P0-7）
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.services import planner as planner_mod
from app.services.agent_runtime import run_agent_loop
from app.services.verifier import verify_plan_completion, verify_step
from tests.agent_runtime_fixtures import fake_tool, mk_job, mk_project
from tests.test_agent_runtime_tx import _fixed_plan


@pytest.fixture()
def project(db):
    return mk_project(db, with_script=True, with_parse=True)


def _goal_and_plan(db, monkeypatch, tools, project, *, session="a6"):
    from app.services.goal_manager import get_or_create_goal
    from app.services.planner import build_and_save_plan

    goal = get_or_create_goal(
        db, session_id=session, description="验证器测试", project_id=project.id
    )
    _fixed_plan(monkeypatch, "", tools)
    plan, report = build_and_save_plan(
        db, goal_id=goal.goal_id, description="验证器测试", project_id=project.id
    )
    assert report.passed, report.errors
    return goal, plan


def _steps(db, plan):
    from app.models.agent import AgentPlanStep

    return db.scalars(
        select(AgentPlanStep)
        .where(AgentPlanStep.plan_id == plan.plan_id)
        .order_by(AgentPlanStep.step_index)
    ).all()


def test_failed_step_not_verified(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a6_fail", returns={"ok": 1})
    goal, plan = _goal_and_plan(db, monkeypatch, ["fake_a6_fail"], project)
    step = _steps(db, plan)[0]
    step.status = "failed"
    step.error_code = "tool_internal_error"
    db.flush()

    verification = verify_step(db, step_id=step.id, project_id=project.id)
    assert verification.verified is False
    assert verification.overall == "failed"
    assert any(c.name == "step_status" and c.status == "failed" for c in verification.checks)


def test_failed_step_with_ok_false_result_fails(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a6_result", returns={"ok": 1})
    goal, plan = _goal_and_plan(db, monkeypatch, ["fake_a6_result"], project)
    step = _steps(db, plan)[0]
    step.status = "succeeded"
    step.result = {"ok": False, "error_code": "tool_internal_error", "error_message": "boom"}
    db.flush()

    verification = verify_step(db, step_id=step.id, project_id=project.id)
    assert verification.verified is False
    assert any(c.name == "tool_result_ok" for c in verification.checks)


def test_waiting_job_is_pending(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a6_wait", returns={"ok": 1})
    goal, plan = _goal_and_plan(db, monkeypatch, ["fake_a6_wait"], project)
    step = _steps(db, plan)[0]
    job = mk_job(db, project.id, status="queued")
    step.status = "waiting_job"
    step.job_id = job.id
    db.flush()

    verification = verify_step(db, step_id=step.id, project_id=project.id)
    assert verification.overall == "pending"
    assert verification.verified is False


def test_job_failed_fails_verification(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a6_jobfail", returns={"ok": 1})
    goal, plan = _goal_and_plan(db, monkeypatch, ["fake_a6_jobfail"], project)
    step = _steps(db, plan)[0]
    job = mk_job(db, project.id, status="failed")
    step.status = "waiting_job"
    step.job_id = job.id
    db.flush()

    verification = verify_step(db, step_id=step.id, project_id=project.id)
    assert verification.verified is False
    assert verification.overall == "failed"


def test_job_other_project_fails(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a6_jobother", returns={"ok": 1})
    goal, plan = _goal_and_plan(db, monkeypatch, ["fake_a6_jobother"], project)
    step = _steps(db, plan)[0]
    other_project = mk_project(db, with_script=True, with_parse=True)
    job = mk_job(db, other_project.id, status="succeeded")
    step.status = "succeeded"
    step.job_id = job.id
    db.flush()

    verification = verify_step(db, step_id=step.id, project_id=project.id)
    assert verification.verified is False
    assert any(c.name == "job_project_match" for c in verification.checks)


def test_all_skipped_not_verified(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a6_skip", returns={"ok": 1})
    goal, plan = _goal_and_plan(db, monkeypatch, ["fake_a6_skip"], project)
    step = _steps(db, plan)[0]
    step.status = "skipped"
    db.flush()

    verification = verify_step(
        db, step_id=step.id, project_id=project.id, dimensions=["quality"]
    )
    assert verification.overall == "skipped"
    assert verification.verified is False


def test_plan_completion_partial_when_some_succeeded(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a6_p1", returns={"ok": 1})
    goal, plan = _goal_and_plan(db, monkeypatch, ["fake_a6_p1"], project)
    steps = _steps(db, plan)
    steps[0].status = "succeeded"
    db.flush()

    # 再造一条失败步骤
    from app.models.agent import AgentPlanStep

    db.add(
        AgentPlanStep(
            id="asp_a6partial0000000000000001",
            task_id="",
            step_index=99,
            step_type="tool_call",
            tool_name="fake_a6_p1",
            plan_id=plan.plan_id,
            status="failed",
        )
    )
    db.flush()

    status = verify_plan_completion(db, plan_id=plan.plan_id)
    assert status["all_verified"] is False
    assert status["partial"] is True
    assert len(status["failed_steps"]) == 1


def test_loop_does_not_count_failed_as_completed(db, project, monkeypatch):
    """P0-7 回归：工具抛错时 completed_steps 必须为 0。"""
    fake_tool(monkeypatch, "fake_a6_raise", raises=RuntimeError("boom"))
    _fixed_plan(monkeypatch, "", ["fake_a6_raise"])

    result = run_agent_loop(
        db, session_id="a6raise", description="失败步骤测试", project_id=project.id
    )
    assert result.completed_steps == 0, (result.status, result.message)
    assert result.failed_steps >= 1
