"""待确认事项与确认答复解析测试（多轮上下文与用户确认机制整改）。

覆盖：明确拒绝、明确通过、拒绝并带修改意见、多轮修改、页面刷新恢复、
多对象歧义澄清、重复提交幂等、按钮裁决接口。
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Base, Project, ids


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    """独立数据库 + 项目 + 会话，返回 (client, session_factory, session_id, project_id)。"""
    db_file = tmp_path / "pending.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)

    # 意图识别打桩：确认答复分支在意图识别之前执行，打桩只影响兜底路径
    import app.services.intent_router as ir_mod
    from app.services.intent_router import IntentResult

    monkeypatch.setattr(
        ir_mod,
        "classify_intent",
        lambda content, **kwargs: IntentResult(
            "query", "测试桩", risk_level="low", confidence=1.0, source="test_stub"
        ),
    )

    client = TestClient(app)
    now = datetime.now()
    db = factory()
    project = Project(
        id=ids.project_id(),
        name="确认事项测试项目",
        aspect_ratio="9:16",
        creation_type="novel",
        status="draft",
        created_at=now,
        updated_at=now,
    )
    db.add(project)
    db.commit()
    project_id = project.id
    db.close()

    r = client.post(
        "/api/agent/sessions", json={"title": "确认测试", "project_id": project_id}
    )
    assert r.status_code == 201, r.text
    session_id = r.json()["id"]
    return client, factory, session_id, project_id


def _open_review(factory, session_id, project_id, version=None, round_no=1):
    from app.services import review_confirmation as rc

    db = factory()
    card = rc.to_card(
        rc.create_script_review(
            db,
            session_id=session_id,
            project_id=project_id,
            question="请确认这份剧本是否可以？",
            target_version=version,
            round_no=round_no,
        )
    )
    db.close()
    return card


def _pending(factory, session_id):
    from app.services import review_confirmation as rc

    db = factory()
    pending = rc.get_pending(db, session_id)
    result = rc.to_card(pending) if pending else None
    db.close()
    return result


def _script_state(factory, project_id):
    from app.services import review_confirmation as rc

    db = factory()
    state = rc.script_review_state(db.get(Project, project_id))
    db.close()
    return state


def _chat(client, session_id, content):
    r = client.post(f"/api/agent/sessions/{session_id}/chat", json={"content": content})
    assert r.status_code == 200, r.text
    return r.json()["assistant_message"]


def _set_draft_state(factory, session_id, project_id):
    from app.models import AgentSession
    from app.services import video_workflow as vw

    db = factory()
    vw.set_workflow_state(
        db,
        db.get(AgentSession, session_id),
        {
            "stage": vw.STAGE_SCRIPT_DRAFT,
            "project_id": project_id,
            "script_draft": "旧剧本内容" * 30,
        },
    )
    db.close()


# ---------- 测试 1：明确拒绝 ----------
def test_explicit_reject(ctx):
    client, factory, sid, pid = ctx
    _open_review(factory, sid, pid)

    msg = _chat(client, sid, "不可以。")

    payload = msg["payload"]
    assert payload["route_action"] == "confirmation_resolved"
    assert payload["resolution"] == "reject"
    assert payload["confirmation"]["status"] == "rejected"
    # 不再重复询问同一个『是否可以』
    assert "请确认这份剧本是否可以" not in msg["content"]
    state = _script_state(factory, pid)
    assert state["status"] == "rejected"
    # 后续依赖任务被后端阻断
    from app.services import pipeline_state as ps

    db = factory()
    with pytest.raises(ps.PipelineStateError):
        ps.assert_action_allowed(db, pid, "generate_character")
    db.close()


# ---------- 测试 2：明确通过 ----------
def test_explicit_approve(ctx):
    client, factory, sid, pid = ctx
    _open_review(factory, sid, pid, version=3)

    msg = _chat(client, sid, "可以。")

    assert msg["payload"]["resolution"] == "approve"
    assert msg["payload"]["confirmation"]["status"] == "approved"
    state = _script_state(factory, pid)
    assert state["status"] == "approved"
    assert state["approved_version"] == 3
    assert _pending(factory, sid) is None


# ---------- 测试 3：拒绝并带修改意见 ----------
def test_reject_with_revision(ctx, monkeypatch):
    client, factory, sid, pid = ctx
    from app.services import video_workflow as vw

    monkeypatch.setattr(vw, "revise_script", lambda *a, **k: "修改后的剧本内容" * 20)
    _set_draft_state(factory, sid, pid)
    _open_review(factory, sid, pid, version=1)

    msg = _chat(client, sid, "不可以，主角衣服不符合古代背景。")

    assert msg["payload"]["resolution"] == "reject"
    # 用户已给出修改要求，不重复追问改什么，直接进入修改流程
    assert "请告诉我需要修改的内容" not in msg["content"]
    assert "修改后的剧本内容" in msg["content"]
    assert msg["payload"]["confirmation"]["status"] == "pending"
    assert msg["payload"]["confirmation"]["target_version"] == 2


# ---------- 测试 4：多轮修改 ----------
def test_multi_round_revision(ctx, monkeypatch):
    client, factory, sid, pid = ctx
    from app.services import video_workflow as vw

    monkeypatch.setattr(vw, "revise_script", lambda *a, **k: "第二版剧本内容" * 20)
    first = _open_review(factory, sid, pid, version=1)

    msg1 = _chat(client, sid, "不可以。")
    assert "请告诉我需要修改的内容" in msg1["content"]

    _set_draft_state(factory, sid, pid)
    msg2 = _chat(client, sid, "剧情反转不够强烈。")
    assert msg2["payload"]["workflow"] == "script_revised"

    from app.models import AgentConfirmation

    db = factory()
    old = db.get(AgentConfirmation, first["confirmation_id"])
    assert old.status == "rejected"
    db.close()

    pending = _pending(factory, sid)
    assert pending is not None
    assert pending["target_version"] == 2


# ---------- 测试 5：页面刷新 ----------
def test_state_survives_refresh(ctx):
    client, factory, sid, pid = ctx
    _open_review(factory, sid, pid, version=3)
    _chat(client, sid, "不可以。")

    r = client.get(f"/api/agent/sessions/{sid}/reviews")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items
    assert items[0]["status"] == "rejected"
    assert items[0]["target_version"] == 3
    assert _pending(factory, sid) is None


# ---------- 测试 6：上下文隔离 ----------
def test_multiple_pending_requires_clarify(ctx):
    client, factory, sid, pid = ctx
    _open_review(factory, sid, pid, version=1)
    _open_review(factory, sid, pid, version=2, round_no=2)

    msg = _chat(client, sid, "不可以。")

    assert msg["payload"]["route_action"] == "confirmation_clarify"
    from app.models import AgentConfirmation

    db = factory()
    count = (
        db.query(AgentConfirmation)
        .filter(
            AgentConfirmation.session_id == sid,
            AgentConfirmation.status == "pending",
        )
        .count()
    )
    db.close()
    assert count == 2


def test_ambiguous_answer_does_not_approve(ctx):
    client, factory, sid, pid = ctx
    _open_review(factory, sid, pid, version=1)

    from app.services import review_confirmation as rc

    parsed = rc.parse_confirmation_response("好的，但是再看看第三幕吧，我觉得有问题")
    assert parsed["resolution"] != "approve"
    assert _pending(factory, sid) is not None


# ---------- 测试 7：幂等 ----------
def test_resolve_idempotent(ctx):
    client, factory, sid, pid = ctx
    card = _open_review(factory, sid, pid, version=1)

    from app.models import AgentConfirmation, AgentMessage
    from app.services import review_confirmation as rc

    db = factory()
    confirmation = db.get(AgentConfirmation, card["confirmation_id"])
    rc.resolve(db, confirmation, resolution="reject", user_response="不可以")
    first_count = (
        db.query(AgentMessage)
        .filter(AgentMessage.session_id == sid, AgentMessage.role == "approval")
        .count()
    )
    rc.resolve(db, confirmation, resolution="reject", user_response="不可以")
    second_count = (
        db.query(AgentMessage)
        .filter(AgentMessage.session_id == sid, AgentMessage.role == "approval")
        .count()
    )
    assert first_count == second_count == 1
    db.close()

    # 已裁决后再创建同版本同轮次 → 新建；再重复创建 → 幂等复用
    again = _open_review(factory, sid, pid, version=1)
    assert again["confirmation_id"] != card["confirmation_id"]
    again2 = _open_review(factory, sid, pid, version=1)
    assert again2["confirmation_id"] == again["confirmation_id"]


# ---------- 按钮裁决接口 ----------
def test_button_resolve_api(ctx):
    client, factory, sid, pid = ctx
    card = _open_review(factory, sid, pid, version=2)

    r = client.post(
        f"/api/agent/sessions/{sid}/reviews/{card['confirmation_id']}/resolve",
        json={"action": "approve", "comment": "可以"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"

    r2 = client.post(
        f"/api/agent/sessions/{sid}/reviews/{card['confirmation_id']}/resolve",
        json={"action": "approve"},
    )
    assert r2.status_code == 409
