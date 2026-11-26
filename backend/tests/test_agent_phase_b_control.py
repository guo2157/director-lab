"""Phase B B9 验收：暂停 / 继续 / 取消，以及 /chat 控制关键词。"""
from __future__ import annotations

import hashlib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Asset, Character, Job, Scene, ScriptVersion, Shot, Workflow, ids
from app.models.agent import AgentConfirmation, AgentGoal, AgentPlanStep
from app.services import approvals
from app.services.agent_runtime import (
    cancel_goal_run,
    pause_goal,
    resume_goal_control,
    run_agent_loop,
    tick,
)
from app.services.agent_runtime_router import detect_control
from app.services.goal_manager import get_goal_by_id, get_or_create_goal
from tests.agent_runtime_fixtures import mk_project


def _media_file(name: str) -> str:
    from app.core.config import get_settings

    media = get_settings().media_dir
    media.mkdir(parents=True, exist_ok=True)
    (media / name).write_bytes(b"x")
    return name


def _project(db, *, characters=2):
    p = mk_project(db, with_script=True, with_parse=True)
    now = datetime.now()
    script = db.get(ScriptVersion, p.current_script_version_id)
    h = hashlib.sha256(script.content_snapshot.encode("utf-8")).hexdigest()
    script.content_hash = h
    p.estimated_text_hash = h
    db.add(Job(id=ids.job_id(), project_id=p.id, job_type="parse", status="succeeded", created_at=now))
    db.flush()
    scene = Scene(id=ids.scene_id(), project_id=p.id, episode=1, seq=1, title="场景一")
    db.add(scene)
    db.flush()
    for idx in range(characters):
        db.add(Character(id=ids.character_id(), project_id=p.id, name=f"角色{idx + 1}"))
    db.add(
        Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1, shot_kind="normal", duration=3)
    )
    db.flush()
    baseline = Asset(
        id=ids.asset_id(), project_id=p.id, asset_type="scene", name="场景一基准图",
        version=1, file_path=_media_file("b9_scene_baseline.png"), validity="current",
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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    import app.core.database as db_mod
    from app.core.config import clear_settings_cache
    from app.main import app
    from app.models import Base

    engine = db_mod.make_engine(f"sqlite:///{tmp_path / 'b9.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    clear_settings_cache()
    yield TestClient(app)
    clear_settings_cache()


def _setup_goal(factory, project, *, session="b9goal"):
    db = factory()
    try:
        goal = get_or_create_goal(
            db, session_id=session, description="生成所有角色资产",
            project_id=project.id, goal_type="generate_assets",
            constraints={"scope": "character"},
        )
        db.commit()
        goal_id = goal.goal_id
    finally:
        db.close()
    first = run_agent_loop(
        factory(), session_id=session, description="生成所有角色资产",
        project_id=project.id, goal_type="generate_assets",
        constraints={"scope": "character"},
    )
    return goal_id, first


def test_pause_stops_advancing(rt_sessionmaker):
    db = rt_sessionmaker()
    try:
        project = _project(db)
    finally:
        db.close()

    goal_id, first = _setup_goal(rt_sessionmaker, project)
    assert first.lifecycle_state == "waiting_approval", first.to_dict()

    db = rt_sessionmaker()
    try:
        steps = db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == first.plan_id)
        ).all()
        confirmation_ids = [s.confirmation_id for s in steps if s.confirmation_id]
        assert confirmation_ids
        assert pause_goal(db, goal_id)["ok"]
        db.commit()
    finally:
        db.close()

    # 暂停期间审批通过 + Job 完成：步骤状态照常收敛，但下游不推进
    db = rt_sessionmaker()
    try:
        for cid in confirmation_ids:
            approvals.resolve_confirmation(db, confirmation_id=cid, approve=True)
    finally:
        db.close()

    paused_tick = tick(rt_sessionmaker, goal_id, trigger="worker_resume")
    assert paused_tick.lifecycle_state == "paused"

    db = rt_sessionmaker()
    try:
        steps = db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == first.plan_id)
        ).all()
        statuses = {s.status for s in steps}
        assert "waiting_job" in statuses or "succeeded" in statuses
        assert "pending" not in statuses
    finally:
        db.close()

    # 恢复后继续推进
    db = rt_sessionmaker()
    try:
        assert resume_goal_control(db, goal_id)["ok"]
        db.commit()
    finally:
        db.close()
    resumed = tick(rt_sessionmaker, goal_id, trigger="worker_resume")
    assert resumed.lifecycle_state in ("waiting_job", "waiting_approval", "waiting_user", "completed")


def test_resume_continues(rt_sessionmaker):
    db = rt_sessionmaker()
    try:
        project = _project(db, characters=1)
    finally:
        db.close()

    goal_id, _first = _setup_goal(rt_sessionmaker, project, session="b9resume")
    db = rt_sessionmaker()
    try:
        pause_goal(db, goal_id)
        db.commit()
        assert get_goal_by_id(db, goal_id).status == "paused"
    finally:
        db.close()

    db = rt_sessionmaker()
    try:
        assert resume_goal_control(db, goal_id)["ok"]
        db.commit()
        goal = get_goal_by_id(db, goal_id)
        assert goal.status == "active"
        assert goal.lifecycle_state == "running"
    finally:
        db.close()


