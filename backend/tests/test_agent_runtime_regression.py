"""A12b 回归汇总：每个 P0 缺陷一条命名回归用例。

复用各任务验收测试中的 helper，只保留最小断言，作为 Phase A 的最终门禁：
    pytest -q tests/test_agent_runtime_regression.py
"""
from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.services import planner as planner_mod
from app.services.agent_runtime import run_agent_loop
from tests.agent_runtime_fixtures import fake_tool, mk_job, mk_project
from tests.test_agent_runtime_tx import _fixed_plan


@pytest.fixture()
def project(db):
    return mk_project(db, with_script=True, with_parse=True)


@pytest.fixture()
def committed_project(db):
    p = mk_project(db, with_script=True, with_parse=True)
    db.commit()
    return p


def _enable_debug(monkeypatch):
    from app.core.config import clear_settings_cache

    monkeypatch.setenv("DLAB_AGENT_LOOP_DEBUG_ENABLED", "true")
    clear_settings_cache()


# -- P0-1 事务边界：Runtime 落库后新连接可见 ------------------------------

def test_p0_1_persisted(db, committed_project, monkeypatch, fresh_session):
    from app.models.agent import AgentExecutionLog, AgentGoal, AgentPlan, AgentPlanStep

    fake_tool(monkeypatch, "reg_p0_1", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["reg_p0_1"])

    result = run_agent_loop(
        db, session_id="reg_p01", description="检查项目进度", project_id=committed_project.id
    )
    assert result.status == "completed", (result.status, result.message, result.errors)
    db.close()

    other = fresh_session()
    assert other.get(AgentGoal, result.goal_id) is not None
    assert other.get(AgentPlan, result.plan_id) is not None
    steps = other.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id == result.plan_id)
    ).all()
    assert steps and all(s.status == "succeeded" for s in steps)
    assert other.scalars(
        select(AgentExecutionLog).where(AgentExecutionLog.goal_id == result.goal_id)
    ).all()
    other.close()


# -- P0-2 请求作用域 Session 不进线程 ------------------------------------

@pytest.mark.asyncio
async def test_p0_2_no_request_session_in_thread(monkeypatch, committed_project):
    from sqlalchemy.orm import Session

    from app.main import app
    from app.services import agent_runtime as rt

    seen: dict = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        return rt.AgentLoopResult(
            status="completed", goal_id="gol_reg", plan_id="pln_reg", message="ok"
        )

    monkeypatch.setattr(rt, "run_agent_loop_in_new_session", _fake)
    _enable_debug(monkeypatch)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as c, c.stream(
        "POST",
        "/api/agent/loop/run",
        json={
            "session_id": "reg_p02",
            "description": "执行",
            "project_id": committed_project.id,
        },
        timeout=10.0,
    ) as resp:
        async for _ in resp.aiter_lines():
            pass

    assert not any(isinstance(v, Session) for v in seen.values()), "不得把 Session 传进线程"
    assert seen.get("session_id") == "reg_p02"


# -- P0-3 SSE 事件不丢失且必定终止 --------------------------------------

