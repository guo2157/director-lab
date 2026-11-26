"""Phase B B4 验收：审批自动申请、裁决后恢复、自动执行、拒绝进入 waiting_user。"""
from __future__ import annotations

import hashlib
from datetime import datetime

import pytest
from sqlalchemy import select

from app.models import Asset, Job, Scene, ScriptVersion, Shot, Workflow, ids
from app.models.agent import AgentConfirmation, AgentPlanStep
from app.services import approvals
from app.services import planner as planner_mod
from app.services.agent_runtime import tick
from tests.agent_runtime_fixtures import fake_tool, mk_project


@pytest.fixture()
def project(db):
    """可申请生成的已解析项目：剧本 + 解析 + 场景基准图 + 默认图像工作流。"""
    p = mk_project(db, with_script=True, with_parse=True)
    now = datetime.now()
    script = db.get(ScriptVersion, p.current_script_version_id)
    script_hash = hashlib.sha256(script.content_snapshot.encode("utf-8")).hexdigest()
    script.content_hash = script_hash
    p.estimated_text_hash = script_hash
    db.add(Job(id=ids.job_id(), project_id=p.id, job_type="parse", status="succeeded", created_at=now))
    scene = Scene(id=ids.scene_id(), project_id=p.id, episode=1, seq=1, title="场景一")
    db.add(scene)
    db.flush()
    # 有镜头项目才会推进到 keyframes 阶段（scene 目标要求至少 scenes 阶段）
    db.add(
        Shot(
            id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1,
            shot_kind="normal", duration=3,
        )
    )
    db.flush()
    baseline = Asset(
        id=ids.asset_id(), project_id=p.id, asset_type="scene", name="场景一基准图",
        version=1, file_path="placeholder.png", validity="current",
    )
    db.add(baseline)
    db.flush()
    scene.baseline_asset_id = baseline.id
    db.add(
        Workflow(
            id=ids.workflow_id(), name="默认图像工作流", workflow_type="image",
            is_default=True, is_builtin=True, version=1, definition={},
        )
    )
    db.commit()
    return p


def _plan(monkeypatch, steps):
    """steps: [(step_key, tool_name, args)]"""
    def _build(db_, *, goal_id, description, project_id=None, previous_plan=None):
        plan = planner_mod.Plan(
            plan_id=planner_mod.agent_plan_id(),
            goal_id=goal_id,
            revision=(previous_plan.revision + 1) if previous_plan else 1,
            metadata={"description": description, "project_id": project_id},
        )
        for idx, (key, tool, args) in enumerate(steps):
            plan.steps.append(
                planner_mod.PlanStep(
                    step_key=key, tool_name=tool, description=f"步骤 {key}",
                    args=args, depends_on=[], estimated_timeout=30,
                )
            )
        report = planner_mod.validate_plan(plan, project_id=project_id)
        if report.passed:
            planner_mod.save_plan(db_, plan, supersede_previous=True)
            planner_mod.save_plan_steps(db_, plan)
        return plan, report

    monkeypatch.setattr(planner_mod, "build_and_save_plan", _build)


def _goal_with_plan(db, project, monkeypatch, steps, *, session):
    from app.services.goal_manager import get_or_create_goal

    _plan(monkeypatch, steps)
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


def test_request_generate_waits_then_runs_on_approve(db, project, monkeypatch, rt_sessionmaker):
    """request_generate → waiting_approval → 批准打标记 → tick 自动建 Job → waiting_job。"""
    goal, plan = _goal_with_plan(
        db, project, monkeypatch,
        [("s0", "request_generate", {"project_id": project.id, "target": "scene", "entity_id": None})],
        session="b4req",
    )
    # 用一个真实场景实体作为生成目标
    scene = db.scalars(select(Scene).where(Scene.project_id == project.id)).first()
    step = _steps(db, plan.plan_id)[0]
    step.args = {"project_id": project.id, "target": "scene", "entity_id": scene.id}
    db.commit()

    first = tick(rt_sessionmaker, goal.goal_id, trigger="chat")
    assert first.lifecycle_state == "waiting_approval", first.to_dict()

    db.expire_all()
    step = _steps(db, plan.plan_id)[0]
    assert step.status == "waiting_approval"
    assert step.confirmation_id, "审批记录必须绑定到步骤"
    confirmation = db.get(AgentConfirmation, step.confirmation_id)
    assert confirmation.status == "pending"

    approvals.resolve_confirmation(db, confirmation_id=confirmation.id, approve=True, comment="同意")
    db.expire_all()
    step = _steps(db, plan.plan_id)[0]
    assert step.resume_requested_at is not None
    assert step.resume_reason == "approval_resolved"

    second = tick(rt_sessionmaker, goal.goal_id, trigger="worker_resume")
    db.expire_all()
    step = _steps(db, plan.plan_id)[0]
    assert step.status == "waiting_job", (step.status, step.error_code, step.error_message)
    assert step.job_id
    jobs = db.scalars(select(Job).where(Job.project_id == project.id, Job.job_type != "parse")).all()
    assert len(jobs) == 1
    assert jobs[0].id == step.job_id
    assert second.lifecycle_state == "waiting_job", second.to_dict()


