"""A11 验收测试：原子执行锁（P1）。

覆盖：
- 两个独立 Session 并发抢同一步骤，恰好一个成功
- 释放锁必须校验 lock_owner
- reclaim_stale_steps 的 retry / failed 两个分支
- 同一 Goal 并发 run_agent_loop：第二次返回 busy 且不执行任何工具
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.services import planner as planner_mod
from app.services.agent_runtime import run_agent_loop
from app.services.execution_manager import (
    _acquire_goal_lock,
    _acquire_step_lock,
    _release_goal_lock,
    _release_step_lock,
    reclaim_stale_steps,
)
from app.models.ids import agent_plan_step_id
from tests.agent_runtime_fixtures import fake_tool, mk_project
from tests.test_agent_runtime_tx import _fixed_plan


@pytest.fixture()
def project(db):
    p = mk_project(db, with_script=True, with_parse=True)
    db.commit()
    return p


def _mk_step(db, *, status: str = "pending", timeout_seconds: int = 600,
             retry_count: int = 0, max_retries: int = 3, started_at=None):
    from app.models.agent import AgentPlanStep

    step = AgentPlanStep(
        id=agent_plan_step_id(),
        task_id="",
        step_index=0,
        step_type="tool_call",
        tool_name="fake_lock_tool",
        args={},
        status=status,
        plan_id="pln_lock_test",
        step_key="s0",
        timeout_seconds=timeout_seconds,
        retry_count=retry_count,
        max_retries=max_retries,
        started_at=started_at,
    )
    db.add(step)
    db.commit()
    return step


def test_concurrent_acquire_only_one_wins(rt_sessionmaker, db):
    step = _mk_step(db)

    def _try_acquire():
        session = rt_sessionmaker()
        try:
            owner = _acquire_step_lock(session, step.id)
            session.commit()
            return owner
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _try_acquire(), range(2)))

    winners = [r for r in results if r]
    assert len(winners) == 1, results

    db.expire_all()
    row = db.get(type(step), step.id)
    assert row.status == "running"
    assert row.lock_owner == winners[0]


def test_release_requires_owner(db):
    step = _mk_step(db)
    owner = _acquire_step_lock(db, step.id)
    assert owner

    # 错误 owner 不能释放
    assert _release_step_lock(db, step.id, status="failed", lock_owner="not-owner") is False
    db.expire_all()
    assert db.get(type(step), step.id).status == "running"

    # 正确 owner 可以释放
    assert _release_step_lock(db, step.id, status="succeeded", lock_owner=owner) is True
    db.expire_all()
    row = db.get(type(step), step.id)
    assert row.status == "succeeded"
    assert row.lock_owner is None


def test_reclaim_stale_steps_retry_branch(db):
    stale = _mk_step(
        db,
        status="running",
        timeout_seconds=10,
        retry_count=0,
        max_retries=3,
        started_at=datetime.now() - timedelta(seconds=60),
    )
    outcome = reclaim_stale_steps(db)
    assert stale.id in outcome["reclaimed"]

    db.expire_all()
    row = db.get(type(stale), stale.id)
    assert row.status == "pending"
    assert row.retry_count == 1
    assert row.started_at is None
    assert row.lock_owner is None


def test_reclaim_stale_steps_failed_branch(db):
    stale = _mk_step(
        db,
        status="running",
        timeout_seconds=10,
        retry_count=2,
        max_retries=3,
        started_at=datetime.now() - timedelta(seconds=60),
    )
    outcome = reclaim_stale_steps(db)
    assert stale.id in outcome["failed"]

    db.expire_all()
    row = db.get(type(stale), stale.id)
    assert row.status == "failed"
    assert row.error_code == "lock_timeout"
    assert row.retry_count == 3


def test_reclaim_skips_fresh_running_step(db):
    fresh = _mk_step(db, status="running", timeout_seconds=600, started_at=datetime.now())
    outcome = reclaim_stale_steps(db)
    assert fresh.id not in outcome["reclaimed"]
    assert fresh.id not in outcome["failed"]
    db.expire_all()
    assert db.get(type(fresh), fresh.id).status == "running"


def test_goal_lock_busy(db, project, monkeypatch):
    calls = {"n": 0}

    def _tool(**kwargs):
        calls["n"] += 1
        return {"ok": 1}

    fake_tool(monkeypatch, "fake_lock_busy", returns=_tool)
    _fixed_plan(monkeypatch, "", ["fake_lock_busy"])

    first = run_agent_loop(
        db, session_id="a11busy", description="执行测试", project_id=project.id
    )
    assert first.goal_id

    # 手工持有该 Goal 的锁，模拟另一个执行者正在推进
    assert _acquire_goal_lock(db, first.goal_id)
    db.commit()
    calls_before = calls["n"]

    second = run_agent_loop(
        db, session_id="a11busy", description="执行测试", project_id=project.id
    )
    assert second.status == "busy", (second.status, second.message)
    assert second.goal_id == first.goal_id
    assert calls["n"] == calls_before, "busy 时不得执行任何工具"

    _release_goal_lock(db, first.goal_id)
    db.commit()


def test_goal_lock_released_after_run(db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_lock_rel", returns={"ok": 1})
    _fixed_plan(monkeypatch, "", ["fake_lock_rel"])

    result = run_agent_loop(
        db, session_id="a11rel", description="执行测试", project_id=project.id
    )
    assert result.status == "completed"

    from app.models.agent import AgentGoal

    row = db.get(AgentGoal, result.goal_id)
    db.refresh(row)
    assert row.locked_until is None, "执行结束后必须释放 Goal 锁"