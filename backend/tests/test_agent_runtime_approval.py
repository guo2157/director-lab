"""A7 验收测试：审批按步骤绑定（P0-8）。

覆盖：
- 计划里 step.requires_approval=True 时即使工具 spec 不要求审批也进入 waiting_approval
- 一次审批只放行它绑定的那个步骤
- confirmation 与步骤不匹配时拒绝恢复
- 驳回后进入 waiting_user
- 同一审批不能被重复消费
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.services import planner as planner_mod
from app.services.agent_runtime import resume_after_approval, run_agent_loop
from app.services.execution_manager import execute_step
from tests.agent_runtime_fixtures import fake_tool, mk_project
from tests.test_agent_runtime_tx import _fixed_plan


@pytest.fixture()
def project(db):
    return mk_project(db, with_script=True, with_parse=True)


def _plan(db, monkeypatch, tools, project, *, session="a7"):
    from app.services.goal_manager import get_or_create_goal

    goal = get_or_create_goal(
        db, session_id=session, description="审批测试", project_id=project.id
    )
    # 必须先打补丁再取模块属性，否则绑定的仍是原始函数
    _fixed_plan(monkeypatch, "", tools)
    plan, report = planner_mod.build_and_save_plan(
        db, goal_id=goal.goal_id, description="审批测试", project_id=project.id
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


def _confirmation(db, *, cid, tool_name, project_id, status="pending", step_id=None):
    from app.models.agent import AgentConfirmation

    row = AgentConfirmation(
        id=cid,
        task_id=None,
        tool_name=tool_name,
        args={},
        status=status,
        session_id="a7",
        project_id=project_id,
    )
    db.add(row)
    db.flush()
    return row


def test_step_requires_approval_flag_respected(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a7_low", returns={"ok": 1})
    goal, plan = _plan(db, monkeypatch, ["fake_a7_low"], project)
    step = _steps(db, plan)[0]
    step.requires_approval = True
    db.flush()

    result = execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    assert step.status == "waiting_approval", result
    assert result["policy"] == "REQUIRE_CONFIRMATION"


def test_one_approval_releases_only_its_step(db, project, monkeypatch):
    calls = {"s1": 0, "s2": 0}

    def _count_s1(**kwargs):
        calls["s1"] += 1
        return {"ok": 1}

    def _count_s2(**kwargs):
        calls["s2"] += 1
        return {"ok": 1}

    fake_tool(monkeypatch, "fake_a7_s1", returns=_count_s1, spec_overrides={"risk_level": "high"})
    fake_tool(monkeypatch, "fake_a7_s2", returns=_count_s2, spec_overrides={"risk_level": "high"})

    # 两个互相独立的步骤，一次循环里都会进入 waiting_approval
    def _build(db_, *, goal_id, description, project_id=None, previous_plan=None):
        plan = planner_mod.Plan(
            plan_id=planner_mod.agent_plan_id(),
            goal_id=goal_id,
            revision=(previous_plan.revision + 1) if previous_plan else 1,
            metadata={"description": description, "project_id": project_id},
        )
        for name in ("fake_a7_s1", "fake_a7_s2"):
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

    first = run_agent_loop(
        db, session_id="a7two", description="两个高风险步骤", project_id=project.id
    )
    assert first.status == "waiting_approval", (first.status, first.message)

    from app.models.agent import AgentPlanStep

    steps = {
        s.tool_name: s
        for s in db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == first.plan_id)
        ).all()
    }
    s1, s2 = steps["fake_a7_s1"], steps["fake_a7_s2"]
    assert s1.status == "waiting_approval" and s2.status == "waiting_approval"
    assert calls == {"s1": 0, "s2": 0}

    c1 = _confirmation(
        db, cid="acf_a7s1approved00000000000001",
        tool_name="fake_a7_s1", project_id=project.id, status="approved",
    )
    s1.confirmation_id = c1.id
    db.flush()

    outcome = resume_after_approval(
        db, goal_id=first.goal_id, step_id=s1.id,
        confirmation_id=c1.id, project_id=project.id,
    )
    assert outcome["resumed"] is True, outcome
    assert calls["s1"] == 1
    assert calls["s2"] == 0, "一次审批不能放行其他步骤"

    s1_after = db.get(type(s1), s1.id)
    s2_after = db.get(type(s2), s2.id)
    assert s1_after.status == "succeeded"
    assert s2_after.status == "waiting_approval"


def test_confirmation_mismatch_rejected(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a7_mm", returns={"ok": 1}, spec_overrides={"requires_approval": True})
    goal, plan = _plan(db, monkeypatch, ["fake_a7_mm"], project)
    step = _steps(db, plan)[0]

    execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    assert step.status == "waiting_approval"

    other = _confirmation(
        db, cid="acf_a7other000000000000000001",
        tool_name="fake_a7_mm", project_id=project.id, status="approved",
    )
    step.confirmation_id = "acf_a7bound000000000000000001"
    db.flush()

    outcome = resume_after_approval(
        db, goal_id=goal.goal_id, step_id=step.id,
        confirmation_id=other.id, project_id=project.id,
    )
    assert outcome["resumed"] is False
    assert outcome["error"] == "confirmation_mismatch"
    assert step.status == "waiting_approval"


def test_reject_goes_waiting_user(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a7_reject", returns={"ok": 1}, spec_overrides={"requires_approval": True})
    goal, plan = _plan(db, monkeypatch, ["fake_a7_reject"], project)
    step = _steps(db, plan)[0]

    execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    confirmation = _confirmation(
        db, cid="acf_a7rejected0000000000000001",
        tool_name="fake_a7_reject", project_id=project.id, status="rejected",
    )
    step.confirmation_id = confirmation.id
    db.flush()

    outcome = resume_after_approval(
        db, goal_id=goal.goal_id, step_id=step.id,
        confirmation_id=confirmation.id, project_id=project.id,
    )
    assert outcome["resumed"] is True
    assert outcome["status"] == "waiting_user"
    assert step.status == "waiting_user"
    assert step.error_code == "approval_rejected"


def test_approval_not_reusable(db, project, monkeypatch):
    calls = {"n": 0}

    def _count(**kwargs):
        calls["n"] += 1
        return {"ok": 1}

    fake_tool(
        monkeypatch, "fake_a7_once", returns=_count, spec_overrides={"requires_approval": True}
    )
    goal, plan = _plan(db, monkeypatch, ["fake_a7_once"], project)
    step = _steps(db, plan)[0]

    execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    confirmation = _confirmation(
        db, cid="acf_a7once0000000000000000001",
        tool_name="fake_a7_once", project_id=project.id, status="approved",
    )
    step.confirmation_id = confirmation.id
    db.flush()

    first = resume_after_approval(
        db, goal_id=goal.goal_id, step_id=step.id,
        confirmation_id=confirmation.id, project_id=project.id,
    )
    assert first["resumed"] is True
    assert calls["n"] == 1

    # 再次恢复：步骤已不是 waiting_approval，直接返回 step_not_waiting，工具不再执行
    second = resume_after_approval(
        db, goal_id=goal.goal_id, step_id=step.id,
        confirmation_id=confirmation.id, project_id=project.id,
    )
    assert second["resumed"] is False
    assert second["error"] == "step_not_waiting"
    assert calls["n"] == 1, "同一审批不能重复执行"


def test_approved_confirmation_consumed_once(db, project, monkeypatch):
    """步骤执行后记录 executed_confirmation_id，审批不可被第二次执行消费。"""
    calls = {"n": 0}

    def _count(**kwargs):
        calls["n"] += 1
        return {"ok": 1}

    fake_tool(
        monkeypatch, "fake_a7_consume", returns=_count,
        spec_overrides={"requires_approval": True},
    )
    goal, plan = _plan(db, monkeypatch, ["fake_a7_consume"], project)
    step = _steps(db, plan)[0]
    confirmation = _confirmation(
        db, cid="acf_a7consume00000000000000001",
        tool_name="fake_a7_consume", project_id=project.id, status="approved",
    )
    step.confirmation_id = confirmation.id
    db.flush()

    execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    assert calls["n"] == 1
    assert step.result["executed_confirmation_id"] == confirmation.id

    # 把步骤重置为 pending 再执行一次：审批已被消费，必须回到 waiting_approval
    step.status = "pending"
    db.flush()
    execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    assert step.status == "waiting_approval"
    assert calls["n"] == 1
