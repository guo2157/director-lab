"""Phase B B5 验收：agent-resume 认领 / 兜底扫描 / Worker 线程生命周期。"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.agent import AgentConfirmation, AgentGoal, AgentPlanStep
from app.services import agent_resume
from app.services import planner as planner_mod
from tests.agent_runtime_fixtures import fake_tool, mk_job, mk_project


@pytest.fixture()
def project(db):
    p = mk_project(db, with_script=True, with_parse=True)
    db.commit()
    return p


def _plan(monkeypatch, tools):
    def _build(db_, *, goal_id, description, project_id=None, previous_plan=None):
        plan = planner_mod.Plan(
            plan_id=planner_mod.agent_plan_id(),
            goal_id=goal_id,
            revision=(previous_plan.revision + 1) if previous_plan else 1,
            metadata={"description": description, "project_id": project_id},
        )
        for idx, name in enumerate(tools):
            plan.steps.append(
                planner_mod.PlanStep(
                    step_key=f"s{idx}", tool_name=name, description=f"步骤 {idx}",
                    args={}, depends_on=[f"s{idx - 1}"] if idx else [],
                    estimated_timeout=30,
                )
            )
        report = planner_mod.validate_plan(plan, project_id=project_id)
        if report.passed:
            planner_mod.save_plan(db_, plan, supersede_previous=True)
            planner_mod.save_plan_steps(db_, plan)
        return plan, report

    monkeypatch.setattr(planner_mod, "build_and_save_plan", _build)


def _goal(db, project, monkeypatch, tools, *, session):
    from app.services.goal_manager import get_or_create_goal

    _plan(monkeypatch, tools)
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


def _mark(db, step, reason="job_terminal"):
    step.resume_requested_at = datetime.now()
    step.resume_reason = reason
    db.commit()


def test_process_marked_ticks_each_goal_once(db, project, monkeypatch, rt_sessionmaker):
    calls = {"n": 0}

    def _count(**_kw):
        calls["n"] += 1
        return {"ok": 1}

    fake_tool(monkeypatch, "b5_ok", returns=_count)
    _unused_goal, plan = _goal(db, project, monkeypatch, ["b5_ok", "b5_ok", "b5_ok"], session="b5once")
    for step in _steps(db, plan.plan_id):
        _mark(db, step, reason="sweep")

    tick_calls = {"n": 0}
    from app.services import agent_runtime as rt_mod

    real_tick = rt_mod.tick

    def _counting(*args, **kwargs):
        tick_calls["n"] += 1
        return real_tick(*args, **kwargs)

    monkeypatch.setattr(rt_mod, "tick", _counting)
    processed = agent_resume.process_marked(rt_sessionmaker, limit=5)
    assert processed == 1
    assert tick_calls["n"] == 1, "同一 Goal 的多个标记只应 tick 一次"
    db.expire_all()
    assert all(s.status == "succeeded" for s in _steps(db, plan.plan_id))


def test_concurrent_process_marked_no_double_tick(db, project, monkeypatch, rt_sessionmaker):
    fake_tool(monkeypatch, "b5_conc", returns={"ok": 1})
    _unused_goal, plan = _goal(db, project, monkeypatch, ["b5_conc", "b5_conc"], session="b5conc")
    for step in _steps(db, plan.plan_id):
        _mark(db, step, reason="sweep")

    from app.services import agent_runtime as rt_mod

    real_tick = rt_mod.tick
    tick_calls = {"n": 0}
    lock = threading.Lock()

    def _counting(*args, **kwargs):
        with lock:
            tick_calls["n"] += 1
        time.sleep(0.2)
        return real_tick(*args, **kwargs)

    monkeypatch.setattr(rt_mod, "tick", _counting)

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(agent_resume.process_marked(rt_sessionmaker)))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sum(results) <= 1, results
    assert tick_calls["n"] <= 1, "并发认领不应重复 tick"


def test_sweep_recovers_lost_marker(db, project, monkeypatch, rt_sessionmaker):
    fake_tool(monkeypatch, "b5_sweep", returns={"ok": 1})
    _unused_goal, plan = _goal(db, project, monkeypatch, ["b5_sweep"], session="b5sweep")
    job = mk_job(db, project.id, status="succeeded")
    step = _steps(db, plan.plan_id)[0]
    step.status = "waiting_job"
    step.job_id = job.id
    db.commit()

    out = agent_resume.sweep(rt_sessionmaker)
    assert out["marked_job"] == 1, out

    db.expire_all()
    assert _steps(db, plan.plan_id)[0].resume_requested_at is not None
    agent_resume.process_marked(rt_sessionmaker)
    db.expire_all()
    assert _steps(db, plan.plan_id)[0].status == "succeeded"


def test_sweep_recovers_crashed_tick(db, project, monkeypatch, rt_sessionmaker):
    fake_tool(monkeypatch, "b5_crash", returns={"ok": 1})
    goal, plan = _goal(db, project, monkeypatch, ["b5_crash"], session="b5crash")

    row = db.get(AgentGoal, goal.goal_id)
    row.lifecycle_state = "running"
    row.last_tick_at = datetime.now() - timedelta(minutes=10)
    db.commit()

    out = agent_resume.sweep(rt_sessionmaker)
    assert goal.goal_id in out["ticked"], out
    db.expire_all()
    assert _steps(db, plan.plan_id)[0].status == "succeeded"


def test_sweep_marks_resolved_approval(db, project, monkeypatch, rt_sessionmaker):
    fake_tool(monkeypatch, "b5_appr", returns={"ok": 1})
    _unused_goal, plan = _goal(db, project, monkeypatch, ["b5_appr"], session="b5appr")
    step = _steps(db, plan.plan_id)[0]
    confirmation = AgentConfirmation(
        id="acf_b5sweep00000000000000001",
        task_id=None,
        tool_name="b5_appr",
        args={},
        status="approved",
        session_id="b5appr",
        project_id=project.id,
    )
    db.add(confirmation)
    step.status = "waiting_approval"
    step.confirmation_id = confirmation.id
    db.commit()

    out = agent_resume.sweep(rt_sessionmaker)
    assert out["marked_approval"] == 1, out


def test_worker_thread_start_stop(monkeypatch):
    """线程能启动，并在 stop_event 置位后 5 秒内退出。"""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=agent_resume.run_resume_loop,
        args=(stop_event, None),
        kwargs={"poll_interval": 0.05, "sweep_interval": 60.0},
        daemon=True,
    )
    thread.start()
    time.sleep(0.2)
    assert thread.is_alive()
    stop_event.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), "停止后线程必须在 5 秒内退出"


def test_resume_thread_respects_config_flag(monkeypatch):
    from app.core.config import clear_settings_cache

    monkeypatch.setenv("DLAB_AGENT_RESUME_ENABLED", "false")
    clear_settings_cache()
    try:
        assert agent_resume.start_resume_thread() is None
    finally:
        monkeypatch.delenv("DLAB_AGENT_RESUME_ENABLED", raising=False)
        clear_settings_cache()


def test_chat_flag_off_means_no_marked_steps(db, project, monkeypatch, rt_sessionmaker):
    """agent_runtime_chat_enabled 关闭时 /chat 不产生 Goal，resume 线程无步骤可处理。"""
    from app.core.config import clear_settings_cache, get_settings

    monkeypatch.setenv("DLAB_AGENT_RUNTIME_CHAT_ENABLED", "false")
    clear_settings_cache()
    try:
        assert get_settings().agent_runtime_chat_enabled is False
        assert agent_resume.process_marked(rt_sessionmaker) == 0
    finally:
        monkeypatch.delenv("DLAB_AGENT_RUNTIME_CHAT_ENABLED", raising=False)
        clear_settings_cache()