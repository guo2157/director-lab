"""「本镜主体角色」口径唯一性验收测试。

背景（真实故障）：同一个概念在三个地方各写了一份实现，逐渐分叉：

- 注入器 `reference_resolver`：`character_id` 或 名字出现在 `image_prompt`
  或 **名字出现在 `dialogue` 正文**
- 硬门禁 `prompt_compiler.audit_reference_slots`：`character_id` + 对白说话人
- 提示词编译器 `prompt_compiler._shot_characters`：`character_id` + 对白说话人

后果：`《小马过河》` 镜头 2「@小马 向牛伯伯询问河水深浅」里，
`牛伯伯` 只出现在台词正文（"牛伯伯，河水深不深？"），注入器据此判定「在镜内」
并把他塞进 `ref_character_2`；门禁按自己的主体集合判定 → `identity_reference_leak`
→ 整个生成请求 409，分镜图彻底出不来。

本文件锁定：三处必须共用 `shot_refs.shot_subject_characters` 一份定义。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Character, Project, Scene, Shot, ids


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'subject.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = maker()
    now = datetime.now()
    project = Project(
        id=ids.project_id(), name="主体角色口径", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add(project)
    session.flush()
    scene = Scene(id=ids.scene_id(), project_id=project.id, title="小河与河岸", seq=1)
    session.add(scene)
    chars = {}
    for name in ("小马", "老马", "牛伯伯", "小松鼠"):
        c = Character(id=ids.character_id(), project_id=project.id, name=name,
                      appearance=f"{name}外观")
        session.add(c)
        chars[name] = c
    session.commit()
    yield session, project, scene, chars
    session.close()
    engine.dispose()


def _shot(db, scene, chars, *, character_id, image_prompt="", dialogue="",
          segments=None) -> Shot:
    shot = Shot(
        id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1,
        shot_kind="dialogue" if dialogue else "visual", duration=5,
        character_id=character_id, image_prompt=image_prompt,
        dialogue=dialogue, dialogue_segments=segments or [],
        created_at=datetime.now(), updated_at=datetime.now(),
    )
    db.add(shot)
    db.commit()
    return shot


def test_name_only_in_dialogue_text_is_not_a_subject(db, ):
    """台词正文提到名字 ≠ 在画面里：不得进入主体角色集合。"""
    from app.services.shot_refs import shot_subject_characters

    session, project, scene, chars = db
    shot = _shot(
        session, scene, chars,
        character_id=chars["小马"].id,
        image_prompt="@小马 站在画面左侧的河边，向画面右侧的牛伯伯询问河水深浅，中景",
        dialogue="牛伯伯，河水深不深？",
    )
    names = [c.name for c in shot_subject_characters(session, shot)]
    assert names == ["小马"], names


def test_dialogue_segment_speaker_is_a_subject(db):
    """对白分段说话人算主体角色（有台词必然在画面里）。"""
    from app.services.shot_refs import shot_subject_characters

    session, project, scene, chars = db
    shot = _shot(
        session, scene, chars,
        character_id=chars["小马"].id,
        image_prompt="@小马 在河边与牛伯伯对话，中景",
        dialogue="牛伯伯，河水深不深？",
        segments=[
            {"speaker": "小马", "speaker_id": chars["小马"].id, "text": "牛伯伯，河水深不深？"},
            {"speaker": "牛伯伯", "speaker_id": chars["牛伯伯"].id, "text": "不深，刚过膝盖。"},
        ],
    )
    names = [c.name for c in shot_subject_characters(session, shot)]
    assert names == ["小马", "牛伯伯"], names


def test_prompt_at_reference_is_a_subject(db):
    """提示词显式 @引用 的角色算主体角色（作者显式点名要求一致性）。"""
    from app.services.shot_refs import shot_subject_characters

    session, project, scene, chars = db
    shot = _shot(
        session, scene, chars,
        character_id=chars["小马"].id,
        image_prompt="@小马 与 @小松鼠 在河岸对视，中景",
        dialogue="",
    )
    names = [c.name for c in shot_subject_characters(session, shot)]
    assert names == ["小马", "小松鼠"], names


def test_video_prompt_reference_counts_too(db):
    """视频提示词里的 @引用 同样算：图/视频提示词可能由不同阶段渲染。"""
    from app.services.shot_refs import shot_subject_characters

    session, project, scene, chars = db
    shot = _shot(
        session, scene, chars,
        character_id=chars["小马"].id,
        image_prompt="小马站在河边，中景",
        dialogue="",
    )
    shot.video_prompt = "@小马 与 @老马 并肩向前走"
    session.commit()
    names = [c.name for c in shot_subject_characters(session, shot)]
    assert names == ["小马", "老马"], names


def test_resolver_and_gate_agree_on_subject_set(db):
    """注入器与门禁必须对同一镜头得出相同主体集合（回归核心断言）。"""
    from app.services.prompt_compiler import _shot_characters
    from app.services.reference_resolver import resolve_shot_references
    from app.services.shot_refs import shot_subject_characters

    session, project, scene, chars = db
    shot = _shot(
        session, scene, chars,
        character_id=chars["小马"].id,
        image_prompt="@小马 站在画面左侧的河边，向画面右侧的牛伯伯询问河水深浅，中景",
        dialogue="牛伯伯，河水深不深？",
    )
    subject_ids = {c.id for c in shot_subject_characters(session, shot)}
    compiler_ids = {c.id for c in _shot_characters(session, shot)}
    assert subject_ids == compiler_ids == {chars["小马"].id}

    # 注入器只能给主体角色分配身份槽
    manifest = resolve_shot_references(session, shot.id, project.id)
    identity_slots = [
        s for s in manifest.slots if s.slot.startswith(("ref_identity", "ref_character_"))
    ]
    for slot in identity_slots:
        assert slot.note == "小马", f"{slot.slot} 注入了非主体角色 {slot.note}"


def test_gate_rejects_non_subject_in_identity_slot(db):
    """门禁仍必须拦住真泄漏：非主体角色图进入身份槽要报 identity_reference_leak。

    放宽口径不能把防护一起放开——这条保证「统一口径」不是「取消门禁」。
    """
    from app.models import Asset
    from app.services.prompt_compiler import audit_reference_slots

    session, project, scene, chars = db
    # 给牛伯伯一个定妆图资产
    asset = Asset(
        id=ids.asset_id(), project_id=project.id, asset_type="character",
        name="牛伯伯定妆", version=1, file_path="/tmp/niu.png", validity="current",
    )
    session.add(asset)
    session.flush()
    chars["牛伯伯"].portrait_asset_id = asset.id
    session.commit()

    shot = _shot(
        session, scene, chars,
        character_id=chars["小马"].id,
        image_prompt="@小马 站在河边，中景",
        dialogue="河水深不深？",
    )
    issues = audit_reference_slots(
        session, shot, {"ref_identity": "/tmp/niu.png"})
    codes = [i["code"] for i in issues]
    assert "identity_reference_leak" in codes, issues
