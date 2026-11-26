"""Phase B B3 验收：Job 终态 → 恢复标记 → tick 收敛。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.agent import AgentPlanStep
from app.services import planner as planner_mod
from app.services.agent_runtime import tick
from app.services.task_orchestrator import bridge_job_event
from tests.agent_runtime_fixtures import fake_tool, mk_job, mk_project


@pytest.fixture()
def project(db):
    p = mk_project(db, with_script=True, with_parse=True)
    db.commit()
    return p


def _build_plan(monkeypatch, tools, deps=None):
    def _build(db_, *, goal_id, description, project_id=None, previous_plan=None):
        plan = planner_mod.Plan(
            plan_id=planner_mod.agent_plan_id(),
            goal_id=goal_id,
            revision=(previous_plan.revision + 1) if previous_plan else 1,
            metadata={"description": description, "project_id": project_id},
        )
        for idx, name in enumerate(tools):
            depends = deps[idx] if deps else ([f"s{idx - 1}"] if idx else [])
            plan.steps.append(
                planner_mod.PlanStep(
                    step_key=f"s{idx}", tool_name=name, description=f"步骤 {idx}",
                    args={}, depends_on=depends, estimated_timeout=30,
                )
            )
        report = planner_mod.validate_plan(plan, project_id=project_id)
        if report.passed:
            planner_mod.save_plan(db_, plan, supersede_previous=True)
            planner_mod.save_plan_steps(db_, plan)
        return plan, report

    monkeypatch.setattr(planner_mod, "build_and_save_plan", _build)


def _goal_with_plan(db, project, monkeypatch, tools, *, session, deps=None):
    from app.services.goal_manager import get_or_create_goal

    _build_plan(monkeypatch, tools, deps)
    goal = get_or_create_goal(
        db, session_id=session, description="执行测试步骤", project_id=project.id
    )
    plan, report = planner_mod.build_and_save_plan(
        db, goal_id=goal.goal_id, description="执行测试步骤", project_id=project.id
    )
    assert report.passed, report.errors
    db.commit()
    return goal, plan


def _steps(db, plan_id):
    return db.scalars(
        select(AgentPlanStep)
        .where(AgentPlanStep.plan_id == plan_id)
        .order_by(AgentPlanStep.step_index)
    ).all()


def test_terminal_job_marks_step(db, project, monkeypatch, rt_sessionmaker):
    """job.finished(succeeded) → 打标记 → tick 后步骤 succeeded，下游开始执行。"""
    calls = {"downstream": 0}

    def _downstream(**_kw):
        calls["downstream"] += 1
        return {"ok": 1}

    fake_tool(monkeypatch, "b3_job", returns={"ok": 1})
    fake_tool(monkeypatch, "b3_next", returns=_downstream)
    goal, plan = _goal_with_plan(
        db, project, monkeypatch, ["b3_job", "b3_next"], session="b3term"
    )

    job = mk_job(db, project.id, status="succeeded")
    s0, s1 = _steps(db, plan.plan_id)
    s0.status = "waiting_job"
    s0.job_id = job.id
    db.commit()

    bridged = bridge_job_event(db, job_id=job.id, event_type="job.finished", payload={"status": "succeeded"})
    db.commit()
    assert bridged == 0  # 没有 TaskStage
    db.expire_all()
    marked = db.get(AgentPlanStep, s0.id)
    assert marked.resume_requested_at is not None
    assert marked.resume_reason == "job_terminal"

    result = tick(rt_sessionmaker, goal.goal_id, trigger="worker_resume")
    db.expire_all()
    assert result.lifecycle_state == "completed", result.to_dict()
    assert db.get(AgentPlanStep, s0.id).status == "succeeded"
    assert db.get(AgentPlanStep, s1.id).status == "succeeded"
    assert calls["downstream"] == 1


def test_job_failed_goes_waiting_user(db, project, monkeypatch, rt_sessionmaker):
    fake_tool(monkeypatch, "b3_fail_job", returns={"ok": 1})
    goal, plan = _goal_with_plan(db, project, monkeypatch, ["b3_fail_job"], session="b3fail")
    job = mk_job(db, project.id, status="failed")
    job.error_code = "oom"
    job.error_msg = "显存不足"
    step = _steps(db, plan.plan_id)[0]
    step.status = "waiting_job"
    step.job_id = job.id
    db.commit()

    bridge_job_event(db, job_id=job.id, event_type="job.finished", payload={"status": "failed"})
    db.commit()

    result = tick(rt_sessionmaker, goal.goal_id, trigger="worker_resume")
    db.expire_all()
    step = db.get(AgentPlanStep, step.id)
    assert step.status == "waiting_user"
    assert step.error_code == "job_failed:oom"
    assert "显存不足" in (step.error_message or "")
    assert result.lifecycle_state == "waiting_user"


def test_marker_set_even_without_stage(db, project, monkeypatch):
    fake_tool(monkeypatch, "b3_nostage", returns={"ok": 1})
    _goal, plan = _goal_with_plan(db, project, monkeypatch, ["b3_nostage"], session="b3nostage")
    job = mk_job(db, project.id, status="succeeded")
    step = _steps(db, plan.plan_id)[0]
    step.status = "waiting_job"
    step.job_id = job.id
    db.commit()

    assert bridge_job_event(db, job_id=job.id, event_type="job.succeeded") == 0
    db.expire_all()
    step = db.get(AgentPlanStep, step.id)
    assert step.resume_requested_at is not None
    assert step.resume_reason == "job_terminal"


def test_retry_event_does_not_mark(db, project, monkeypatch):
    fake_tool(monkeypatch, "b3_retry", returns={"ok": 1})
    _goal, plan = _goal_with_plan(db, project, monkeypatch, ["b3_retry"], session="b3retry")
    job = mk_job(db, project.id, status="running")
    step = _steps(db, plan.plan_id)[0]
    step.status = "waiting_job"
    step.job_id = job.id
    db.commit()

    bridge_job_event(db, job_id=job.id, event_type="job.retry", payload={"attempt": 2})
    db.commit()
    db.expire_all()
    assert db.get(AgentPlanStep, step.id).resume_requested_at is None


def test_early_marker_cleared_if_job_running(db, project, monkeypatch, rt_sessionmaker):
    fake_tool(monkeypatch, "b3_early", returns={"ok": 1})
    goal, plan = _goal_with_plan(db, project, monkeypatch, ["b3_early"], session="b3early")
    job = mk_job(db, project.id, status="running")
    step = _steps(db, plan.plan_id)[0]
    step.status = "waiting_job"
    step.job_id = job.id
    from datetime import datetime

    step.resume_requested_at = datetime.now()
    step.resume_reason = "job_terminal"
    db.commit()

    result = tick(rt_sessionmaker, goal.goal_id, trigger="worker_sweep")
    db.expire_all()
    step = db.get(AgentPlanStep, step.id)
    assert step.status == "waiting_job"
    assert step.resume_requested_at is None
    assert result.lifecycle_state == "waiting_job"


def test_bridge_does_not_call_runtime(db, project, monkeypatch):
    """恢复只在 Worker 线程里发生：bridge 自身绝不调用 tick / Runtime。"""
    from app.services import agent_runtime as rt_mod

    calls = {"tick": 0}
    real_tick = rt_mod.tick

    def _counting(*args, **kwargs):
        calls["tick"] += 1
        return real_tick(*args, **kwargs)

    monkeypatch.setattr(rt_mod, "tick", _counting)

    fake_tool(monkeypatch, "b3_nort", returns={"ok": 1})
    _goal, plan = _goal_with_plan(db, project, monkeypatch, ["b3_nort"], session="b3nort")
    job = mk_job(db, project.id, status="succeeded")
    step = _steps(db, plan.plan_id)[0]
    step.status = "waiting_job"
    step.job_id = job.id
    db.commit()

    bridge_job_event(db, job_id=job.id, event_type="job.finished", payload={"status": "succeeded"})
    db.commit()
    assert calls["tick"] == 0