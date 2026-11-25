"""Phase 4 §9 验收：多道具编译阻断与 compiler/resolver 槽位一致性（P1-6）。

缺陷背景：
- 道具槽位池 ``ref_prop`` / ``ref_prop_2`` 在 ``prompt_compiler`` 与
  ``reference_resolver`` 里各写了一份，历史上曾出现「提示词说只出现 A、B，
  参考图只注入 A」的口径分叉（P0-4），改造后用本文件锁死两条链路的一致性。
- ``reference_capacity_exceeded`` 属硬阻断（``generate._HARD_BLOCKING``），
  参考图总数超过工作流上限时分镜图生成直接 409，不静默丢图。

覆盖：
1. 两个道具 → 两条链路给出同一份 (asset_id → slot) 映射（P1-6 一致性）
2. 三个道具 → compiler 封顶 2 槽并告警；resolver 继续产出 ref_prop_3（已知差异）
3. 参考图总数超过工作流上限 → reference_capacity_exceeded 硬阻断 409
4. 工作流未声明 ref_prop → 道具降级文字描述并告警，不阻断整批出图
5. API 端到端：≥2 道具场景下锚定图失效 → POST generate 返回 409
   ``prompt_validation_failed``（含 reference_asset_not_current 明细）
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.database import make_engine
from app.models import Asset, Base, Character, Project, Prop, Scene, Shot, Workflow, ids
from app.services.prompt_compiler import compile_storyboard_image
from app.services.reference_resolver import resolve_shot_references


@pytest.fixture()
def db(tmp_path):
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path / 'multi_prop.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


def _media(tmp_path, name: str) -> str:
    """生成真实存在的图片文件，供文件可读性校验通过。"""
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return str(path)


def _workflow(capabilities: dict, name: str = "多道具验收工作流") -> Workflow:
    return Workflow(
        id=ids.workflow_id(),
        name=name,  # NOT NULL 且无默认值，建行时必须显式给值
        workflow_type="image",
        version=1,
        capabilities=capabilities,
        definition={"nodes": []},
        is_default=True,
    )


def _project(db, name: str = "多道具验收") -> Project:
    p = Project(
        id=ids.project_id(), name=name, creation_type="script",
        aspect_ratio="9:16", status="active", assets_review_status="approved",
    )
    db.add(p)
    db.commit()
    return p


def _character(db, project, tmp_path, name: str = "小马") -> Character:
    a = Asset(
        id=ids.asset_id(), project_id=project.id, asset_type="character",
        name=name, version=1, validity="current",
        file_path=_media(tmp_path, f"chr_{ids.asset_id()}.png"),
    )
    db.add(a)
    db.flush()
    c = Character(
        id=ids.character_id(), project_id=project.id, name=name,
        appearance=f"{name}的外观描述", portrait_asset_id=a.id,
    )
    db.add(c)
    db.commit()
    return c


def _scene(db, project, tmp_path, title: str = "仓库") -> Scene:
    a = Asset(
        id=ids.asset_id(), project_id=project.id, asset_type="scene",
        name=f"{title}-基准图", version=1, validity="current",
        file_path=_media(tmp_path, f"scn_{ids.asset_id()}.png"),
    )
    db.add(a)
    db.flush()
    s = Scene(
        id=ids.scene_id(), project_id=project.id, episode=1, seq=1,
        title=title, description="木质结构的仓库，堆着干草", baseline_asset_id=a.id,
    )
    db.add(s)
    db.commit()
    return s


def _prop(db, project, tmp_path, name: str, *, validity: str = "current",
          anchor_required: bool = True) -> Prop:
    a = Asset(
        id=ids.asset_id(), project_id=project.id, asset_type="prop",
        name=f"{name}-锚定图", version=1, validity=validity,
        file_path=_media(tmp_path, f"prop_{ids.asset_id()}.png"),
    )
    db.add(a)
    db.flush()
    p = Prop(
        id=ids.prop_id(), project_id=project.id, name=name,
        description=f"{name}的描述", appearance=f"{name}的外观",
        anchor_required=anchor_required, anchor_asset_id=a.id,
    )
    db.add(p)
    db.commit()
    return p


def _shot(db, scene, character, prop_names: list[str]) -> Shot:
    s = Shot(
        id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1,
        character_id=character.id, shot_kind="visual", duration=5,
        image_prompt="小马走进仓库", props=list(prop_names),
    )
    db.add(s)
    db.commit()
    return s


# ---------- 1. compiler / resolver 道具槽位一致性（P1-6） ----------
def test_two_props_compiler_and_resolver_share_slot_mapping(db, tmp_path):
    """两个道具：两条链路必须给出同一份 (asset_id → slot) 映射。"""
    project = _project(db)
    scene = _scene(db, project, tmp_path)
    hero = _character(db, project, tmp_path)
    _prop(db, project, tmp_path, "古剑")
    _prop(db, project, tmp_path, "草帽")
    shot = _shot(db, scene, hero, ["古剑", "草帽"])

    wf = _workflow({"reference_slots": ["ref_scene", "ref_identity", "ref_prop", "ref_prop_2"]})
    compiled = compile_storyboard_image(db, shot.id, workflow=wf)
    compiler_map = {
        slot["approved_asset_file_id"]: slot["slot_key"]
        for slot in compiled.reference_slots
        if slot.get("entity_type") == "prop"
    }
    assert sorted(compiler_map.values()) == ["ref_prop", "ref_prop_2"]

    manifest = resolve_shot_references(db, shot.id, project.id)
    resolver_map = {
        s.asset_id: s.slot for s in manifest.slots if s.asset_type == "prop"
    }
    assert resolver_map == compiler_map, (
        f"compiler 与 resolver 的道具槽位映射必须一致：{compiler_map} vs {resolver_map}"
    )


# ---------- 2. 三道具：compiler 封顶 2 槽并告警 ----------
def test_three_props_capped_to_two_slots_with_warning(db, tmp_path):
    """第三个道具超出 ref_prop 池，compiler 只告警不阻断；resolver 仍产出 ref_prop_3。"""
    project = _project(db)
    scene = _scene(db, project, tmp_path)
    hero = _character(db, project, tmp_path)
    for name in ("古剑", "草帽", "灯笼"):
        _prop(db, project, tmp_path, name)
    shot = _shot(db, scene, hero, ["古剑", "草帽", "灯笼"])

    wf = _workflow({"reference_slots": ["ref_scene", "ref_identity", "ref_prop", "ref_prop_2"]})
    compiled = compile_storyboard_image(db, shot.id, workflow=wf)
    prop_slots = [
        slot["slot_key"] for slot in compiled.reference_slots
        if slot.get("entity_type") == "prop"
    ]
    assert len(prop_slots) == 2, f"道具槽位池只有 2 个，实际 {prop_slots}"
    assert sorted(prop_slots) == ["ref_prop", "ref_prop_2"]
    assert any(
        w.startswith("prop_reference_unsupported")
        for w in compiled.warnings
    ), f"越界道具必须显式告警，实际 {compiled.warnings}"
    assert not [
        i for i in compiled.blocking_issues if i.get("code") == "reference_capacity_exceeded"
    ], "道具越界只降级不阻断，不应产生容量阻断项"

    # 已知差异（P1-6 待收敛）：resolver 按序号无限扩展 ref_prop_N，
    # 而 compiler 的槽位池固定为 ref_prop / ref_prop_2。此处锁死现状，
    # 若两侧收敛需同步修改本断言。
    manifest = resolve_shot_references(db, shot.id, project.id)
    resolver_slots = sorted(s.slot for s in manifest.slots if s.asset_type == "prop")
    assert resolver_slots == ["ref_prop", "ref_prop_2", "ref_prop_3"]


# ---------- 3. 参考图超上限 → 硬阻断 409 ----------
def test_reference_capacity_exceeded_hard_blocks_with_409(db, tmp_path):
    """4 张参考图（场景 + 角色 + 2 道具）超过工作流上限 2 → 硬阻断 409。"""
    from app.api.v1.generate import _compilation_blocked

    project = _project(db)
    scene = _scene(db, project, tmp_path)
    hero = _character(db, project, tmp_path)
    _prop(db, project, tmp_path, "古剑")
    _prop(db, project, tmp_path, "草帽")
    shot = _shot(db, scene, hero, ["古剑", "草帽"])

    wf = _workflow({
        "reference_slots": ["ref_scene", "ref_identity", "ref_prop", "ref_prop_2"],
        "max_reference_images": 2,
    })
    compiled = compile_storyboard_image(db, shot.id, workflow=wf)
    assert compiled.status == "needs_review"
    capacity = [
        i for i in compiled.blocking_issues if i.get("code") == "reference_capacity_exceeded"
    ]
    assert len(capacity) == 1
    assert capacity[0]["limit"] == 2
    assert capacity[0]["count"] == 4

    with pytest.raises(HTTPException) as exc_info:
        _compilation_blocked(compiled, 409)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "prompt_validation_failed"
    assert "reference_capacity_exceeded" in {
        issue["code"] for issue in exc_info.value.detail["blocking_issues"]
    }


# ---------- 4. 工作流未声明 ref_prop → 降级告警不阻断 ----------
def test_props_degrade_when_workflow_declares_no_prop_slot(db, tmp_path):
    """工作流只有角色/场景槽位时，道具降级为文字描述并告警，不打断整批出图。"""
    project = _project(db)
    scene = _scene(db, project, tmp_path)
    hero = _character(db, project, tmp_path)
    _prop(db, project, tmp_path, "古剑")
    _prop(db, project, tmp_path, "草帽")
    shot = _shot(db, scene, hero, ["古剑", "草帽"])

    wf = _workflow({"reference_slots": ["ref_scene", "ref_identity"]})
    compiled = compile_storyboard_image(db, shot.id, workflow=wf)
    assert compiled.status == "ready", f"道具缺槽位不应阻断，实际 {compiled.blocking_issues}"
    assert not [
        slot for slot in compiled.reference_slots if slot.get("entity_type") == "prop"
    ]
    unsupported = [w for w in compiled.warnings if w.startswith("prop_reference_unsupported")]
    assert len(unsupported) == 2, f"两个道具都应显式告警，实际 {compiled.warnings}"


# ---------- 5. API 端到端：≥2 道具 + 锚定图失效 → 409 ----------
@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    import app.core.database as db_mod
    import app.worker.parse_executor as pe_mod
    from app.main import app

    engine = make_engine(f"sqlite:///{tmp_path / 'api_multi_prop.db'}")
    Base.metadata.create_all(engine)
    new_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", new_session)
    monkeypatch.setattr(pe_mod, "SessionLocal", new_session)
    with TestClient(app) as c:
        yield c, new_session


def test_api_keyframe_with_two_props_and_stale_anchor_returns_409(api_client, tmp_path):
    """≥2 道具场景下锚定图失效：生成分镜图必须 409 并回传结构性阻断明细。"""
    client, session_factory = api_client
    db = session_factory()
    try:
        project = _project(db, name="多道具端到端")
        scene = _scene(db, project, tmp_path)
        hero = _character(db, project, tmp_path)
        # 锚定图转 superseded：assets_gate 不拦（anchor_required=False），
        # 但编译期 _approved_asset 会判为 reference_asset_not_current 硬阻断。
        _prop(db, project, tmp_path, "古剑")
        _prop(db, project, tmp_path, "草帽", validity="superseded", anchor_required=False)
        shot = _shot(db, scene, hero, ["古剑", "草帽"])

        wf = _workflow({"reference_slots": ["ref_scene", "ref_identity", "ref_prop", "ref_prop_2"]})
        db.add(wf)
        db.commit()

        r = client.post(
            f"/api/shots/{shot.id}/generate",
            json={"target": "keyframe", "workflow_id": wf.id},
        )
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "prompt_validation_failed"
        codes = {issue["code"] for issue in detail.get("blocking_issues") or []}
        assert "reference_asset_not_current" in codes, f"实际阻断项 {codes}"
    finally:
        db.close()
