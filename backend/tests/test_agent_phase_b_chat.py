"""Phase B B6 验收：/chat 接入 Agent Runtime（开关后面）。

覆盖：
- 开关关闭时行为与改动前一致（黄金响应 + 分支从不进入 + 不产生 agent_goal）
- 开关打开时批量资产生成进入 Runtime，建立 Goal/Plan，返回 route_action=agent_runtime
- 单实体生成、无项目不接管
- 重复请求复用同一 Goal
- 审批后 tick：所有 TaskStage 都挂在 goal.task_id 上
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Asset, Job, Scene, ScriptVersion, Shot, Workflow, ids
from app.models.agent import AgentGoal, AgentPlanStep, AgentTask
from app.models.task import Task, TaskStage
from app.services import planner as planner_mod
from app.services.agent_runtime import tick

_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "chat_baseline_flag_off.json"

# 开关关闭时用到的固定输入（问答 / 导航 / 单镜头生成 / 批量资产生成）
_FLAG_OFF_INPUTS = [
    "这个项目现在什么进度？",
    "去分镜页",
    "为第 1 镜生成关键帧",
    "帮我生成这个项目所有角色资产",
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    import app.core.database as db_mod
    from app.core.config import clear_settings_cache
    from app.main import app

    engine = db_mod.make_engine(f"sqlite:///{tmp_path / 'b6chat.db'}")
    from app.models import Base

    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    clear_settings_cache()
    yield TestClient(app)
    clear_settings_cache()


@pytest.fixture()
def project_env(client, tmp_path, monkeypatch):
    """已解析项目：3 个角色（均无定妆图）+ 场景基准图 + 镜头 + 默认图像工作流。"""
    import app.core.database as db_mod

    db = db_mod.SessionLocal()
    now = datetime.now()
    from app.models import Character, Project

    project = Project(
        id=ids.project_id(), name="B6 项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    db.add(project)
    db.flush()
    script = ScriptVersion(
        id=ids.script_version_id(), project_id=project.id, version=1,
        content_snapshot="测试剧本内容", content_hash="hash-v1", created_at=now,
    )
    db.add(script)
    db.flush()
    project.current_script_version_id = script.id
    from app.models import ParseVersion

    pv = ParseVersion(
        id=ids.parse_version_id(), project_id=project.id,
        script_version_id=script.id, version=1, status="current", created_at=now,
    )
    db.add(pv)
    db.flush()
    project.current_parse_version_id = pv.id
    script_hash = hashlib.sha256(script.content_snapshot.encode("utf-8")).hexdigest()
    script.content_hash = script_hash
    project.estimated_text_hash = script_hash

    for name in ("角色甲", "角色乙", "角色丙"):
        db.add(Character(id=ids.character_id(), project_id=project.id, name=name))
    scene = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=1, title="场景一")
    db.add(scene)
    db.flush()
    db.add(
        Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1, shot_kind="normal", duration=3)
    )
    baseline = Asset(
        id=ids.asset_id(), project_id=project.id, asset_type="scene", name="场景一基准图",
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
    db.add(Job(id=ids.job_id(), project_id=project.id, job_type="parse", status="succeeded", created_at=now))
    db.commit()
    project_id = project.id
    db.close()

    session = client.post(
        "/api/agent/sessions", json={"title": "B6 会话", "project_id": project_id}
    )
    assert session.status_code == 201, session.text
    return {"project_id": project_id, "session_id": session.json()["id"]}


@pytest.fixture()
def stub_intent(monkeypatch):
    """意图识别打桩：生成类消息固定为 generate_request / asset.generate。"""
    import app.services.intent_router as ir_mod
    from app.services.intent_router import IntentResult

    def _classify(content, **kwargs):
        if "生成" in content or "分镜图" in content:
            return IntentResult(
                "generate_request",
                "测试桩：批量生成请求",
                goal="批量生成项目资产",
                subcategory="asset.generate",
                risk_level="medium",
                confidence=1.0,
                source="test_stub",
            )
        return IntentResult("query", "测试桩：普通对话", risk_level="low", confidence=1.0, source="test_stub")

    monkeypatch.setattr(ir_mod, "classify_intent", _classify)

    def _no_llm(self, messages, **kwargs):
        return "这是助手的普通回复"

    import app.api.v1.agent as agent_mod

    monkeypatch.setattr(agent_mod.LLMClient, "chat_messages", _no_llm)
    return _classify


@pytest.fixture()
def enable_runtime(monkeypatch):
    from app.core.config import clear_settings_cache

    monkeypatch.setenv("DLAB_AGENT_RUNTIME_CHAT_ENABLED", "true")
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.fixture()
def template_plan(monkeypatch):
    """B7 之前的临时模板：overview + 每个缺资产的实体一个 request_generate。"""
    import app.core.database as db_mod
    from app.models import Character

    def _install(project_id: str, *, target: str = "character"):
        db = db_mod.SessionLocal()
        entity_ids = [
            c.id
            for c in db.scalars(select(Character).where(Character.project_id == project_id)).all()
        ]
        db.close()

        def _build(db_, *, goal_id, description, project_id=None, previous_plan=None):
            plan = planner_mod.Plan(
                plan_id=planner_mod.agent_plan_id(),
                goal_id=goal_id,
                revision=(previous_plan.revision + 1) if previous_plan else 1,
                metadata={"description": description, "project_id": project_id},
            )
            plan.steps.append(
                planner_mod.PlanStep(
                    step_key="project_overview", tool_name="project_overview",
                    description="项目概览", args={"project_id": project_id}, estimated_timeout=30,
                )
            )
            for eid in entity_ids:
                plan.steps.append(
                    planner_mod.PlanStep(
                        step_key=f"asset:{target}:{eid}",
                        tool_name="request_generate",
                        description=f"生成{target}资产 {eid}",
                        args={"project_id": project_id, "entity_id": eid, "target": target},
                        depends_on=["project_overview"],
                        estimated_timeout=60,
                    )
                )
            report = planner_mod.validate_plan(plan, project_id=project_id)
            if report.passed:
                planner_mod.save_plan(db_, plan, supersede_previous=True)
                planner_mod.save_plan_steps(db_, plan)
            return plan, report

        monkeypatch.setattr(planner_mod, "build_and_save_plan", _build)

    return _install


def _db():
    import app.core.database as db_mod

    return db_mod.SessionLocal()


def _golden() -> dict:
    if not _GOLDEN_PATH.exists():  # pragma: no cover - 首次生成由脚本写入
        return {}
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


def test_flag_off_behavior_unchanged(client, project_env, stub_intent, monkeypatch):
    """开关关闭：分支从不进入，响应与改动前黄金快照一致，且不产生 agent_goal。"""
    import app.api.v1.agent as agent_mod

    def _boom(*args, **kwargs):
        raise AssertionError("开关关闭时不应进入 Agent Runtime 分支")

    monkeypatch.setattr(agent_mod, "_handle_agent_runtime_route", _boom, raising=False)

    golden = _golden()
    if not golden and os.environ.get("DLAB_WRITE_GOLDEN") == "1":
        # 一次性生成：分支被上面的 _boom 拦住，因此记录的响应等价于接入 Runtime 之前的行为
        golden = {}
        sid = project_env["session_id"]
        for content in _FLAG_OFF_INPUTS:
            body = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": content}).json()
            payload = body["assistant_message"].get("payload") or {}
            golden[content] = {
                "content": body["assistant_message"]["content"],
                "route_action": payload.get("route_action"),
            }
        _GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN_PATH.write_text(
            json.dumps(golden, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    assert golden, "缺少黄金响应快照 tests/fixtures/chat_baseline_flag_off.json"
    sid = project_env["session_id"]

    for content in _FLAG_OFF_INPUTS:
        r = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": content})
        assert r.status_code == 200, r.text
        body = r.json()
        expected = golden[content]
        assert body["assistant_message"]["content"] == expected["content"]
        payload = body["assistant_message"].get("payload") or {}
        assert payload.get("route_action") == expected["route_action"]
        assert payload.get("goal_id") is None
        assert payload.get("lifecycle_state") is None

    db = _db()
    try:
        assert db.scalars(select(AgentGoal)).all() == []
    finally:
        db.close()


def test_runtime_route_creates_goal_and_replies(
    client, project_env, stub_intent, enable_runtime, template_plan
):
    template_plan(project_env["project_id"])
    sid = project_env["session_id"]
    r = client.post(
        f"/api/agent/sessions/{sid}/chat", json={"content": "帮我生成这个项目所有角色资产"}
    )
    assert r.status_code == 200, r.text
    payload = r.json()["assistant_message"]["payload"]
    assert payload["route_action"] == "agent_runtime"
    assert payload["goal_id"]
    assert payload["plan_id"]
    assert payload["lifecycle_state"] == "waiting_approval"
    assert "审批" in r.json()["assistant_message"]["content"]

    db = _db()
    try:
        # /chat 自己写一条回复；run_agent_loop 走 _tick_locked，不应再额外写进度消息
        from app.models.agent import AgentMessage

        messages = db.scalars(
            select(AgentMessage).where(
                AgentMessage.session_id == sid, AgentMessage.role == "assistant"
            )
        ).all()
        assert len(messages) == 1, [m.content for m in messages]

        goal = db.get(AgentGoal, payload["goal_id"])
        assert goal is not None
        assert goal.agent_task_id, "Goal 必须绑定到 /chat 创建的 AgentTask"
        assert goal.task_id, "Goal 必须绑定到统一 Task"
        assert db.get(AgentTask, goal.agent_task_id) is not None
        assert db.get(Task, goal.task_id) is not None
        steps = db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == payload["plan_id"])
        ).all()
        assert [s.status for s in steps].count("waiting_approval") == 3
    finally:
        db.close()


def test_single_entity_generation_not_routed(
    client, project_env, stub_intent, enable_runtime, template_plan
):
    template_plan(project_env["project_id"])
    sid = project_env["session_id"]
    from app.models import Character

    db = _db()
    try:
        chr_id = db.scalars(select(Character)).first().id
    finally:
        db.close()

    r = client.post(
        f"/api/agent/sessions/{sid}/chat", json={"content": f"给 {chr_id} 生成定妆图"}
    )
    assert r.status_code == 200, r.text
    payload = r.json()["assistant_message"]["payload"]
    assert payload["route_action"] != "agent_runtime"

    db = _db()
    try:
        assert db.scalars(select(AgentGoal)).all() == []
    finally:
        db.close()


def test_no_project_not_routed(client, stub_intent, enable_runtime, monkeypatch):
    sid = client.post("/api/agent/sessions", json={"title": "无项目"}).json()["id"]
    r = client.post(
        f"/api/agent/sessions/{sid}/chat", json={"content": "帮我生成这个项目所有角色资产"}
    )
    assert r.status_code == 200, r.text
    payload = r.json()["assistant_message"]["payload"]
    assert payload["route_action"] != "agent_runtime"

    db = _db()
    try:
        assert db.scalars(select(AgentGoal)).all() == []
    finally:
        db.close()


def test_repeat_request_reuses_goal(
    client, project_env, stub_intent, enable_runtime, template_plan
):
    template_plan(project_env["project_id"])
    sid = project_env["session_id"]
    first = client.post(
        f"/api/agent/sessions/{sid}/chat", json={"content": "帮我生成这个项目所有角色资产"}
    ).json()["assistant_message"]["payload"]
    second = client.post(
        f"/api/agent/sessions/{sid}/chat", json={"content": "帮我生成这个项目所有角色资产"}
    ).json()["assistant_message"]["payload"]

    assert first["goal_id"] == second["goal_id"]
    db = _db()
    try:
        assert len(db.scalars(select(AgentGoal)).all()) == 1
    finally:
        db.close()


def test_job_steps_attach_to_goal_task(
    client, project_env, stub_intent, enable_runtime, template_plan
):
    template_plan(project_env["project_id"])
    sid = project_env["session_id"]
    payload = client.post(
        f"/api/agent/sessions/{sid}/chat", json={"content": "帮我生成这个项目所有角色资产"}
    ).json()["assistant_message"]["payload"]
    goal_id = payload["goal_id"]

    db = _db()
    try:
        steps = db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == payload["plan_id"])
        ).all()
        confirmation_ids = [s.confirmation_id for s in steps if s.confirmation_id]
        goal_task_id = db.get(AgentGoal, goal_id).task_id
    finally:
        db.close()
    assert len(confirmation_ids) == 3

    from app.services import approvals

    db = _db()
    try:
        for cid in confirmation_ids:
            approvals.resolve_confirmation(db, confirmation_id=cid, approve=True)
    finally:
        db.close()

    tick(None, goal_id, trigger="worker_resume")

    db = _db()
    try:
        stages = db.scalars(select(TaskStage).where(TaskStage.task_id == goal_task_id)).all()
        assert len(stages) == 3, [s.id for s in stages]
        assert {s.task_id for s in stages} == {goal_task_id}
        steps = db.scalars(
            select(AgentPlanStep).where(AgentPlanStep.plan_id == payload["plan_id"])
        ).all()
        assert [s.status for s in steps].count("waiting_job") == 3
    finally:
        db.close()
