"""Context Manager 与 Intent Resolver 单元测试（意图识别改造 Phase 2）。
覆盖：
- 焦点读写持久化（context_state JSON 字段）
- 项目切换时清空焦点/操作上下文（跨项目污染防护）
- 焦点验证：实体不存在/不属于当前项目/过期 均不得使用
- Intent Resolver：显式 ID 定位 > 焦点回退 > 澄清，不猜测执行
- 目标歧义（镜号命中多个）时输出澄清而非挑一个
"""
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Character, Project, Scene, Shot, ids
from app.models.agent import AgentSession
from app.services import context_manager
from app.services.intent_resolver import resolve_intent
from app.services.intent_router import IntentResult


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'ctx.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    now = datetime.now()
    project = Project(
        id=ids.project_id(), name="项目A", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    other = Project(
        id=ids.project_id(), name="项目B", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    scene = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=1, title="场景一")
    scene2 = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=2, title="场景二")
    shot1 = Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1, shot_kind="normal", duration=3)
    # 第 2 镜在两个场景各出现一次：验证歧义不挑一个
    shot2a = Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=2, shot_kind="normal", duration=3)
    shot2b = Shot(id=ids.shot_id(), scene_id=scene2.id, episode=1, seq=2, shot_kind="normal", duration=3)
    char_a = Character(id=ids.character_id(), project_id=project.id, name="主角", description="")
    char_b = Character(id=ids.character_id(), project_id=other.id, name="别人家的主角", description="")
    ags = AgentSession(
        id=ids.agent_session_id(), title="测试会话", project_id=project.id,
        status="active", created_at=now, updated_at=now,
    )
    # 先插入 Project 并 flush：character/scene 与 project 无 ORM relationship，
    # flush 按表序插入时 character（字母序）先于 project 会触发外键约束失败
    session.add_all([project, other])
    session.flush()
    session.add_all(
        [scene, scene2, shot1, shot2a, shot2b, char_a, char_b, ags]
    )
    session.commit()
    yield session, ags, project.id, other.id, char_a.id, char_b.id, shot1.id
    session.close()


# --- Context Manager ---


def test_focus_roundtrip(db):
    """焦点写入后可读回，字段完整（含实体 ID，不只名称）。"""
    session, ags, pid, _oid, cid, _cb, _sid = db
    context_manager.update_focus(
        ags, entity_type="character", entity_id=cid, entity_name="主角", updated_seq=5
    )
    ctx = context_manager.load_context(ags)
    assert ctx.focus is not None
    assert ctx.focus.entity_id == cid and ctx.focus.entity_type == "character"
    assert ctx.focus.entity_name == "主角" and ctx.focus.updated_seq == 5


def test_clear_on_project_switch(db):
    """项目切换时清空焦点与操作上下文，防止跨项目污染。"""
    session, ags, pid, oid, cid, _cb, _sid = db
    context_manager.update_focus(
        ags, entity_type="character", entity_id=cid, entity_name="主角", updated_seq=1
    )
    context_manager.update_operation(
        ags, operation="modify_prompt", entity_id=cid, last_result="success", updated_seq=1
    )
    assert context_manager.clear_on_project_switch(ags) is True
    ctx = context_manager.load_context(ags)
    assert ctx.focus is None and ctx.operation is None
    # 再次清理（已空）返回 False
    assert context_manager.clear_on_project_switch(ags) is False


def test_validate_focus_rejects_foreign_entity(db):
    """焦点实体不属于当前项目时验证失败，不得用于指代消解。"""
    session, ags, pid, _oid, _cid, cbid, _sid = db
    context_manager.update_focus(
        ags, entity_type="character", entity_id=cbid, entity_name="别人家的主角", updated_seq=1
    )
    assert context_manager.validate_focus(session, ags, current_seq=2) is None


