"""Phase B B1 验收：tick 幂等、步数上限、锁、暂停、异常释放锁、生命周期落库。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.services import planner as planner_mod
from app.services.agent_runtime import tick
from app.services.execution_manager import _acquire_goal_lock, _release_goal_lock
from tests.agent_runtime_fixtures import fake_tool, mk_job, mk_project


@pytest.fixture()
def project(db):
    p = mk_project(db, with_script=True, with_parse=True)
    db.commit()
    return p


def _build_plan(monkeypatch, tools):
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
                    step_key=f"s{idx}",
                    tool_name=name,
                    description=f"步骤 {idx}",
                    args={},
                    depends_on=[f"s{idx - 1}"] if idx else [],
                    estimated_timeout=30,
                )
            )
        report = planner_mod.validate_plan(plan, project_id=project_id)
        if report.passed:
            planner_mod.save_plan(db_, plan, supersede_previous=True)
            planner_mod.save_plan_steps(db_, plan)
        return plan, report

    monkeypatch.setattr(planner_mod, "build_and_save_plan", _build)


def _goal_with_plan(db, project, monkeypatch, tools, *, session="b1"):
    """只建 Goal + Plan（不执行），保证 tick 是唯一的推进者。"""
    from app.services.goal_manager import get_or_create_goal

    _build_plan(monkeypatch, tools)
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
    from app.models.agent import AgentPlanStep

    return db.scalars(
        select(AgentPlanStep)
        .where(AgentPlanStep.plan_id == plan_id)
        .order_by(AgentPlanStep.step_index)
    ).all()


def test_tick_is_idempotent(db, project, monkeypatch, rt_sessionmaker):
    """waiting_job 的 Goal 连续 tick 3 次：工具调用计数与步骤状态都不变。"""
    calls = {"n": 0}

    def _count(**_kwargs):
        calls["n"] += 1
        return {"ok": 1}

    fake_tool(monkeypatch, "b1_job", returns=_count)
    goal, plan = _goal_with_plan(db, project, monkeypatch, ["b1_job"], session="b1idem")

    job = mk_job(db, project.id, status="queued")
    step = _steps(db, plan.plan_id)[0]
    step.status = "waiting_job"
    step.job_id = job.id
    db.commit()

    for _ in range(3):
        result = tick(rt_sessionmaker, goal.goal_id, trigger="manual")
        assert result.busy is False
        assert result.lifecycle_state == "waiting_job", result.to_dict()

    db.expire_all()
    assert calls["n"] == 0, "tick 不应执行 waiting_job 的步骤"
    assert _steps(db, plan.plan_id)[0].status == "waiting_job"


def test_tick_respects_max_steps(db, project, monkeypatch, rt_sessionmaker):
    """12 个串行步骤、上限 10：第一次推进 10 步，第二次推进 2 步并 completed。"""
    fake_tool(monkeypatch, "b1_sync", returns={"ok": 1})
    goal, plan = _goal_with_plan(db, project, monkeypatch, ["b1_sync"] * 12, session="b1max")

    from app.core.config import clear_settings_cache

    monkeypatch.setenv("DLAB_AGENT_MAX_STEPS_PER_TICK", "10")
    clear_settings_cache()
    try:
        r1 = tick(rt_sessionmaker, goal.goal_id, trigger="manual")
        db.expire_all()
        succeeded = sum(1 for s in _steps(db, plan.plan_id) if s.status == "succeeded")
        assert succeeded == 10, succeeded
        assert r1.lifecycle_state == "running", r1.to_dict()

        r2 = tick(rt_sessionmaker, goal.goal_id, trigger="manual")
        db.expire_all()
        assert r2.lifecycle_state == "completed", r2.to_dict()
        assert all(s.status == "succeeded" for s in _steps(db, plan.plan_id))
    finally:
        monkeypatch.delenv("DLAB_AGENT_MAX_STEPS_PER_TICK", raising=False)
        clear_settings_cache()


def test_tick_busy_when_locked(db, project, monkeypatch, rt_sessionmaker):
    fake_tool(monkeypatch, "b1_busy", returns={"ok": 1})
    goal, plan = _goal_with_plan(db, project, monkeypatch, ["b1_busy"], session="b1busy")

    assert _acquire_goal_lock(db, goal.goal_id) is True
    db.commit()
    try:
        result = tick(rt_sessionmaker, goal.goal_id, trigger="manual")
        assert result.busy is True
        assert result.status == "busy"
        assert _steps(db, plan.plan_id)[0].status == "pending"
    finally:
        _release_goal_lock(db, goal.goal_id)
        db.commit()


def test_tick_paused_goal_noop(db, project, monkeypatch, rt_sessionmaker):
    from app.models.agent import AgentGoal

    fake_tool(monkeypatch, "b1_paused", returns={"ok": 1})
    goal, plan = _goal_with_plan(db, project, monkeypatch, ["b1_paused"], session="b1pause")

    row = db.get(AgentGoal, goal.goal_id)
    row.status = "paused"
    row.lifecycle_state = "paused"
    db.commit()

    result = tick(rt_sessionmaker, goal.goal_id, trigger="manual")
    assert result.busy is False
    assert result.status == "paused"
    assert _steps(db, plan.plan_id)[0].status == "pending"


def test_tick_exception_releases_lock(db, project, monkeypatch, rt_sessionmaker):
    from app.services import agent_runtime as rt

    fake_tool(monkeypatch, "b1_err", returns={"ok": 1})
    goal, plan = _goal_with_plan(db, project, monkeypatch, ["b1_err"], session="b1err")

    real = rt._execute_plan_loop

    def _boom(*args, **kwargs):
        raise RuntimeError("tick boom")

    monkeypatch.setattr(rt, "_execute_plan_loop", _boom)
    bad = tick(rt_sessionmaker, goal.goal_id, trigger="manual")
    assert bad.status == "failed"
    assert "tick boom" in bad.message

    monkeypatch.setattr(rt, "_execute_plan_loop", real)
    good = tick(rt_sessionmaker, goal.goal_id, trigger="manual")
    assert good.busy is False, "异常后 Goal 锁必须已释放"
    assert good.lifecycle_state == "completed", good.to_dict()


def test_lifecycle_written_to_goal_and_agent_task(db, project, monkeypatch, rt_sessionmaker):
    from app.models.agent import AgentGoal, AgentMessage, AgentTask
    from app.services.goal_manager import get_or_create_goal
    from app.services.parent_agent import create_task_from_intent

    fake_tool(monkeypatch, "b1_life", returns={"ok": 1})
    _build_plan(monkeypatch, ["b1_life"])

    class _Source:
        goal = "生成资产"

    class _Resolved:
        intent = "generate_request"
        risk_level = "low"
        confidence = 1.0
        target = "assets"
        resolution_path = "explicit"
        source = _Source()

    msg = AgentMessage(
        id="amsg_b1life000000000000000001", session_id="b1life",
        role="user", content="生成资产", seq=1,
    )
    db.add(msg)
    db.flush()
    agent_task = create_task_from_intent(db, "b1life", msg, _Resolved())
    assert agent_task is not None

    goal = get_or_create_goal(
        db, session_id="b1life", description="执行测试步骤", project_id=project.id
    )
    row = db.get(AgentGoal, goal.goal_id)
    row.agent_task_id = agent_task.id
    db.commit()

    plan, report = planner_mod.build_and_save_plan(
        db, goal_id=goal.goal_id, description="执行测试步骤", project_id=project.id
    )
    assert report.passed, report.errors
    db.commit()

    result = tick(rt_sessionmaker, goal.goal_id, trigger="manual")
    assert result.lifecycle_state == "completed", result.to_dict()
    db.expire_all()

    goal_row = db.get(AgentGoal, goal.goal_id)
    assert goal_row.lifecycle_state == "completed"
    assert goal_row.last_tick_at is not None
    assert db.get(AgentTask, agent_task.id).lifecycle_state == "completed"


def test_tick_unknown_goal(db, rt_sessionmaker):
    result = tick(rt_sessionmaker, "gol_missing", trigger="manual")
    assert result.status == "failed"
    assert result.lifecycle_state == "unknown"