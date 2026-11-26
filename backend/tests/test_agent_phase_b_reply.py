"""Phase B B8 验收：确定性回复模板、状态查询接口、Worker 进度消息。"""
from __future__ import annotations

import hashlib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Asset, Character, Job, Scene, ScriptVersion, Shot, ids
from app.models.agent import AgentMessage
from app.services.agent_reply import render_tick_reply, reply_violates_wording
from app.services.agent_runtime import run_agent_loop, tick
from app.services.goal_manager import get_or_create_goal
from tests.agent_runtime_fixtures import mk_project

_LIFECYCLES = [
    "planning", "running", "waiting_job", "waiting_approval", "waiting_user",
    "paused", "partially_completed", "completed", "failed", "cancelled",
]


class _FakeTick:
    def __init__(self, state: str):
        self.lifecycle_state = state
        self.status = state
        self.waiting = {
            "job": ["stp_1", "stp_2"],
            "approval": [{"step_id": "stp_3", "confirmation_id": "cnf_1"}],
            "user": [{"step_id": "stp_4", "error_code": "precondition_failed", "message": "缺少剧本"}],
        }
        self.failed = [
            {"step_id": "stp_5", "tool_name": "request_generate", "error_code": "job_failed:comfy_timeout",
             "message": "渲染超时"}
        ]
        self.counts = {"succeeded": 2, "failed": 1, "waiting": 3, "pending": 1}
        self.message = ""
        self.busy = False


class _FakeGoal:
    description = "生成所有角色资产"


def test_reply_templates_by_state():
    plan_steps = [{"description": "角色甲", "step_key": "asset:character:chr_1", "tool_name": "request_generate"}]
    for state in _LIFECYCLES:
        reply = render_tick_reply(_FakeTick(state), goal=_FakeGoal(), plan_steps=plan_steps)
        assert isinstance(reply, str) and reply
        assert not reply_violates_wording(reply, state), (state, reply)


def test_no_false_completion_wording():
    waiting_job = render_tick_reply(_FakeTick("waiting_job"), goal=_FakeGoal(), plan_steps=[])
    assert "生成完成" not in waiting_job
    assert "已完成" not in waiting_job
    waiting_approval = render_tick_reply(_FakeTick("waiting_approval"), goal=_FakeGoal(), plan_steps=[])
    assert "已完成" not in waiting_approval


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    import app.core.database as db_mod
    from app.core.config import clear_settings_cache
    from app.main import app
    from app.models import Base

    engine = db_mod.make_engine(f"sqlite:///{tmp_path / 'b8.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    clear_settings_cache()
    yield TestClient(app)
    clear_settings_cache()


def _media_file(name: str) -> str:
    from app.core.config import get_settings

    media = get_settings().media_dir
    media.mkdir(parents=True, exist_ok=True)
    (media / name).write_bytes(b"x")
    return name


def _project(db, *, characters=2, with_assets=0):
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
        c = Character(id=ids.character_id(), project_id=p.id, name=f"角色{idx + 1}")
        db.add(c)
        db.flush()
        if idx < with_assets:
            asset = Asset(
                id=ids.asset_id(), project_id=p.id, asset_type="character",
                name=f"角色{idx + 1}定妆图", version=1,
                file_path=_media_file(f"b8_char_{idx + 1}.png"), validity="current",
            )
            db.add(asset)
            db.flush()
            c.portrait_asset_id = asset.id
    # 有镜头才算走完 parse 阶段（characters 阶段门禁要求）
    db.add(
        Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1, shot_kind="normal", duration=3)
    )
    db.flush()
    baseline = Asset(
        id=ids.asset_id(), project_id=p.id, asset_type="scene", name="场景一基准图",
        version=1, file_path=_media_file("b8_scene_baseline.png"), validity="current",
    )
    db.add(baseline)
    db.flush()
    scene.baseline_asset_id = baseline.id
    from app.models import Workflow

    db.add(
        Workflow(
            id=ids.workflow_id(), name="默认图像工作流", workflow_type="image",
            is_default=True, is_builtin=True, version=1, definition={},
        )
    )
    db.commit()
    return p


def test_goal_status_endpoints(client):
    import app.core.database as db_mod

    db = db_mod.SessionLocal()
    try:
        project = _project(db)
        session_id = "ags_b8status"
        from app.models.agent import AgentSession

        db.add(AgentSession(id=session_id, title="B8", status="active", created_at=datetime.now()))
        db.commit()
        goal = get_or_create_goal(
            db, session_id=session_id, description="生成所有角色资产",
            project_id=project.id, goal_type="generate_assets",
            constraints={"scope": "character"},
        )
        db.commit()
        goal_id = goal.goal_id
    finally:
        db.close()

    r = client.get(f"/api/agent/{session_id}/goal")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["goal"]["goal_id"] == goal_id
    assert body["goal"]["fingerprint"]
    assert "steps" in body and "latest_plan" in body

    r = client.get(f"/api/agent/{session_id}/goals")
    assert r.status_code == 200, r.text
    assert [item["goal_id"] for item in r.json()["items"]] == [goal_id]

    r = client.get(f"/api/agent/goals/{goal_id}")
    assert r.status_code == 200, r.text
    assert r.json()["goal"]["goal_id"] == goal_id

    r = client.get("/api/agent/goals/gol_notexist")
    assert r.status_code == 404

    empty = client.post("/api/agent/sessions", json={"title": "空会话"}).json()["id"]
    r = client.get(f"/api/agent/{empty}/goal")
    assert r.status_code == 200
    assert r.json() == {"goal": None}


def test_worker_tick_posts_message_on_state_change_only(client, monkeypatch):
    import app.core.database as db_mod
    from app.models.agent import AgentSession

    db = db_mod.SessionLocal()
    try:
        project = _project(db, characters=2, with_assets=0)
        session_id = "ags_b8msg"
        db.add(AgentSession(id=session_id, title="B8 消息", status="active", created_at=datetime.now()))
        db.commit()
        goal = get_or_create_goal(
            db, session_id=session_id, description="生成所有角色资产",
            project_id=project.id, goal_type="generate_assets",
            constraints={"scope": "character"},
        )
        db.commit()
        goal_id = goal.goal_id
    finally:
        db.close()

    # 第一次推进：planning/running → waiting_approval，应写一条进度消息
    factory = db_mod.SessionLocal
    first = run_agent_loop(
        factory(), session_id=session_id, description="生成所有角色资产",
        project_id=project.id, goal_type="generate_assets",
        constraints={"scope": "character"},
    )
    assert first.lifecycle_state in ("waiting_approval", "waiting_user"), first.to_dict()

    db = factory()
    try:
        count_1 = len(
            db.scalars(
                select(AgentMessage).where(
                    AgentMessage.session_id == session_id,
                    AgentMessage.role == "assistant",
                )
            ).all()
        )
    finally:
        db.close()

    # Worker tick：生命周期没变 → 不写第二条
    tick(factory, goal_id, trigger="worker_resume")
    db = factory()
    try:
        messages = db.scalars(
            select(AgentMessage).where(AgentMessage.session_id == session_id, AgentMessage.role == "assistant")
        ).all()
        assert len(messages) == count_1
    finally:
        db.close()