def test_validate_focus_rejects_expired(db):
    """焦点超过有效期后不再作为消歧回退。"""
    session, ags, pid, _oid, cid, _cb, _sid = db
    context_manager.update_focus(
        ags, entity_type="character", entity_id=cid, entity_name="主角", updated_seq=1
    )
    assert (
        context_manager.validate_focus(
            session, ags, current_seq=1 + context_manager.FOCUS_MAX_AGE_SEQ + 1
        )
        is None
    )


def test_validate_focus_ok(db):
    """有效期内且属于当前项目的焦点验证通过。"""
    session, ags, pid, _oid, cid, _cb, _sid = db
    context_manager.update_focus(
        ags, entity_type="character", entity_id=cid, entity_name="主角", updated_seq=1
    )
    focus = context_manager.validate_focus(session, ags, current_seq=2)
    assert focus is not None and focus.entity_id == cid


# --- Intent Resolver ---


def _gen_intent() -> IntentResult:
    return IntentResult(
        "generate_request", "用户要求重新生成",
        goal="重新生成", risk_level="low", confidence=0.95,
    )


def test_resolve_explicit_id(db):
    """消息含显式实体 ID 时精确定位并更新焦点。"""
    session, ags, pid, _oid, cid, _cb, _sid = db
    resolved = resolve_intent(
        session, ags, f"重新生成角色 {cid}", _gen_intent(), current_seq=1
    )
    assert resolved.target is not None
    assert resolved.target["entity_id"] == cid
    assert resolved.resolution_path == "explicit_id"
    assert resolved.focus_updated is True
    # 焦点已持久化到会话
    ctx = context_manager.load_context(ags)
    assert ctx.focus is not None and ctx.focus.entity_id == cid


def test_resolve_focus_fallback(db):
    """无显式 ID 但有有效焦点时回退到焦点（『重新生成』指代消解）。"""
    session, ags, pid, _oid, cid, _cb, _sid = db
    context_manager.update_focus(
        ags, entity_type="character", entity_id=cid, entity_name="主角", updated_seq=1
    )
    resolved = resolve_intent(
        session, ags, "重新生成", _gen_intent(), current_seq=2
    )
    assert resolved.target is not None
    assert resolved.target["entity_id"] == cid
    assert resolved.resolution_path == "focus_context"


def test_resolve_no_target_clarifies(db):
    """无显式 ID 且无有效焦点时澄清，不猜测执行。"""
    session, ags, pid, _oid, _cid, _cb, _sid = db
    resolved = resolve_intent(
        session, ags, "重新生成", _gen_intent(), current_seq=1
    )
    assert resolved.target is None
    assert resolved.requires_clarification is True
    assert resolved.clarification_question is not None


def test_resolve_ambiguous_shot_clarifies(db):
    """『第 2 镜』命中多个镜头时澄清而非挑一个。"""
    session, ags, pid, _oid, _cid, _cb, _sid = db
    resolved = resolve_intent(
        session, ags, "重新生成第 2 镜的关键帧", _gen_intent(), current_seq=1
    )
    assert resolved.target is None
    assert resolved.requires_clarification is True
    assert resolved.clarification_code == "shot_ambiguous"


def test_resolve_foreign_id_rejected(db):
    """显式 ID 属于其他项目时拒绝并澄清，不跨项目执行。"""
    session, ags, pid, _oid, _cid, cbid, _sid = db
    resolved = resolve_intent(
        session, ags, f"重新生成角色 {cbid}", _gen_intent(), current_seq=1
    )
    assert resolved.target is None
    assert resolved.requires_clarification is True
    assert resolved.clarification_code == "entity_not_in_project"


def test_resolve_query_passthrough(db):
    """query/analysis 等无实体目标意图直接透传，不触发目标定位。"""
    session, ags, pid, _oid, _cid, _cb, _sid = db
    resolved = resolve_intent(
        session, ags, "项目进展如何", IntentResult("query", "进度查询"), current_seq=1
    )
    assert resolved.target is None and resolved.requires_clarification is False
