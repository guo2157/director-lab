"""Phase B B2 验收：agent.* 事件写入 outbox、同事务、只在状态变化时发、payload 无自由文本。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.workspace_event import WorkspaceEvent
from app.services import agent_events
from app.services import planner as planner_mod
from app.services.agent_runtime import tick
from tests.agent_runtime_fixtures import fake_tool, mk_project


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


def _goal_with_plan(db, project, monkeypatch, tools, session="b2"):
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


def _events(db, event_type=None):
    q = select(WorkspaceEvent).order_by(WorkspaceEvent.event_seq)
    if event_type:
        q = q.where(WorkspaceEvent.event_type == event_type)
    return db.scalars(q).all()


def test_events_written_in_same_tx(db, project):
    """业务写入回滚后，outbox 里不应留下对应事件。"""
    from app.services.goal_manager import get_or_create_goal

    goal = get_or_create_goal(
        db, session_id="b2tx", description="检查项目进度", project_id=project.id
    )
    db.commit()
    agent_events.publish_goal(db, goal, "agent.goal.updated")
    db.rollback()

    assert _events(db, "agent.goal.updated") == []


def test_step_event_only_on_change(db, project, monkeypatch, rt_sessionmaker):
    fake_tool(monkeypatch, "b2_sync", returns={"ok": 1})
    goal, _plan = _goal_with_plan(db, project, monkeypatch, ["b2_sync"], session="b2step")

    tick(rt_sessionmaker, goal.goal_id, trigger="manual")
    first_count = len(_events(db, "agent.step.updated"))
    assert first_count >= 1

    # 已完成 Goal 再 tick：不推进、不发步骤事件
    tick(rt_sessionmaker, goal.goal_id, trigger="manual")
    assert len(_events(db, "agent.step.updated")) == first_count


def test_event_sequence_for_simple_plan(db, project, monkeypatch, rt_sessionmaker):
    fake_tool(monkeypatch, "b2_seq", returns={"ok": 1})
    goal, plan = _goal_with_plan(db, project, monkeypatch, ["b2_seq", "b2_seq"], session="b2seq")

    from app.services.agent_events import publish_goal, publish_plan_created

    publish_goal(db, goal, "agent.goal.created")
    publish_plan_created(db, goal, plan)
    db.commit()

    tick(rt_sessionmaker, goal.goal_id, trigger="manual")

    events = _events(db)
    types = [e.event_type for e in events]
    assert types[0] == "agent.goal.created"
    assert types[1] == "agent.plan.created"
    assert types[-1] == "agent.tick.finished"
    assert types.count("agent.step.updated") == 2

    seqs = [e.event_seq for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_payload_has_no_free_text(db, project, monkeypatch, rt_sessionmaker):
    fake_tool(monkeypatch, "b2_payload", returns={"ok": 1})
    goal, _plan = _goal_with_plan(db, project, monkeypatch, ["b2_payload"], session="b2pay")
    tick(rt_sessionmaker, goal.goal_id, trigger="manual")

    allowed = {
        "agent.goal.created": agent_events.GOAL_PAYLOAD_KEYS,
        "agent.goal.updated": agent_events.GOAL_PAYLOAD_KEYS,
        "agent.goal.superseded": agent_events.GOAL_PAYLOAD_KEYS,
        "agent.goal.finished": agent_events.GOAL_PAYLOAD_KEYS,
        "agent.plan.created": agent_events.PLAN_PAYLOAD_KEYS,
        "agent.step.updated": agent_events.STEP_PAYLOAD_KEYS,
        "agent.tick.finished": agent_events.TICK_PAYLOAD_KEYS,
    }
    for ev in _events(db):
        if not ev.event_type.startswith("agent."):
            continue
        keys = set((ev.payload or {}).keys())
        assert keys <= allowed[ev.event_type], (ev.event_type, keys - allowed[ev.event_type])


def test_publish_ignores_unknown_extra_keys(db, project):
    from app.services.goal_manager import get_or_create_goal

    goal = get_or_create_goal(
        db, session_id="b2extra", description="检查项目进度", project_id=project.id
    )
    db.commit()
    agent_events.publish_goal(
        db, goal, "agent.goal.updated", extra={"prompt": "自由文本不该出现", "status": "active"}
    )
    db.commit()
    ev = _events(db, "agent.goal.updated")[-1]
    assert "prompt" not in (ev.payload or {})