@pytest.mark.asyncio
async def test_p0_3_sse_events_delivered(
    patch_runtime_sessionmaker, committed_project, monkeypatch
):
    from app.main import app

    fake_tool(monkeypatch, "reg_p0_3", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["reg_p0_3"])
    _enable_debug(monkeypatch)

    events: list[dict] = []
    started = time.monotonic()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as c, c.stream(
        "POST",
        "/api/agent/loop/run",
        json={
            "session_id": "reg_p03",
            "description": "执行测试步骤",
            "project_id": committed_project.id,
        },
        timeout=15.0,
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"SSE 未在 5 秒内结束：{elapsed:.1f}s"

    types = [e["event_type"] for e in events]
    assert "step_started" in types
    assert "step_completed" in types
    assert types[-1] in {
        "completed", "failed", "blocked", "waiting",
        "waiting_approval", "waiting_job", "waiting_user", "paused", "cancelled",
    }
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


# -- P0-4 进度事件顺序与统计 --------------------------------------------

def test_p0_4_progress_events(db, project, monkeypatch):
    fake_tool(monkeypatch, "reg_p0_4", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["reg_p0_4", "reg_p0_4"])

    events: list[tuple[str, dict]] = []
    result = run_agent_loop(
        db,
        session_id="reg_p04",
        description="执行两步测试",
        project_id=project.id,
        progress_callback=lambda ev, payload: events.append((ev, payload)),
    )
    assert result.status == "completed", (result.status, result.message)
    assert [e[0] for e in events] == [
        "goal_created",
        "plan_generated",
        "step_started",
        "step_completed",
        "step_started",
        "step_completed",
        "completed",
    ]
    assert events[-1][1]["completed_steps"] == 2
    assert events[-1][1]["total_steps"] == 2


# -- P0-5 工具只走 execute_tool 网关 ------------------------------------

def test_p0_5_gateway_only(db, project, monkeypatch):
    from app.models.agent import AgentPlanStep
    from app.services import agent_tools
    from app.services.execution_manager import execute_step
    from app.services.goal_manager import get_or_create_goal

    calls = {"n": 0}
    real = agent_tools.execute_tool

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    fake_tool(monkeypatch, "reg_p0_5", returns={"ok": 1})
    monkeypatch.setattr(agent_tools, "execute_tool", _counting)

    goal = get_or_create_goal(
        db, session_id="reg_p05", description="执行测试步骤", project_id=project.id
    )
    _fixed_plan(monkeypatch, "", ["reg_p0_5"])
    plan, report = planner_mod.build_and_save_plan(
        db, goal_id=goal.goal_id, description="执行测试步骤", project_id=project.id
    )
    assert report.passed, report.errors
    step = db.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id == plan.plan_id)
    ).first()

    execute_step(db, step_id=step.id, plan_id=plan.plan_id, goal_id=goal.goal_id)
    assert calls["n"] == 1, "执行步骤必须经过 execute_tool 网关"


def test_p0_5_no_direct_tool_fn_access():
    """静态守卫：execution_manager / agent_runtime 不得直接取工具 fn。"""
    services = Path(__file__).resolve().parents[1] / "app" / "services"
    offenders: list[str] = []
    for name in ("execution_manager.py", "agent_runtime.py"):
        tree = ast.parse((services / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                key = node.slice
                if isinstance(key, ast.Constant) and key.value == "fn":
                    offenders.append(f"{name}:{node.lineno}")
    assert not offenders, "禁止绕过网关直接调用工具 fn：" + ", ".join(offenders)


# -- P0-6 Job 创建成功只进 waiting_job ----------------------------------

def test_p0_6_job_waits(db, project, monkeypatch):
    from app.models.agent import AgentPlanStep

    job = mk_job(db, project.id, status="queued")
    fake_tool(monkeypatch, "reg_p0_6", returns={"job_id": job.id})
    _fixed_plan(monkeypatch, "", ["reg_p0_6"])

    result = run_agent_loop(
        db, session_id="reg_p06", description="生成分镜图", project_id=project.id
    )
    assert result.status == "waiting_job", (result.status, result.message)

    step = db.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id == result.plan_id)
    ).first()
    assert step.status == "waiting_job"
    assert step.job_id == job.id


# -- P0-7 失败步骤不算完成 ----------------------------------------------

def test_p0_7_failed_not_verified(db, project, monkeypatch):
    from app.models.agent import AgentPlanStep
    from app.services.verifier import verify_step

    fake_tool(monkeypatch, "reg_p0_7", raises=RuntimeError("boom"))
    _fixed_plan(monkeypatch, "", ["reg_p0_7"])

    result = run_agent_loop(
        db, session_id="reg_p07", description="失败步骤测试", project_id=project.id
    )
    assert result.completed_steps == 0, (result.status, result.message)
    assert result.failed_steps >= 1

    step = db.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id == result.plan_id)
    ).first()
    verification = verify_step(db, step_id=step.id, project_id=project.id)
    assert verification.verified is False


