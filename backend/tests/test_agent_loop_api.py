"""A2 验收测试：调试接口的线程与 SSE 桥接（P0-2、P0-3）。

使用 httpx.AsyncClient + ASGITransport 直接打 ASGI app，
不启动 uvicorn、不连接真实库（Runtime 通过 patch_runtime_sessionmaker 指向 tmp 库）。
"""
from __future__ import annotations

import json
import time

import httpx
import pytest
from sqlalchemy import select

from tests.agent_runtime_fixtures import fake_tool, mk_project


@pytest.fixture()
def project(db):
    p = mk_project(db, with_script=True, with_parse=True)
    db.commit()
    return p


def _fixed_plan(monkeypatch, tools):
    from app.services import planner as planner_mod

    def _build(db, *, goal_id, description, project_id=None, previous_plan=None):
        plan = planner_mod.Plan(
            plan_id=planner_mod.agent_plan_id(),
            goal_id=goal_id,
            revision=1,
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


@pytest.fixture()
def client(monkeypatch):
    from app.main import app

    monkeypatch.setenv("DLAB_AGENT_LOOP_DEBUG_ENABLED", "true")
    from app.core.config import clear_settings_cache

    clear_settings_cache()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture()
def runtime_db(patch_runtime_sessionmaker, db, project):
    """后台线程用的 Session 必须指向同一个 tmp 库，且项目已提交释放写锁。"""
    return db


@pytest.mark.asyncio
async def test_disabled_by_default(monkeypatch):
    from app.core.config import clear_settings_cache

    monkeypatch.setenv("DLAB_AGENT_LOOP_DEBUG_ENABLED", "false")
    clear_settings_cache()
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/api/agent/loop/run", json={"session_id": "a2off", "description": "检查进度"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "agent_loop_debug_disabled"


@pytest.mark.asyncio
async def test_sse_emits_ordered_events_and_terminates(client, runtime_db, project, monkeypatch):
    fake_tool(monkeypatch, "fake_a2", returns={"ok": 1})
    _fixed_plan(monkeypatch, ["fake_a2"])

    started = time.monotonic()
    async with client as c:
        async with c.stream(
            "POST",
            "/api/agent/loop/run",
            json={"session_id": "a2sse", "description": "执行测试步骤", "project_id": project.id},
            timeout=10.0,
        ) as resp:
            assert resp.status_code == 200
            events = []
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"SSE 未在 5 秒内结束：{elapsed:.1f}s"

    types = [e["event_type"] for e in events]
    assert "step_started" in types
    assert "step_completed" in types
    assert types[-1] in {"completed", "failed", "waiting", "waiting_approval", "waiting_job", "blocked"}

    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs), "seq 必须严格递增且唯一"


@pytest.mark.asyncio
async def test_no_request_session_in_thread(monkeypatch, runtime_db, project):
    from app.main import app
    from app.services import agent_runtime as rt

    seen = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        return rt.AgentLoopResult(status="completed", goal_id="gol_x", plan_id="pln_x", message="ok")

    monkeypatch.setattr(rt, "run_agent_loop_in_new_session", _fake)
    monkeypatch.setenv("DLAB_AGENT_LOOP_DEBUG_ENABLED", "true")
    from app.core.config import clear_settings_cache

    clear_settings_cache()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with c.stream(
            "POST",
            "/api/agent/loop/run",
            json={"session_id": "a2nosess", "description": "执行", "project_id": project.id},
            timeout=10.0,
        ) as resp:
            async for _ in resp.aiter_lines():
                pass

    assert "session_factory" not in seen or not hasattr(seen.get("session_factory"), "execute")
    assert not any(isinstance(v, __import__("sqlalchemy.orm", fromlist=["Session"]).Session) for v in seen.values())
    assert seen.get("session_id") == "a2nosess"