def test_user_clicked_execute_first_no_duplicate_job(db, project, monkeypatch, rt_sessionmaker):
    """用户在页面先点执行，再 tick：run_generate 幂等，Job 表只有 1 条。"""
    goal, plan = _goal_with_plan(
        db, project, monkeypatch, [("s0", "request_generate", {"project_id": project.id, "target": "scene"})],
        session="b4dup",
    )
    scene = db.scalars(select(Scene).where(Scene.project_id == project.id)).first()
    step = _steps(db, plan.plan_id)[0]
    step.args = {"project_id": project.id, "target": "scene", "entity_id": scene.id}
    db.commit()

    tick(rt_sessionmaker, goal.goal_id, trigger="chat")
    db.expire_all()
    step = _steps(db, plan.plan_id)[0]
    confirmation_id = step.confirmation_id

    approvals.resolve_confirmation(db, confirmation_id=confirmation_id, approve=True)
    from app.services.agent_tools import execute_tool

    clicked = execute_tool(db, "run_generate", approved=True, confirmation_id=confirmation_id)
    assert clicked.ok, clicked.error_message
    db.commit()

    tick(rt_sessionmaker, goal.goal_id, trigger="worker_resume")
    db.expire_all()
    jobs = db.scalars(select(Job).where(Job.project_id == project.id, Job.job_type != "parse")).all()
    assert len(jobs) == 1, [j.id for j in jobs]
    assert db.get(AgentPlanStep, step.id).job_id == jobs[0].id


def test_reject_goes_waiting_user_with_comment(db, project, monkeypatch, rt_sessionmaker):
    goal, plan = _goal_with_plan(
        db, project, monkeypatch, [("s0", "request_generate", {"project_id": project.id, "target": "scene"})],
        session="b4rej",
    )
    scene = db.scalars(select(Scene).where(Scene.project_id == project.id)).first()
    step = _steps(db, plan.plan_id)[0]
    step.args = {"project_id": project.id, "target": "scene", "entity_id": scene.id}
    db.commit()

    tick(rt_sessionmaker, goal.goal_id, trigger="chat")
    db.expire_all()
    step = _steps(db, plan.plan_id)[0]
    approvals.resolve_confirmation(
        db, confirmation_id=step.confirmation_id, approve=False, comment="风格不符合要求"
    )

    result = tick(rt_sessionmaker, goal.goal_id, trigger="worker_resume")
    db.expire_all()
    step = db.get(AgentPlanStep, step.id)
    assert step.status == "waiting_user"
    assert step.error_code == "approval_rejected"
    assert "风格不符合要求" in (step.error_message or "")
    assert result.lifecycle_state == "waiting_user"


def test_tool_requiring_approval_auto_creates_confirmation(db, project, monkeypatch, rt_sessionmaker):
    """工具本身需要审批：同一步骤 tick 两次只产生 1 条 AgentConfirmation。"""
    fake_tool(
        monkeypatch, "b4_needs_approval",
        returns={"ok": 1},
        spec_overrides={"requires_approval": True, "risk_level": "high", "side_effect": True},
    )
    goal, plan = _goal_with_plan(
        db, project, monkeypatch, [("s0", "b4_needs_approval", {})], session="b4auto"
    )

    first = tick(rt_sessionmaker, goal.goal_id, trigger="chat")
    assert first.lifecycle_state == "waiting_approval", first.to_dict()
    db.expire_all()
    step = _steps(db, plan.plan_id)[0]
    assert step.confirmation_id
    count_1 = db.scalar(
        select(AgentConfirmation).where(AgentConfirmation.tool_name == "b4_needs_approval").with_only_columns(
            __import__("sqlalchemy").func.count()
        )
    )
    assert count_1 == 1

    # 第二次 tick（无新标记）不应重复建审批
    tick(rt_sessionmaker, goal.goal_id, trigger="worker_sweep")
    db.expire_all()
    count_2 = db.scalar(
        select(AgentConfirmation).where(AgentConfirmation.tool_name == "b4_needs_approval").with_only_columns(
            __import__("sqlalchemy").func.count()
        )
    )
    assert count_2 == 1


def test_pipeline_tool_rejected_by_validator(db, project, monkeypatch):
    from app.services.planner import Plan, PlanStep, validate_plan

    plan = Plan(
        plan_id=planner_mod.agent_plan_id(),
        goal_id="gol_x",
        metadata={"description": "含流程工具的非法计划"},
        steps=[PlanStep(step_key="s0", tool_name="compose_video", args={})],
    )
    report = validate_plan(plan, project_id=project.id)
    assert report.passed is False
    assert any(i.check_name == "tool_not_found" for i in report.issues), report.to_dict()


def test_resolve_confirmation_does_not_call_runtime(db, project, monkeypatch):
    """裁决只打标记，绝不在 API 请求里同步推进 Runtime。"""
    from app.services import agent_runtime as rt_mod

    calls = {"tick": 0}
    real_tick = rt_mod.tick

    def _counting(*args, **kwargs):
        calls["tick"] += 1
        return real_tick(*args, **kwargs)

    monkeypatch.setattr(rt_mod, "tick", _counting)

    goal, plan = _goal_with_plan(
        db, project, monkeypatch, [("s0", "request_generate", {"project_id": project.id, "target": "scene"})],
        session="b4nort",
    )
    scene = db.scalars(select(Scene).where(Scene.project_id == project.id)).first()
    step = _steps(db, plan.plan_id)[0]
    step.args = {"project_id": project.id, "target": "scene", "entity_id": scene.id}
    db.commit()
    from app.services.execution_manager import execute_step

    execute_step(
        db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id,
        session_id=goal.session_id, project_id=project.id, goal=goal,
    )
    db.commit()
    db.expire_all()
    step = db.get(AgentPlanStep, step.id)
    assert step.status == "waiting_approval"

    approvals.resolve_confirmation(db, confirmation_id=step.confirmation_id, approve=True)
    assert calls["tick"] == 0