# -- P0-8 一次审批只放行它绑定的步骤 ------------------------------------

def test_p0_8_approval_per_step(db, project, monkeypatch):
    from app.models.agent import AgentConfirmation, AgentPlanStep
    from app.services.agent_runtime import resume_after_approval

    calls = {"s1": 0, "s2": 0}

    def _count_s1(**_kwargs):
        calls["s1"] += 1
        return {"ok": 1}

    def _count_s2(**_kwargs):
        calls["s2"] += 1
        return {"ok": 1}

    fake_tool(monkeypatch, "reg_p0_8_s1", returns=_count_s1, spec_overrides={"risk_level": "high"})
    fake_tool(monkeypatch, "reg_p0_8_s2", returns=_count_s2, spec_overrides={"risk_level": "high"})

    def _build(db_, *, goal_id, description, project_id=None, previous_plan=None):
        plan = planner_mod.Plan(
            plan_id=planner_mod.agent_plan_id(),
            goal_id=goal_id,
            revision=(previous_plan.revision + 1) if previous_plan else 1,
            metadata={"description": description, "project_id": project_id},
        )
        for name in ("reg_p0_8_s1", "reg_p0_8_s2"):
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
        db, session_id="reg_p08", description="两个高风险步骤", project_id=project.id
    )
    assert first.status == "waiting_approval", (first.status, first.message)
    assert calls == {"s1": 0, "s2": 0}

    steps = {
        s.tool_name: s
        for s in db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == first.plan_id)
        ).all()
    }
    s1, s2 = steps["reg_p0_8_s1"], steps["reg_p0_8_s2"]
    assert s1.status == "waiting_approval" and s2.status == "waiting_approval"

    confirmation = AgentConfirmation(
        id="acf_regp080000000000000000001",
        task_id=None,
        tool_name="reg_p0_8_s1",
        args={},
        status="approved",
        session_id="reg_p08",
        project_id=project.id,
    )
    db.add(confirmation)
    s1.confirmation_id = confirmation.id
    db.flush()

    outcome = resume_after_approval(
        db,
        goal_id=first.goal_id,
        step_id=s1.id,
        confirmation_id=confirmation.id,
        project_id=project.id,
    )
    assert outcome["resumed"] is True, outcome
    assert calls == {"s1": 1, "s2": 0}, "一次审批不能放行其他步骤"
    assert db.get(AgentPlanStep, s1.id).status == "succeeded"
    assert db.get(AgentPlanStep, s2.id).status == "waiting_approval"


# -- P0-9 resume 尚无调用方（Phase B） ----------------------------------

def test_p0_9_resume_has_caller():
    """Phase B：Job 终态与审批裁决都会打恢复标记，Worker 的 agent-resume 线程负责消费。"""
    from app.services import agent_resume, approvals, task_orchestrator

    source = Path(task_orchestrator.__file__).read_text(encoding="utf-8")
    assert "mark_agent_steps_for_job" in source

    approvals_source = Path(approvals.__file__).read_text(encoding="utf-8")
    assert "resume_requested_at" in approvals_source

    resume_source = Path(agent_resume.__file__).read_text(encoding="utf-8")
    assert "process_marked" in resume_source and "sweep" in resume_source


# -- P0-10 新意图不复用旧目标 -------------------------------------------

def test_p0_10_new_intent_new_goal(db, project, monkeypatch):
    from app.models.agent import AgentGoal

    fake_tool(monkeypatch, "reg_p0_10", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["reg_p0_10"])

    first = run_agent_loop(
        db, session_id="reg_p010", description="检查项目进度概览", project_id=project.id
    )
    second = run_agent_loop(
        db, session_id="reg_p010", description="生成分镜图", project_id=project.id
    )
    assert second.goal_id != first.goal_id
    assert db.get(AgentGoal, first.goal_id).status == "superseded"


# -- P0-11 Replan 产生新 revision ---------------------------------------

