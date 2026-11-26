"""A1 事务边界验收测试（P0-1）。

验证：
- Runtime 对外入口负责提交，新开连接能读到 Goal / Plan / Step / 日志
- 中途崩溃保留已完成步骤，且不向上抛异常
- 内部函数只 flush、不 commit
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.services.agent_runtime import run_agent_loop
from app.services import planner as planner_mod

from tests.agent_runtime_fixtures import fake_tool, mk_project


@pytest.fixture()
def project(db):
    return mk_project(db, with_script=True, with_parse=True)


@pytest.fixture()
def committed_project(db):
    """跨 Session 用例需要先提交，否则 SQLite 写锁会让新 Session 报 database is locked。"""
    p = mk_project(db, with_script=True, with_parse=True)
    db.commit()
    return p


def _fixed_plan(monkeypatch, goal_id: str, tools: list[str]):
    """让 planner.build_and_save_plan 产出只含指定工具的计划。"""
    def _build(db, *, goal_id, description, project_id=None, previous_plan=None):
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
            planner_mod.save_plan(db, plan, supersede_previous=True)
            planner_mod.save_plan_steps(db, plan)
        return plan, report

    monkeypatch.setattr(planner_mod, "build_and_save_plan", _build)


def test_loop_persists_across_sessions(db, project, monkeypatch, fresh_session):
    fake_tool(monkeypatch, "fake_ok", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_ok"])

    result = run_agent_loop(
        db, session_id="tx1", description="执行测试步骤", project_id=project.id
    )
    assert result.status == "completed", (result.status, result.message, result.errors)
    assert result.completed_steps == 1
    db.close()

    from app.models.agent import AgentExecutionLog, AgentGoal, AgentPlan, AgentPlanStep

    other = fresh_session()
    assert other.scalar(select(AgentGoal).where(AgentGoal.id == result.goal_id)) is not None
    assert other.scalar(select(AgentPlan).where(AgentPlan.id == result.plan_id)) is not None
    steps = other.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id == result.plan_id)
    ).all()
    assert steps and all(s.status == "succeeded" for s in steps)
    logs = other.scalars(
        select(AgentExecutionLog).where(AgentExecutionLog.goal_id == result.goal_id)
    ).all()
    assert logs
    other.close()


def test_crash_midway_keeps_completed_steps(db, project, monkeypatch, fresh_session):
    """第二个步骤执行时抛出未捕获异常：第一步已提交，第二步标记 failed，不向上抛。"""
    fake_tool(monkeypatch, "fake_first", returns={"ok": 1})
    fake_tool(monkeypatch, "fake_second", returns={"ok": 2})
    _fixed_plan(monkeypatch, "", ["fake_first", "fake_second"])

    from app.services import execution_manager as em

    real_execute = em.execute_step
    calls = {"n": 0}

    def _flaky_execute(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(em, "execute_step", _flaky_execute)

    result = run_agent_loop(
        db, session_id="tx2", description="执行测试步骤", project_id=project.id
    )
    assert result.status == "failed", (result.status, result.message)
    assert "RuntimeError" in result.message
    assert calls["n"] == 2, "第二个步骤应被尝试执行并抛出异常"
    db.close()

    from app.models.agent import AgentPlanStep

    other = fresh_session()
    steps = {
        s.step_index: s
        for s in other.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == result.plan_id)
        ).all()
    }
    assert steps[0].status == "succeeded", "第一步已完成并提交，崩溃不应回滚它"
    assert steps[1].status != "succeeded", "第二个步骤未成功，不得计入完成"
    other.close()


def test_internal_functions_do_not_commit(db, project, monkeypatch):
    calls = {"n": 0}
    real_commit = db.commit

    def _counting_commit():
        calls["n"] += 1
        real_commit()

    monkeypatch.setattr(db, "commit", _counting_commit)

    from app.services.goal_manager import get_or_create_goal
    from app.services.planner import build_and_save_plan

    goal = get_or_create_goal(
        db, session_id="tx3", description="检查项目进度", project_id=project.id
    )
    build_and_save_plan(db, goal_id=goal.goal_id, description="检查项目进度", project_id=project.id)
    assert calls["n"] == 0, "内部函数不应 commit"

    from app.models.agent import AgentPlanStep
    from app.services.execution_manager import execute_step

    step = db.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id.is_not(None))
    ).first()
    if step is not None:
        execute_step(db, step_id=step.id, plan_id=step.plan_id, goal_id=goal.goal_id)
        assert calls["n"] == 0, "execution_manager.execute_step 不应 commit"


def test_run_agent_loop_in_new_session_owns_session(committed_project, rt_sessionmaker, monkeypatch):
    from app.services.agent_runtime import run_agent_loop_in_new_session

    fake_tool(monkeypatch, "fake_ok", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_ok"])

    result = run_agent_loop_in_new_session(
        session_factory=rt_sessionmaker,
        session_id="tx4",
        description="执行测试步骤",
        project_id=committed_project.id,
    )
    assert result.goal_id, (result.status, result.message, result.errors)
    assert result.status in {"completed", "waiting", "waiting_approval", "failed"}