def test_cancel_cancels_jobs_and_confirmations(rt_sessionmaker):
    db = rt_sessionmaker()
    try:
        project = _project(db)
    finally:
        db.close()

    goal_id, first = _setup_goal(rt_sessionmaker, project, session="b9cancel")

    # 先让 1 个审批通过并 tick，产生真实 Job（步骤进入 waiting_job），再取消整个目标
    db = rt_sessionmaker()
    try:
        steps = db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == first.plan_id)
        ).all()
        confirmation_ids = [s.confirmation_id for s in steps if s.confirmation_id]
        assert confirmation_ids
        approvals.resolve_confirmation(db, confirmation_id=confirmation_ids[0], approve=True)
    finally:
        db.close()

    after_tick = tick(rt_sessionmaker, goal_id, trigger="worker_resume")
    assert after_tick.lifecycle_state in ("waiting_job", "waiting_approval"), after_tick.to_dict()

    db = rt_sessionmaker()
    try:
        jobs_before = db.scalars(select(Job).where(Job.project_id == project.id)).all()
        in_flight = [j for j in jobs_before if j.status in ("queued", "running")]
        assert in_flight, [j.status for j in jobs_before]
    finally:
        db.close()

    db = rt_sessionmaker()
    try:
        outcome = cancel_goal_run(db, goal_id, reason="用户取消")
        db.commit()
        assert outcome["ok"], outcome
        assert outcome["cancelled_jobs"], outcome
    finally:
        db.close()

    db = rt_sessionmaker()
    try:
        goal = db.get(AgentGoal, goal_id)
        assert goal.status == "cancelled"
        assert goal.lifecycle_state == "cancelled"
        steps = db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == first.plan_id)
        ).all()
        assert not [s for s in steps if s.status in ("running", "waiting_job", "waiting_approval", "pending")]
        # 已裁决的审批保持原状（裁决事实不可改写），只有 pending 的被取消
        statuses = {cid: db.get(AgentConfirmation, cid).status for cid in confirmation_ids}
        assert statuses[confirmation_ids[0]] == "approved"
        assert all(st == "cancelled" for cid, st in statuses.items() if cid != confirmation_ids[0])
        jobs = db.scalars(select(Job).where(Job.project_id == project.id)).all()
        assert not [j for j in jobs if j.status in ("queued", "running")], [j.status for j in jobs]
    finally:
        db.close()


def test_cancel_terminal_goal_409(client):
    import app.core.database as db_mod

    db = db_mod.SessionLocal()
    try:
        project = _project(db)
        goal = get_or_create_goal(
            db, session_id="ags_b9t", description="生成所有角色资产",
            project_id=project.id, goal_type="generate_assets",
        )
        db.commit()
        goal_id = goal.goal_id
        cancel_goal_run(db, goal_id, reason="第一次取消")
        db.commit()
    finally:
        db.close()

    r = client.post(f"/api/agent/goals/{goal_id}/cancel", json={"reason": "再来一次"})
    assert r.status_code == 409, r.text
    r = client.post(f"/api/agent/goals/{goal_id}/pause")
    assert r.status_code == 409, r.text


def test_chat_control_keywords_only_with_active_goal(client, monkeypatch):
    from app.core.config import clear_settings_cache

    monkeypatch.setenv("DLAB_AGENT_RUNTIME_CHAT_ENABLED", "true")
    clear_settings_cache()

    # 没有 active Goal：控制词不接管
    assert detect_control("暂停当前任务", has_active_goal=False) is None
    # 有 active Goal 但消息过长（闲聊）：不接管
    assert detect_control("暂停当前任务，然后帮我看看剧本", has_active_goal=True) is None
    assert detect_control("继续看剧本", has_active_goal=True) is None
    assert detect_control("暂停当前任务", has_active_goal=True) == "pause"
    assert detect_control("继续当前任务", has_active_goal=True) == "resume"
    assert detect_control("取消当前任务", has_active_goal=True) == "cancel"

    import app.services.intent_router as ir_mod
    from app.services.intent_router import IntentResult

    def _classify(content, **kwargs):
        return IntentResult("query", "测试桩", risk_level="low", confidence=1.0, source="test_stub")

    monkeypatch.setattr(ir_mod, "classify_intent", _classify)

    import app.api.v1.agent as agent_mod

    monkeypatch.setattr(agent_mod.LLMClient, "chat_messages", lambda self, m, **k: "普通回复")

    sid = client.post("/api/agent/sessions", json={"title": "控制词"}).json()["id"]
    r = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": "暂停当前任务"})
    assert r.status_code == 200, r.text
    assert r.json()["assistant_message"]["payload"]["route_action"] != "agent_runtime_control"
    clear_settings_cache()