def test_p0_11_replan_revision(db, project, monkeypatch):
    from app.models.agent import AgentPlan, AgentPlanStep
    from app.services import replanner as replanner_mod
    from app.services.replanner import ErrorContext, ReplanAction, replan
    from tests.test_agent_runtime_replan import _two_step_plan

    fake_tool(monkeypatch, "reg_p0_11_ok", returns={"ok": 1})
    fake_tool(monkeypatch, "reg_p0_11_bad", raises=RuntimeError("boom"))
    goal, plan = _two_step_plan(db, monkeypatch, "reg_p0_11_ok", "reg_p0_11_bad", project)

    # replanner 在模块顶层 import 了 build_and_save_plan，必须同时替换它的引用
    def _build(db_, *, goal_id, description, project_id=None, previous_plan=None):
        new_plan = planner_mod.Plan(
            plan_id=planner_mod.agent_plan_id(),
            goal_id=goal_id,
            revision=(previous_plan.revision + 1) if previous_plan else 1,
            metadata={"description": description, "project_id": project_id},
        )
        for idx, name in enumerate(("reg_p0_11_ok", "reg_p0_11_bad")):
            new_plan.steps.append(
                planner_mod.PlanStep(
                    step_key=f"s{idx}", tool_name=name, description=f"步骤 {idx}",
                    args={}, depends_on=[f"s{idx - 1}"] if idx else [],
                    estimated_timeout=30,
                )
            )
        report = planner_mod.validate_plan(new_plan, project_id=project_id)
        if report.passed:
            planner_mod.save_plan(db_, new_plan, supersede_previous=True)
            planner_mod.save_plan_steps(db_, new_plan)
        return new_plan, report

    monkeypatch.setattr(planner_mod, "build_and_save_plan", _build)
    monkeypatch.setattr(replanner_mod, "build_and_save_plan", _build)

    steps = db.scalars(
        select(AgentPlanStep)
        .where(AgentPlanStep.plan_id == plan.plan_id)
        .order_by(AgentPlanStep.step_index)
    ).all()
    failed_step = steps[1]
    failed_step.status = "failed"
    db.flush()

    decision = replan(
        db,
        error_ctx=ErrorContext(
            step_id=failed_step.id,
            step_key="s1",
            tool_name="reg_p0_11_bad",
            error_code="verification_failed",
            error_message="验证失败",
        ),
        plan_id=plan.plan_id,
        goal_id=goal.goal_id,
        project_id=project.id,
    )
    assert decision.action == ReplanAction.REPLAN, decision.reason
    new_plan = decision.updated_plan
    assert new_plan is not None
    assert new_plan.revision == plan.revision + 1
    row = db.get(AgentPlan, new_plan.plan_id)
    assert row.parent_plan_id == plan.plan_id


# -- P0-12 状态接口按 goal_id 查询 ---------------------------------------

def test_p0_12_status_by_goal_id(
    db, committed_project, monkeypatch, patch_runtime_sessionmaker
):
    from fastapi.testclient import TestClient

    from app.main import app

    fake_tool(monkeypatch, "reg_p0_12", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["reg_p0_12"])
    result = run_agent_loop(
        db, session_id="reg_p012", description="状态接口测试", project_id=committed_project.id
    )
    db.commit()

    client = TestClient(app)
    resp = client.get(f"/api/agent/loop/status/{result.goal_id}")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["goal"]["goal_id"] == result.goal_id
    assert payload["latest_plan"]["plan_id"] == result.plan_id
    assert payload["loop_status"] == "completed"
    assert client.get("/api/agent/loop/status/gol_notexist").status_code == 404


# -- P0-13 测试不得连接真实库 -------------------------------------------

def test_p0_13_no_real_db():
    from tests.test_no_real_db_in_tests import (
        test_no_module_level_engine_on_real_path,
        test_no_real_database_literal_in_tests,
    )

    test_no_real_database_literal_in_tests()
    test_no_module_level_engine_on_real_path()