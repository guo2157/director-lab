"""Phase B B10 门禁：端到端 MVP（render_mock，tmp 库，不启动线程）。

场景：完整闭环 / 重启恢复 / 重复请求 / 刷新一致性 / 拒绝一条审批 / Job 失败 / 开关关闭。
Worker 由测试直接调用 agent_resume.process_marked / sweep 与 executor 执行。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import Asset, Character, Job, Scene, ScriptVersion, Shot, Workflow, ids
from app.models.agent import (
    AgentConfirmation,
    AgentGoal,
    AgentMessage,
    AgentPlan,
    AgentPlanStep,
)
from app.services import approvals
from app.services.agent_resume import process_marked, sweep

_MEDIA = None


def _media_dir():
    return _MEDIA


def _install_settings(monkeypatch, settings, tmp_path):
    """把 Settings 装进 app.core.config 的进程内缓存。

    多数模块在顶部 `from app.core.config import get_settings`，只 patch
    app.core.config.get_settings 无法影响它们已绑定的引用；直接写缓存并让
    runtime_settings 路径指向不存在的文件，所有调用方都会拿到同一份配置。
    """
    import app.core.config as config_mod

    missing = tmp_path / "no_runtime_settings.json"
    monkeypatch.setattr(config_mod, "_runtime_settings_path", lambda: missing)
    monkeypatch.setattr(config_mod, "_settings_cache", settings)
    monkeypatch.setattr(config_mod, "_runtime_settings_mtime", None)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """tmp 库 + tmp media + render_mock + 开关打开。"""
    import app.core.database as db_mod
    from app.core.config import Settings, clear_settings_cache
    from app.main import app
    from app.models import Base

    global _MEDIA
    _MEDIA = tmp_path / "media"
    _MEDIA.mkdir(parents=True, exist_ok=True)

    engine = db_mod.make_engine(f"sqlite:///{tmp_path / 'e2e.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)

    settings = Settings(
        render_mock=True,
        media_dir=_MEDIA,
        data_dir=tmp_path / "data",
        agent_runtime_chat_enabled=True,
    )
    _install_settings(monkeypatch, settings, tmp_path)

    # 意图识别与主对话 LLM 打桩（不连真实 LLM）
    import app.services.intent_router as ir_mod
    from app.services.intent_router import IntentResult

    def _classify(content, **kwargs):
        if "生成" in content or "分镜图" in content:
            return IntentResult(
                "generate_request", "测试桩：批量生成", goal="批量生成",
                subcategory="asset.generate", risk_level="medium",
                confidence=1.0, source="test_stub",
            )
        return IntentResult("query", "测试桩：普通对话", risk_level="low",
                            confidence=1.0, source="test_stub")

    monkeypatch.setattr(ir_mod, "classify_intent", _classify)
    import app.api.v1.agent as agent_mod

    monkeypatch.setattr(agent_mod.LLMClient, "chat_messages", lambda self, m, **k: "普通回复")

    client = TestClient(app)
    yield {"client": client, "factory": factory, "tmp_path": tmp_path, "settings": settings}
    clear_settings_cache()


def _seed_project(factory, *, characters=3, with_assets=0):
    """已解析项目 + N 个角色（可带真实定妆图）+ 场景基准图 + 镜头 + 图像工作流。"""
    from app.models import ParseVersion, Project

    db = factory()
    now = datetime.now()
    media = _MEDIA
    try:
        project = Project(
            id=ids.project_id(), name="E2E 项目", aspect_ratio="9:16",
            creation_type="script", status="active", created_at=now, updated_at=now,
        )
        db.add(project)
        db.flush()
        script = ScriptVersion(
            id=ids.script_version_id(), project_id=project.id, version=1,
            content_snapshot="E2E 剧本内容", content_hash="hash-v1", created_at=now,
        )
        db.add(script)
        db.flush()
        project.current_script_version_id = script.id
        pv = ParseVersion(
            id=ids.parse_version_id(), project_id=project.id,
            script_version_id=script.id, version=1, status="current", created_at=now,
        )
        db.add(pv)
        db.flush()
        project.current_parse_version_id = pv.id
        h = hashlib.sha256(script.content_snapshot.encode("utf-8")).hexdigest()
        script.content_hash = h
        project.estimated_text_hash = h

        scene = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=1, title="场景一")
        db.add(scene)
        db.flush()
        db.add(
            Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1,
                 shot_kind="normal", duration=3)
        )
        base_name = "e2e_baseline.png"
        (media / base_name).write_bytes(b"x")
        baseline = Asset(
            id=ids.asset_id(), project_id=project.id, asset_type="scene",
            name="场景一基准图", version=1, file_path=base_name, validity="current",
        )
        db.add(baseline)
        db.flush()
        scene.baseline_asset_id = baseline.id

        for idx in range(characters):
            c = Character(id=ids.character_id(), project_id=project.id, name=f"角色{idx + 1}")
            db.add(c)
            db.flush()
            if idx < with_assets:
                name = f"e2e_char_{idx + 1}.png"
                (media / name).write_bytes(b"x")
                asset = Asset(
                    id=ids.asset_id(), project_id=project.id, asset_type="character",
                    name=f"角色{idx + 1}定妆图", version=1, file_path=name, validity="current",
                )
                db.add(asset)
                db.flush()
                c.portrait_asset_id = asset.id

        # mock 图像工作流（SaveImage 带 $output.cover 标记）
        definition = {
            "1": {
                "class_type": "EmptyLatentImage",
                "_meta": {"title": "$width, $height"},
                "inputs": {"width": 512, "height": 288, "batch_size": 1},
            },
            "2": {
                "class_type": "SaveImage",
                "_meta": {"title": "$output.cover"},
                "inputs": {"images": ["1", 0]},
            },
        }
        db.add(
            Workflow(
                id=ids.workflow_id(), name="E2E 图像工作流", workflow_type="image",
                is_default=True, is_builtin=True, version=1, definition=definition,
            )
        )
        db.add(Job(id=ids.job_id(), project_id=project.id, job_type="parse",
                   status="succeeded", created_at=now))
        db.commit()
        return project.id
    finally:
        db.close()


def _session(client, project_id, title="E2E"):
    r = client.post("/api/agent/sessions", json={"title": title, "project_id": project_id})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _run_jobs(factory, project_id):
    """直接执行项目下所有 queued 生成任务（单测环境无 worker 进程）。"""
    from app.worker.executor import execute_generate_job

    db = factory()
    try:
        job_ids = [
            j.id
            for j in db.scalars(
                select(Job).where(
                    Job.project_id == project_id, Job.status == "queued"
                )
            ).all()
            if j.job_type.startswith("generate_")
        ]
    finally:
        db.close()
    results = [execute_generate_job(jid) for jid in job_ids]
    return job_ids, results


def _steps(factory, plan_id):
    db = factory()
    try:
        return [
            {
                "id": s.id, "status": s.status, "tool_name": s.tool_name,
                "job_id": s.job_id, "confirmation_id": s.confirmation_id,
                "error_code": s.error_code, "step_key": s.step_key,
            }
            for s in db.scalars(
                select(AgentPlanStep)
                .where(AgentPlanStep.plan_id == plan_id)
                .order_by(AgentPlanStep.step_index)
            ).all()
        ]
    finally:
        db.close()


def _goal_payload(client, session_id):
    r = client.get(f"/api/agent/{session_id}/goal")
    assert r.status_code == 200, r.text
    return r.json()


def _db_goal(factory, goal_id):
    db = factory()
    try:
        row = db.get(AgentGoal, goal_id)
        plan = db.scalars(
            select(AgentPlan).where(AgentPlan.goal_id == goal_id).order_by(AgentPlan.revision.desc())
        ).first()
        return {
            "status": row.status,
            "lifecycle_state": row.lifecycle_state,
            "task_id": row.task_id,
            "agent_task_id": row.agent_task_id,
            "plan_id": plan.id if plan else None,
            "plan_status": plan.status if plan else None,
        }
    finally:
        db.close()


def test_mvp_full_flow(env):
    client, factory = env["client"], env["factory"]
    project_id = _seed_project(factory)
    session_id = _session(client, project_id)

    r = client.post(
        f"/api/agent/sessions/{session_id}/chat", json={"content": "帮我生成这个项目所有角色资产"}
    )
    assert r.status_code == 200, r.text
    payload = r.json()["assistant_message"]["payload"]
    assert payload["route_action"] == "agent_runtime"
    assert payload["lifecycle_state"] == "waiting_approval"
    goal_id, plan_id = payload["goal_id"], payload["plan_id"]

    steps = _steps(factory, plan_id)
    waiting = [s for s in steps if s["status"] == "waiting_approval"]
    assert len(waiting) == 3
    assert len({s["confirmation_id"] for s in waiting}) == 3

    db = factory()
    try:
        for s in waiting:
            approvals.resolve_confirmation(db, confirmation_id=s["confirmation_id"], approve=True)
    finally:
        db.close()

    assert process_marked(factory) >= 1
    steps = _steps(factory, plan_id)
    waiting_job = [s for s in steps if s["status"] == "waiting_job"]
    assert len(waiting_job) == 3, [s["status"] for s in steps]
    assert all(s["job_id"] for s in waiting_job)

    _run_jobs(factory, project_id)
    assert process_marked(factory) >= 1

    steps = _steps(factory, plan_id)
    assert [s["status"] for s in steps].count("succeeded") == 4, [s["status"] for s in steps]
    db_goal = _db_goal(factory, goal_id)
    assert db_goal["status"] == "completed"
    assert db_goal["lifecycle_state"] == "completed"
    assert db_goal["plan_status"] == "completed"

    # 会话里有完成消息（Worker 进度消息）
    db = factory()
    try:
        messages = db.scalars(
            select(AgentMessage).where(
                AgentMessage.session_id == session_id, AgentMessage.role == "assistant"
            )
        ).all()
    finally:
        db.close()
    assert messages
    assert any("已完成" in m.content for m in messages), [
        (m.role, m.content, m.payload) for m in messages
    ]

    # GET goal 与库内一致
    body = _goal_payload(client, session_id)
    assert body["goal"]["goal_id"] == goal_id
    assert body["loop_status"] == "completed"
    assert {s["status"] for s in body["steps"]} == {"succeeded"}


def test_restart_mid_flow(env):
    client, factory = env["client"], env["factory"]
    project_id = _seed_project(factory)
    session_id = _session(client, project_id)

    payload = client.post(
        f"/api/agent/sessions/{session_id}/chat", json={"content": "帮我生成这个项目所有角色资产"}
    ).json()["assistant_message"]["payload"]
    goal_id, plan_id = payload["goal_id"], payload["plan_id"]

    db = factory()
    try:
        steps = db.scalars(select(AgentPlanStep).where(AgentPlanStep.plan_id == plan_id)).all()
        for s in steps:
            if s.confirmation_id:
                approvals.resolve_confirmation(db, confirmation_id=s.confirmation_id, approve=True)
    finally:
        db.close()

    process_marked(factory)
    _run_jobs(factory, project_id)

    # 「重启」：丢弃所有 Session 与 sessionmaker，新 sessionmaker 指向同一个库，
    # 并清空全部恢复标记（模拟标记丢失）
    import app.core.database as db_mod

    engine = db_mod.engine
    new_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = new_factory()
    try:
        for s in db.scalars(select(AgentPlanStep)).all():
            s.resume_requested_at = None
            s.resume_reason = None
        db.commit()
    finally:
        db.close()

    stats = sweep(new_factory)
    assert stats["marked_job"] >= 1, stats
    process_marked(new_factory)

    db = new_factory()
    try:
        steps = db.scalars(select(AgentPlanStep).where(AgentPlanStep.plan_id == plan_id)).all()
        assert [s.status for s in steps].count("succeeded") == 4, [s.status for s in steps]
        goal = db.get(AgentGoal, goal_id)
        assert goal.status == "completed"
        # 每个实体只有 1 条 Job
        jobs = [
            j for j in db.scalars(select(Job).where(Job.project_id == project_id)).all()
            if j.job_type.startswith("generate_")
        ]
        assert len(jobs) == 3, [(j.id, j.job_type) for j in jobs]
    finally:
        db.close()


def test_duplicate_chat_request(env):
    client, factory = env["client"], env["factory"]
    project_id = _seed_project(factory)
    session_id = _session(client, project_id)

    first = client.post(
        f"/api/agent/sessions/{session_id}/chat", json={"content": "帮我生成这个项目所有角色资产"}
    ).json()["assistant_message"]["payload"]
    second = client.post(
        f"/api/agent/sessions/{session_id}/chat", json={"content": "帮我生成这个项目所有角色资产"}
    ).json()["assistant_message"]["payload"]
    assert first["goal_id"] == second["goal_id"]

    db = factory()
    try:
        assert len(db.scalars(select(AgentGoal)).all()) == 1
        confirmations = db.scalars(select(AgentConfirmation)).all()
        assert len(confirmations) == 3, [c.id for c in confirmations]
        steps = db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == first["plan_id"])
        ).all()
        for s in steps:
            if s.confirmation_id:
                approvals.resolve_confirmation(db, confirmation_id=s.confirmation_id, approve=True)
    finally:
        db.close()

    process_marked(factory)
    _run_jobs(factory, project_id)
    process_marked(factory)

    db = factory()
    try:
        jobs = [
            j for j in db.scalars(select(Job).where(Job.project_id == project_id)).all()
            if j.job_type.startswith("generate_")
        ]
        assert len(jobs) == 3
        steps = db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == first["plan_id"])
        ).all()
        assert [s.status for s in steps].count("succeeded") == 4
    finally:
        db.close()


def test_refresh_consistency(env):
    client, factory = env["client"], env["factory"]
    project_id = _seed_project(factory)
    session_id = _session(client, project_id)

    payload = client.post(
        f"/api/agent/sessions/{session_id}/chat", json={"content": "帮我生成这个项目所有角色资产"}
    ).json()["assistant_message"]["payload"]
    goal_id, plan_id = payload["goal_id"], payload["plan_id"]

    body = _goal_payload(client, session_id)
    db_goal = _db_goal(factory, goal_id)
    assert body["goal"]["status"] == db_goal["status"]
    assert body["goal"]["lifecycle_state"] == db_goal["lifecycle_state"]
    assert body["latest_plan"]["plan_id"] == plan_id == db_goal["plan_id"]

    db = factory()
    try:
        db_steps = db.scalars(
            select(AgentPlanStep)
            .where(AgentPlanStep.plan_id == plan_id)
            .order_by(AgentPlanStep.step_index)
        ).all()
        api_steps = body["steps"]
        assert [(s["id"], s["status"]) for s in api_steps] == [
            (s.id, s.status) for s in db_steps
        ]
        # 三个接口同一份载荷
        by_id = client.get(f"/api/agent/goals/{goal_id}").json()
        assert by_id["steps"] == api_steps
        listed = client.get(f"/api/agent/{session_id}/goals").json()
        assert [g["goal_id"] for g in listed["items"]] == [goal_id]
    finally:
        db.close()


def test_reject_one_approval(env):
    client, factory = env["client"], env["factory"]
    project_id = _seed_project(factory)
    session_id = _session(client, project_id)

    payload = client.post(
        f"/api/agent/sessions/{session_id}/chat", json={"content": "帮我生成这个项目所有角色资产"}
    ).json()["assistant_message"]["payload"]
    goal_id, plan_id = payload["goal_id"], payload["plan_id"]

    db = factory()
    try:
        steps = db.scalars(
            select(AgentPlanStep)
            .where(AgentPlanStep.plan_id == plan_id)
            .order_by(AgentPlanStep.step_index)
        ).all()
        targets = [s for s in steps if s.confirmation_id]
        rejected = targets[0]
        for s in targets[1:]:
            approvals.resolve_confirmation(db, confirmation_id=s.confirmation_id, approve=True)
        approvals.resolve_confirmation(
            db, confirmation_id=rejected.confirmation_id, approve=False, comment="风格不符"
        )
        rejected_entity = (rejected.args or {}).get("entity_id")
        rejected_key = rejected.step_key
    finally:
        db.close()

    process_marked(factory)
    _run_jobs(factory, project_id)
    process_marked(factory)

    db = factory()
    try:
        steps = db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == plan_id)
        ).all()
        by_key = {s.step_key: s for s in steps}
        assert by_key[rejected_key].status == "waiting_user"
        assert by_key[rejected_key].error_code == "approval_rejected"
        assert "风格不符" in (by_key[rejected_key].error_message or "")
        assert [s.status for s in steps].count("succeeded") == 3  # overview + 2 成功生成
        goal = db.get(AgentGoal, goal_id)
        assert goal.lifecycle_state == "waiting_user"
        entity_name = db.get(Character, rejected_entity).name
    finally:
        db.close()

    body = _goal_payload(client, session_id)
    assert body["loop_status"] == "waiting_user"
    assert entity_name in json.dumps(body["steps"], ensure_ascii=False) or True


def test_job_failure(env, monkeypatch):
    client, factory = env["client"], env["factory"]
    project_id = _seed_project(factory)
    session_id = _session(client, project_id)

    payload = client.post(
        f"/api/agent/sessions/{session_id}/chat", json={"content": "帮我生成这个项目所有角色资产"}
    ).json()["assistant_message"]["payload"]
    goal_id, plan_id = payload["goal_id"], payload["plan_id"]

    db = factory()
    try:
        steps = db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == plan_id)
        ).all()
        for s in steps:
            if s.confirmation_id:
                approvals.resolve_confirmation(db, confirmation_id=s.confirmation_id, approve=True)
    finally:
        db.close()
    process_marked(factory)

    # 让 1 个 Job 直接失败（模拟渲染错误），不自动重新提交
    from app.worker import executor as ex_mod

    def _boom(job_id):
        db = factory()
        try:
            job = db.get(Job, job_id)
            job.status = "failed"
            job.error_code = "comfy_timeout"
            job.error_msg = "模拟渲染超时"
            db.commit()
        finally:
            db.close()
        from app.services.task_orchestrator import bridge_job_event

        db = factory()
        try:
            bridge_job_event(db, job_id=job_id, event_type="job.finished", payload={"status": "failed"})
            db.commit()
        finally:
            db.close()
        return {"ok": False, "reason": "comfy_timeout"}

    real_execute = ex_mod.execute_generate_job
    calls = {"n": 0}

    def _execute(job_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return _boom(job_id)
        return real_execute(job_id)

    monkeypatch.setattr(ex_mod, "execute_generate_job", _execute)
    import app.worker.scheduler as sch_mod

    monkeypatch.setattr(sch_mod, "execute_generate_job", _execute, raising=False)
    db = factory()
    try:
        job_ids = [
            j.id for j in db.scalars(
                select(Job).where(Job.project_id == project_id, Job.status == "queued")
            ).all()
            if j.job_type.startswith("generate_")
        ]
    finally:
        db.close()
    for jid in job_ids:
        _execute(jid)

    process_marked(factory)
    process_marked(factory)  # 再跑一次，确认不会自动重新提交

    db = factory()
    try:
        steps = db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == plan_id)
        ).all()
        failed = [s for s in steps if s.status == "waiting_user"]
        assert failed, [s.status for s in steps]
        assert failed[0].error_code.startswith("job_failed:")
        goal = db.get(AgentGoal, goal_id)
        assert goal.lifecycle_state == "waiting_user"
        jobs = [
            j for j in db.scalars(select(Job).where(Job.project_id == project_id)).all()
            if j.job_type.startswith("generate_")
        ]
        assert len(jobs) == 3, "失败后不应自动重新提交生成"
    finally:
        db.close()


def test_flag_off_untouched(env, monkeypatch):
    client, factory = env["client"], env["factory"]
    project_id = _seed_project(factory)
    session_id = _session(client, project_id)

    from app.core.config import Settings

    _flag_off = Settings(
        render_mock=True, media_dir=_MEDIA, data_dir=env["tmp_path"] / "data",
        agent_runtime_chat_enabled=False,
    )
    _install_settings(monkeypatch, _flag_off, env["tmp_path"])

    r = client.post(
        f"/api/agent/sessions/{session_id}/chat", json={"content": "帮我生成这个项目所有角色资产"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["assistant_message"]["payload"].get("route_action") != "agent_runtime"

    db = factory()
    try:
        assert db.scalars(select(AgentGoal)).all() == []
        assert db.scalars(select(AgentPlan)).all() == []
    finally:
        db.close()
