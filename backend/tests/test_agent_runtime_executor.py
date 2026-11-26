"""A4 验收测试：Executor 只走 execute_tool 网关（P0-5）。"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.services.execution_manager import execute_step
from tests.agent_runtime_fixtures import fake_tool, mk_job, mk_project
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


# ── A5 / P0-6：结果分流（Job → waiting_job，审批 → waiting_approval）───────

def _plan_steps(db, plan):
    from app.models.agent import AgentPlanStep

    return db.scalars(
        select(AgentPlanStep)
        .where(AgentPlanStep.plan_id == plan.plan_id)
        .order_by(AgentPlanStep.step_index)
    ).all()


def test_job_result_enters_waiting_job(db, project, monkeypatch):
    from app.models.task import TaskStage

    job = mk_job(db, project.id, status="queued")
    fake_tool(monkeypatch, "fake_job", returns={"job_id": job.id})
    goal, plan = _plan_with(db, monkeypatch, ["fake_job"], project)
    step = _first_step(db, plan)

    result = execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    assert step.status == "waiting_job", result
    assert step.job_id == job.id
    stage = db.scalars(select(TaskStage).where(TaskStage.job_id == job.id)).first()
    assert stage is not None, "Job 必须挂到 TaskStage 上，bridge_job_event 才能闭环"


def test_job_already_succeeded_converges(db, project, monkeypatch):
    job = mk_job(db, project.id, status="succeeded")
    fake_tool(monkeypatch, "fake_job_done", returns={"job_id": job.id})
    goal, plan = _plan_with(db, monkeypatch, ["fake_job_done"], project)
    step = _first_step(db, plan)

    execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    assert step.status == "succeeded"


def test_confirmation_result_enters_waiting_approval(db, project, monkeypatch):
    from app.models.agent import AgentConfirmation

    confirmation = AgentConfirmation(
        id="acf_a5pending0000000000000001",
        task_id=None,
        tool_name="fake_confirm",
        args={},
        status="pending",
        session_id="a5",
        project_id=project.id,
    )
    db.add(confirmation)
    db.flush()

    fake_tool(monkeypatch, "fake_confirm", returns={"confirmation_id": confirmation.id})
    goal, plan = _plan_with(db, monkeypatch, ["fake_confirm"], project)
    step = _first_step(db, plan)

    execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    assert step.status == "waiting_approval"
    assert step.confirmation_id == confirmation.id


def test_batch_confirmation_ids_all_stored(db, project, monkeypatch):
    from app.models.agent import AgentConfirmation

    ids = ["acf_a5batch00000000000000001", "acf_a5batch00000000000000002"]
    for cid in ids:
        db.add(
            AgentConfirmation(
                id=cid, task_id=None, tool_name="fake_batch", args={},
                status="pending", session_id="a5", project_id=project.id,
            )
        )
    db.flush()

    fake_tool(monkeypatch, "fake_batch", returns={"confirmation_ids": list(ids)})
    goal, plan = _plan_with(db, monkeypatch, ["fake_batch"], project)
    step = _first_step(db, plan)

    execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    assert step.status == "waiting_approval"
    assert step.confirmation_id == ids[0]
    assert step.result["confirmation_ids"] == ids


def test_approval_required_maps_to_waiting_approval(db, project, monkeypatch):
    fake_tool(
        monkeypatch, "fake_needs_approval", returns={"ok": 1},
        spec_overrides={"requires_approval": True},
    )
    goal, plan = _plan_with(db, monkeypatch, ["fake_needs_approval"], project)
    step = _first_step(db, plan)

    execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    assert step.status == "waiting_approval"
    assert step.error_code == "approval_required"


def test_parallel_branch_not_blocked(db, project, monkeypatch):
    """A 返回 job_id，B 是同步工具：循环返回 waiting_job 时 B 必须已经 succeeded。"""
    from app.services.agent_runtime import run_agent_loop
    from tests.test_agent_runtime_tx import _fixed_plan
    from app.services import planner as planner_mod

    job = mk_job(db, project.id, status="queued")
    fake_tool(monkeypatch, "fake_par_job", returns={"job_id": job.id})
    fake_tool(monkeypatch, "fake_par_sync", returns={"ok": 1})

    def _build(db_, *, goal_id, description, project_id=None, previous_plan=None):
        plan = planner_mod.Plan(
            plan_id=planner_mod.agent_plan_id(),
            goal_id=goal_id,
            revision=(previous_plan.revision + 1) if previous_plan else 1,
            metadata={"description": description, "project_id": project_id},
        )
        plan.steps.append(
            planner_mod.PlanStep(
                step_key="a", tool_name="fake_par_job", description="并行 A",
                args={}, estimated_timeout=30,
            )
        )
        plan.steps.append(
            planner_mod.PlanStep(
                step_key="b", tool_name="fake_par_sync", description="并行 B",
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
        db, session_id="a5par", description="并行分支测试", project_id=project.id
    )
    assert result.status == "waiting_job", (result.status, result.message, result.errors)

    from app.models.agent import AgentPlanStep

    steps = {
        s.tool_name: s
        for s in db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == result.plan_id)
        ).all()
    }
    assert steps["fake_par_job"].status == "waiting_job"
    assert steps["fake_par_sync"].status == "succeeded", "等待的分支不能阻塞无关的就绪分支"
