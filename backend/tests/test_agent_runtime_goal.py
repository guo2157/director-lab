"""A8 验收测试：目标指纹（P0-10）。

覆盖：
- 相同意图复用同一 goal 与 plan
- 不同意图生成新 goal，旧 goal 变为 superseded
- 不同 project 生成新 goal
- description 历史最多 10 条
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.services import planner as planner_mod
from app.services.agent_runtime import run_agent_loop
from app.services.goal_manager import compute_fingerprint, get_goal_by_id, get_or_create_goal
from tests.agent_runtime_fixtures import fake_tool, mk_project
from tests.test_agent_runtime_tx import _fixed_plan


@pytest.fixture()
def project(db):
    return mk_project(db, with_script=True, with_parse=True)


def test_fingerprint_is_stable_and_scope_sensitive():
    a = compute_fingerprint(project_id="prj_1", goal_type="user_request", target_stage="query")
    b = compute_fingerprint(project_id="prj_1", goal_type="user_request", target_stage="query")
    c = compute_fingerprint(project_id="prj_1", goal_type="user_request", target_stage="analyze")
    d = compute_fingerprint(project_id="prj_1", goal_type="user_request", target_stage="query", scope="ch1")
    assert a == b
    assert a != c
    assert a != d
    assert len(a) == 16


def test_same_intent_reuses_goal_and_plan(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a8_ok", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_a8_ok"])

    first = run_agent_loop(
        db, session_id="a8same", description="查看项目进度", project_id=project.id
    )
    second = run_agent_loop(
        db, session_id="a8same", description="再看一下项目状态", project_id=project.id
    )
    assert second.goal_id == first.goal_id

    from app.models.agent import AgentGoal

    goal = db.get(AgentGoal, first.goal_id)
    # description 不被覆盖，最新描述进入 history
    assert goal.description == "查看项目进度"
    history = (goal.constraints or {}).get("history") or []
    assert history[-1] == "再看一下项目状态"


def test_different_intent_supersedes_goal(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a8_ok2", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_a8_ok2"])

    first = run_agent_loop(
        db, session_id="a8diff", description="检查项目进度概览", project_id=project.id
    )
    second = run_agent_loop(
        db, session_id="a8diff", description="生成分镜图", project_id=project.id
    )
    assert second.goal_id != first.goal_id

    from app.models.agent import AgentGoal

    old = db.get(AgentGoal, first.goal_id)
    assert old.status == "superseded"
    new = db.get(AgentGoal, second.goal_id)
    assert new.status == "active"
    assert new.fingerprint and new.fingerprint != old.fingerprint


def test_different_project_new_goal(db, project, monkeypatch):
    other = mk_project(db, with_script=True, with_parse=True)
    fake_tool(monkeypatch, "fake_a8_ok3", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_a8_ok3"])

    first = run_agent_loop(
        db, session_id="a8prj", description="检查项目进度", project_id=project.id
    )
    second = run_agent_loop(
        db, session_id="a8prj", description="检查项目进度", project_id=other.id
    )
    assert second.goal_id != first.goal_id


def test_description_history_capped_at_10(db, project):
    goal = get_or_create_goal(
        db, session_id="a8hist", description="检查项目进度 0", project_id=project.id
    )
    for i in range(1, 15):
        goal = get_or_create_goal(
            db, session_id="a8hist", description=f"检查项目进度 {i}", project_id=project.id
        )
    history = goal.constraints.get("history") or []
    assert len(history) == 10
    assert history[-1] == "检查项目进度 14"
    assert history[0] == "检查项目进度 5"


def test_terminal_plan_creates_new_revision(db, project, monkeypatch):
    """复用的目标若上一版计划已全部终态，本次应生成新 revision。"""
    fake_tool(monkeypatch, "fake_a8_rev", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_a8_rev"])

    first = run_agent_loop(
        db, session_id="a8rev", description="检查项目进度", project_id=project.id
    )
    assert first.status == "completed"
    second = run_agent_loop(
        db, session_id="a8rev", description="检查项目进度", project_id=project.id
    )
    assert second.goal_id == first.goal_id
    assert second.plan_id != first.plan_id

    from app.models.agent import AgentPlan

    old_plan = db.get(AgentPlan, first.plan_id)
    new_plan = db.get(AgentPlan, second.plan_id)
    assert new_plan.revision == old_plan.revision + 1
    assert new_plan.parent_plan_id == old_plan.id
