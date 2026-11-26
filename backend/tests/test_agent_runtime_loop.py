"""Agent Loop 主循环测试（run_agent_loop 完整编排）。

原 tests/test_agent_loop_main.py 在模块顶层连真实库、用 print/check 计数，
pytest 收集时就会污染生产数据。这里改为 tmp 库 + 真断言，保留原 5 个场景意图。

TODO(A8)：场景 2 原先断言「同 session 复用同一 plan_id」。那正是 A8 要修的错误行为
（新目标不应复用旧计划），这里不再断言，A8 完成后补上「指纹变化 → 新 Goal / 新 Plan」。
"""
from __future__ import annotations

import pytest

from app.services.agent_runtime import (
    _MAX_LOOP_ITERATIONS,
    AgentLoopResult,
    run_agent_loop,
)

from tests.agent_runtime_fixtures import fake_tool, mk_project
from tests.test_agent_runtime_tx import _fixed_plan


@pytest.fixture()
def project(db):
    return mk_project(db, with_script=True, with_parse=True)


def test_scenario1_query_returns_loop_result(db, project):
    result = run_agent_loop(
        db,
        session_id="ags_mainloop_test1",
        description="检查项目进度概览",
        project_id=project.id,
    )
    assert isinstance(result, AgentLoopResult)
    assert result.goal_id
    assert result.plan_id
    assert isinstance(result.context_snapshot, dict) and result.context_snapshot
    assert result.status
    assert result.total_steps > 0
    assert result.message
    executed = result.completed_steps + result.failed_steps + result.skipped_steps
    assert executed <= result.total_steps


def test_scenario2_same_session_reuses_goal(db, project):
    session_id = "ags_mainloop_test2"
    first = run_agent_loop(
        db, session_id=session_id, description="检查项目进度概览", project_id=project.id
    )
    second = run_agent_loop(
        db, session_id=session_id, description="更新后的概览查询", project_id=project.id
    )
    assert second.goal_id == first.goal_id


def test_scenario3_analysis_intent_has_more_steps(db, project):
    result = run_agent_loop(
        db,
        session_id="ags_mainloop_test3",
        description="分析项目进度并诊断问题",
        project_id=project.id,
    )
    assert result.total_steps >= 3
    assert result.status in {
        "completed",
        "failed",
        "waiting",
        "waiting_approval",
        "waiting_job",
        "blocked",
    }


def test_scenario4_constraints_do_not_crash(db, project):
    result = run_agent_loop(
        db,
        session_id="ags_mainloop_test4",
        description="检查项目状态",
        project_id=project.id,
        constraints={"max_cost": 100, "allowed_tools": ["read"]},
    )
    assert isinstance(result, AgentLoopResult)
    assert result.status


def test_scenario5_vague_description_still_has_plan(db, project):
    result = run_agent_loop(
        db,
        session_id="ags_mainloop_test5",
        description="随便看看",
        project_id=project.id,
    )
    assert result.total_steps >= 1


def test_loop_iteration_cap_is_bounded():
    """防止把 _MAX_LOOP_ITERATIONS 改成无限循环。"""
    assert 0 < _MAX_LOOP_ITERATIONS <= 100


def test_progress_events_order(db, project, monkeypatch):
    """A3/P0-4：事件顺序正确，且最终事件里的统计值不是 0。"""
    fake_tool(monkeypatch, "fake_a3", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_a3", "fake_a3"])

    events: list[tuple[str, dict]] = []
    result = run_agent_loop(
        db,
        session_id="a3order",
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
    final = events[-1][1]
    assert final["completed_steps"] == 2
    assert final["total_steps"] == 2


def test_progress_callback_exception_is_isolated(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a3b", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_a3b"])

    def _boom(_ev, _payload):
        raise RuntimeError("callback down")

    result = run_agent_loop(
        db,
        session_id="a3boom",
        description="执行测试",
        project_id=project.id,
        progress_callback=_boom,
    )
    assert result.status == "completed", (result.status, result.message)
    assert result.completed_steps == 1


def test_goal_resumed_event_on_second_run(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a3c", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_a3c"])
    run_agent_loop(db, session_id="a3resume", description="执行测试", project_id=project.id)

    events: list[str] = []
    run_agent_loop(
        db,
        session_id="a3resume",
        description="执行测试",
        project_id=project.id,
        progress_callback=lambda ev, _p: events.append(ev),
    )
    assert "goal_resumed" in events
