"""分镜规划校验、Shot 兼容 Adapter 预览与既有镜头对比回归（Phase 2 切片）。"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Base, Character, Prop, Scene, Shot, ids

CONTENT = (
    "第1集\n"
    "第一场 教室 日 内\n"
    "旁白：雨停了。\n"
    "@林晓 推门走进教室，看见 @老马 站在讲台边。\n"
    "林晓：你来了。\n"
    "老马：嗯。\n"
    "第二场 走廊 夜 外\n"
    "@林晓 独自离开。\n"
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'plan.db'}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    return TestClient(app)


def _session():
    from app.core.database import SessionLocal

    return SessionLocal()


def _setup(client: TestClient) -> dict:
    project_id = client.post(
        "/api/projects", json={"name": "分镜规划", "aspect_ratio": "9:16"},
    ).json()["id"]
    saved = client.post(
        f"/api/projects/{project_id}/script-versions", json={"text": CONTENT},
    ).json()
    session = _session()
    try:
        classroom = Scene(id=ids.scene_id(), project_id=project_id, episode=1, seq=1,
                          title="教室", description="课桌椅")
        corridor = Scene(id=ids.scene_id(), project_id=project_id, episode=1, seq=2,
                         title="走廊", description="空荡走廊")
        character = Character(id=ids.character_id(), project_id=project_id,
                              name="林晓", description="学生")
        prop = Prop(id=ids.prop_id(), project_id=project_id, name="钥匙",
                    description="铜钥匙")
        session.add_all([classroom, corridor, character, prop])
        session.commit()
        scene_ids = {"classroom": classroom.id, "corridor": corridor.id}
        character_id, prop_id = character.id, prop.id
    finally:
        session.close()
    structure = client.get(
        f"/api/projects/{project_id}/script-versions/{saved['script_version_id']}"
        "/analysis/shadow-run",
    ).json()["structure"]
    return {
        "project_id": project_id,
        "script_version_id": saved["script_version_id"],
        "scenes": scene_ids,
        "character_id": character_id,
        "prop_id": prop_id,
        "structure": structure,
    }


def _valid_plan(context: dict) -> dict:
    scenes = context["structure"]["scenes"]
    beats = context["structure"]["beats"]
    first_scene_beats = [beat for beat in beats if beat["scene_id"] == scenes[0]["scene_id"]]
    second_scene_beats = [beat for beat in beats if beat["scene_id"] == scenes[1]["scene_id"]]
    return {
        "plan_version": 1,
        "shots": [
            {
                "shot_id": "sht_plan_1",
                "scene_id": context["scenes"]["classroom"],
                "beat_ids": [first_scene_beats[1]["beat_id"]],
                "sequence": 1,
                "duration": 5,
                "shot_kind": "visual",
                "visual_description": "林晓推门走进教室，老马站在讲台边",
                "source_block_ids": first_scene_beats[1]["source_block_ids"],
                "character_asset_ids": [context["character_id"]],
                "prop_asset_ids": [context["prop_id"]],
            },
            {
                "shot_id": "sht_plan_2",
                "scene_id": context["scenes"]["corridor"],
                "beat_ids": [second_scene_beats[0]["beat_id"]],
                "sequence": 1,
                "duration": 3,
                "shot_kind": "narration",
                "visual_description": "林晓独自走过走廊",
                "source_block_ids": second_scene_beats[0]["source_block_ids"],
                "character_asset_ids": [context["character_id"]],
            },
        ],
    }


def _post_plan(client: TestClient, context: dict, plan: dict) -> dict:
    response = client.post(
        f"/api/projects/{context['project_id']}/script-versions/"
        f"{context['script_version_id']}/storyboard-plan/validate",
        json={"plan": plan},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_valid_plan_maps_to_shot_preview_without_writing(client):
    context = _setup(client)
    body = _post_plan(client, context, _valid_plan(context))

    assert body["valid"] is True
    assert body["adapter"]["writable"] is False
    assert body["adapter"]["mode"] == "dry_run"
    first = body["adapted_shots"][0]
    assert first["id"] == "sht_plan_1"
    assert first["scene_id"] == context["scenes"]["classroom"]
    assert first["episode"] == 1
    assert first["seq"] == 1
    assert first["props"] == ["钥匙"]
    assert first["image_prompt"] is None
    assert first["video_prompt"] is None
    # Phase 1：plan_meta 已由镜头级契约 shot_plan 取代（导演决策落库）
    assert first["shot_plan"]["visual_description"].startswith("林晓")
    assert first["shot_plan"]["source"] == "storyboard_plan"
    assert first["shot_plan"]["on_screen_characters"][0]["character_id"] == context["character_id"]
    assert first["shot_plan"]["on_screen_characters"][0]["role"] == "primary"

    assert body["coverage"]["scenes"] == {
        "total": 2, "covered": 2, "uncovered_scene_ids": [],
    }
    assert body["coverage"]["beats"]["total"] == 5
    assert body["coverage"]["beats"]["covered"] == 2
    warning_codes = {issue["code"] for issue in body["issues"]}
    assert {"dialogue_not_covered", "narration_not_covered"} <= warning_codes
    assert body["comparison"]["existing_shot_count"] == 0

    session = _session()
    try:
        assert session.query(Shot).count() == 0
    finally:
        session.close()


def test_invalid_plan_reports_structural_and_reference_errors(client):
    context = _setup(client)
    plan = _valid_plan(context)
    plan["shots"][0]["scene_id"] = "scn_missing"
    plan["shots"][0]["shot_kind"] = "unknown"
    plan["shots"][0]["duration"] = 0
    plan["shots"][0]["source_block_ids"] = ["sb_forged"]
    plan["shots"][0]["character_asset_ids"] = [context["scenes"]["classroom"]]
    plan["shots"][1]["shot_id"] = "sht_plan_1"
    plan["shots"][1]["sequence"] = 0

    body = _post_plan(client, context, plan)
    assert body["valid"] is False
    codes = {issue["code"] for issue in body["issues"]}
    assert {
        "scene_not_found", "invalid_value", "source_block_not_found",
        "asset_not_found", "duplicate_id",
    } <= codes
    assert any(issue["level"] == "error" for issue in body["issues"])

    assert _post_plan(client, context, {"plan_version": 1, "shots": []})["valid"] is False
    empty = _post_plan(client, context, {"plan_version": 1, "shots": []})
    assert "plan_empty" in {issue["code"] for issue in empty["issues"]}
    assert _post_plan(client, context, {})["valid"] is False
    non_object = client.post(
        f"/api/projects/{context['project_id']}/script-versions/"
        f"{context['script_version_id']}/storyboard-plan/validate",
        json={"plan": ["not-an-object"]},
    )
    assert non_object.status_code == 422


def test_confirmed_shot_reuse_is_reported_and_not_overwritten(client):
    context = _setup(client)
    existing_id = "sht_plan_1"
    session = _session()
    try:
        session.add(Shot(
            id=existing_id, scene_id=context["scenes"]["classroom"], episode=1,
            seq=1, shot_kind="visual", duration=3, keyframe_confirmed=True,
            image_prompt="已确认的分镜图提示词",
        ))
        session.commit()
    finally:
        session.close()

    body = _post_plan(client, context, _valid_plan(context))
    assert body["comparison"]["reused_shot_ids"] == [existing_id]
    assert body["comparison"]["existing_locked_shot_ids"] == [existing_id]
    assert body["comparison"]["locked_conflicts"] == [
        {"shot_id": existing_id, "fields": ["duration"]},
    ]
    assert {"locked_shot_modified", "sequence_conflict"} <= {
        issue["code"] for issue in body["issues"]
    }
    assert body["valid"] is True

    session = _session()
    try:
        stored = session.get(Shot, existing_id)
        assert stored.duration == 3
        assert stored.image_prompt == "已确认的分镜图提示词"
    finally:
        session.close()


def test_validate_reports_field_changes_before_apply(client):
    """写入前差异：只列规划显式字段的变化，锁定镜头关键字段标记 will_skip。"""
    context = _setup(client)
    session = _session()
    try:
        session.add_all([
            Shot(id="sht_plan_1", scene_id=context["scenes"]["classroom"], episode=1,
                 seq=1, shot_kind="visual", duration=2, props=[], beats=[]),
            Shot(id="sht_plan_2", scene_id=context["scenes"]["corridor"], episode=1,
                 seq=1, shot_kind="narration", duration=3, keyframe_confirmed=True),
        ])
        session.commit()
    finally:
        session.close()

    plan = _valid_plan(context)
    plan["shots"][0]["dialogue"] = "你来了。"
    plan["shots"][1]["duration"] = 4

    body = _post_plan(client, context, plan)
    changes = {item["shot_id"]: item for item in body["comparison"]["field_changes"]}
    assert set(changes) == {"sht_plan_1", "sht_plan_2"}

    first = {item["field"]: item for item in changes["sht_plan_1"]["fields"]}
    assert first["duration"] == {"field": "duration", "from": 2, "to": 5}
    assert first["props"] == {"field": "props", "from": [], "to": ["钥匙"]}
    assert first["dialogue"] == {"field": "dialogue", "from": None, "to": "你来了。"}
    assert changes["sht_plan_1"]["will_skip"] is False

    second = {item["field"]: item for item in changes["sht_plan_2"]["fields"]}
    assert second["duration"] == {"field": "duration", "from": 3, "to": 4}
    assert changes["sht_plan_2"]["will_skip"] is True

    # 校验只读：既有镜头字段未被改动
    session = _session()
    try:
        stored = session.get(Shot, "sht_plan_1")
        assert stored.duration == 2
        assert stored.dialogue is None
        assert session.get(Shot, "sht_plan_2").duration == 3
    finally:
        session.close()


def test_validate_omits_unchanged_fields_from_diff(client):
    """规划未显式提供的字段不进入差异，值相同的字段也不进入。"""
    context = _setup(client)
    session = _session()
    try:
        session.add(Shot(
            id="sht_plan_1", scene_id=context["scenes"]["classroom"], episode=1,
            seq=1, shot_kind="visual", duration=5,
        ))
        session.commit()
    finally:
        session.close()

    plan = _valid_plan(context)
    plan["shots"] = [plan["shots"][0]]
    body = _post_plan(client, context, plan)

    changes = {item["shot_id"]: item for item in body["comparison"]["field_changes"]}
    fields = {item["field"] for item in changes["sht_plan_1"]["fields"]}
    # 序号/时长/镜头类型与既有值一致；规划未提供 dialogue/机位等，均不进入差异
    assert "seq" not in fields
    assert "duration" not in fields
    assert "shot_kind" not in fields
    assert "dialogue" not in fields
    # P1 聚合改造：beats 降级为派生只读视图，规划路径写入 aggregation_meta.source_beat_ids
    # Phase 1：规划提供出场清单时生成镜头级契约 shot_plan 并进入差异
    assert fields == {"character_id", "props", "aggregation_meta", "shot_plan"}


def _post_apply(client: TestClient, context: dict, plan: dict):
    return client.post(
        f"/api/projects/{context['project_id']}/script-versions/"
        f"{context['script_version_id']}/storyboard-plan/apply",
        json={"plan": plan},
    )


def test_apply_creates_shots_from_valid_plan(client):
    context = _setup(client)
    plan = _valid_plan(context)
    plan["shots"][0]["image_prompt"] = "教室全景，林晓推门"

    response = _post_apply(client, context, plan)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["created_shot_ids"] == ["sht_plan_1", "sht_plan_2"]
    assert body["updated_shot_ids"] == []
    assert body["orphan_shot_ids"] == []
    assert body["skipped_locked"] == []

    session = _session()
    try:
        first = session.get(Shot, "sht_plan_1")
        assert first.scene_id == context["scenes"]["classroom"]
        assert first.episode == 1
        assert first.seq == 1
        assert first.status == "pending"
        assert first.props == ["钥匙"]
        assert first.beats == plan["shots"][0]["beat_ids"]
        assert first.image_prompt == "教室全景，林晓推门"
        assert first.video_prompt is None
        assert session.query(Shot).count() == 2
    finally:
        session.close()


def test_apply_rejects_invalid_plan_without_writing(client):
    context = _setup(client)
    plan = _valid_plan(context)
    plan["shots"][0]["scene_id"] = "scn_missing"

    response = _post_apply(client, context, plan)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "storyboard_plan_invalid"
    assert any(issue["level"] == "error" for issue in detail["issues"])

    session = _session()
    try:
        assert session.query(Shot).count() == 0
    finally:
        session.close()


def test_apply_requires_plan_version(client):
    context = _setup(client)
    plan = _valid_plan(context)
    plan.pop("plan_version")

    response = _post_apply(client, context, plan)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "storyboard_plan_invalid"

    session = _session()
    try:
        assert session.query(Shot).count() == 0
    finally:
        session.close()


def test_apply_skips_locked_shot_fields(client):
    context = _setup(client)
    session = _session()
    try:
        session.add(Shot(
            id="sht_plan_1", scene_id=context["scenes"]["classroom"], episode=1,
            seq=1, shot_kind="visual", duration=3, keyframe_confirmed=True,
            image_prompt="已确认的分镜图提示词",
        ))
        session.commit()
    finally:
        session.close()

    response = _post_apply(client, context, _valid_plan(context))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["skipped_locked"] == [
        {"shot_id": "sht_plan_1", "fields": ["duration"]},
    ]
    assert "sht_plan_1" not in body["updated_shot_ids"]
    assert body["created_shot_ids"] == ["sht_plan_2"]

    session = _session()
    try:
        stored = session.get(Shot, "sht_plan_1")
        assert stored.duration == 3
        assert stored.image_prompt == "已确认的分镜图提示词"
    finally:
        session.close()


def test_apply_updates_existing_shot_and_keeps_orphans(client):
    context = _setup(client)
    session = _session()
    try:
        session.add_all([
            Shot(id="sht_plan_1", scene_id=context["scenes"]["classroom"], episode=1,
                 seq=1, shot_kind="visual", duration=2, props=[], beats=[]),
            Shot(id="sht_old", scene_id=context["scenes"]["classroom"], episode=1,
                 seq=9, shot_kind="narration", duration=2),
        ])
        session.commit()
    finally:
        session.close()

    response = _post_apply(client, context, _valid_plan(context))
    assert response.status_code == 200, response.text
    body = response.json()
    assert "sht_plan_1" in body["updated_shot_ids"]
    assert body["orphan_shot_ids"] == ["sht_old"]

    session = _session()
    try:
        stored = session.get(Shot, "sht_plan_1")
        assert stored.duration == 5
        assert stored.props == ["钥匙"]
        assert session.get(Shot, "sht_old") is not None
    finally:
        session.close()


def test_apply_handles_sequence_swap(client):
    context = _setup(client)
    beat = next(
        item for item in context["structure"]["beats"]
        if item["scene_id"] == context["structure"]["scenes"][0]["scene_id"]
    )
    plan = {
        "plan_version": 1,
        "shots": [
            {
                "shot_id": "sht_a", "scene_id": context["scenes"]["classroom"],
                "beat_ids": [beat["beat_id"]], "sequence": 2, "duration": 3,
                "shot_kind": "visual", "visual_description": "教室空镜",
                "source_block_ids": beat["source_block_ids"],
            },
            {
                "shot_id": "sht_b", "scene_id": context["scenes"]["classroom"],
                "beat_ids": [beat["beat_id"]], "sequence": 1, "duration": 3,
                "shot_kind": "visual", "visual_description": "林晓推门",
                "source_block_ids": beat["source_block_ids"],
            },
        ],
    }
    session = _session()
    try:
        session.add_all([
            Shot(id="sht_a", scene_id=context["scenes"]["classroom"], episode=1,
                 seq=1, shot_kind="visual", duration=3),
            Shot(id="sht_b", scene_id=context["scenes"]["classroom"], episode=1,
                 seq=2, shot_kind="visual", duration=3),
        ])
        session.commit()
    finally:
        session.close()

    response = _post_apply(client, context, plan)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["updated_shot_ids"]) == {"sht_a", "sht_b"}

    session = _session()
    try:
        assert session.get(Shot, "sht_a").seq == 2
        assert session.get(Shot, "sht_b").seq == 1
    finally:
        session.close()


def test_apply_rejects_sequence_conflict_with_orphan(client):
    context = _setup(client)
    session = _session()
    try:
        session.add(Shot(
            id="sht_old", scene_id=context["scenes"]["classroom"], episode=1,
            seq=1, shot_kind="visual", duration=2,
        ))
        session.commit()
    finally:
        session.close()

    response = _post_apply(client, context, _valid_plan(context))
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "storyboard_plan_conflict"
    assert detail["conflicts"][0]["conflicts_with"] == "sht_old"

    session = _session()
    try:
        assert session.query(Shot).count() == 1
    finally:
        session.close()


def test_plan_api_enforces_project_isolation(client):
    context = _setup(client)
    other_project = client.post(
        "/api/projects", json={"name": "别的项目", "aspect_ratio": "9:16"},
    ).json()["id"]
    url = (f"/api/projects/{context['project_id']}/script-versions/"
           f"{context['script_version_id']}/storyboard-plan/validate")

    assert client.post(
        f"/api/projects/{other_project}/script-versions/"
        f"{context['script_version_id']}/storyboard-plan/validate",
        json={"plan": _valid_plan(context)},
    ).status_code == 404
    assert client.post(
        f"/api/projects/{context['project_id']}/script-versions/sv_missing"
        "/storyboard-plan/validate",
        json={"plan": _valid_plan(context)},
    ).status_code == 404
    assert client.post(url, json={"plan": _valid_plan(context)}).status_code == 200

    assert _post_apply(client, {**context, "project_id": other_project},
                       _valid_plan(context)).status_code == 404


def test_apply_writes_shot_plan_contract(client):
    """验收用例 D：apply 写入多角色镜头的 shot_plan 契约，重复 apply 幂等。"""
    context = _setup(client)
    session = _session()
    try:
        laoma = Character(id=ids.character_id(), project_id=context["project_id"],
                          name="老马", description="老师")
        wang = Character(id=ids.character_id(), project_id=context["project_id"],
                         name="王老师", description="班主任")
        session.add_all([laoma, wang])
        session.commit()
        laoma_id, wang_id = laoma.id, wang.id
    finally:
        session.close()
    beat = next(
        item for item in context["structure"]["beats"]
        if item["scene_id"] == context["structure"]["scenes"][0]["scene_id"]
    )
    plan = {
        "plan_version": 1,
        "shots": [
            {
                "shot_id": "sht_cast_1",
                "scene_id": context["scenes"]["classroom"],
                "beat_ids": [beat["beat_id"]],
                "sequence": 1, "duration": 5, "shot_kind": "dialogue",
                "visual_description": "林晓与老马、王老师在教室对话",
                "source_block_ids": beat["source_block_ids"],
                "character_asset_ids": [context["character_id"], laoma_id, wang_id],
                "on_screen_character_ids": [context["character_id"], laoma_id, wang_id],
                "blocking": {
                    context["character_id"]: {"anchor": "教室前门", "side": "左"},
                    laoma_id: {"anchor": "讲台", "side": "中"},
                    wang_id: {"anchor": "黑板", "side": "右"},
                },
            },
        ],
    }
    response = _post_apply(client, context, plan)
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    session = _session()
    try:
        stored = session.get(Shot, "sht_cast_1")
        # character_id 仍兼容写首个；完整出场清单进 shot_plan
        assert stored.character_id == context["character_id"]
        contract = stored.shot_plan
        assert contract["version"] == 1
        assert contract["source"] == "storyboard_plan"
        assert [c["character_id"] for c in contract["on_screen_characters"]] == [
            context["character_id"], laoma_id, wang_id,
        ]
        assert contract["on_screen_characters"][0]["role"] == "primary"
        assert contract["on_screen_characters"][0]["position"] == {
            "anchor": "教室前门", "side": "左",
        }
    finally:
        session.close()
    # 重复 apply 幂等：字段一致 → unchanged
    response = _post_apply(client, context, plan)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert "sht_cast_1" in body["unchanged_shot_ids"]


def test_apply_preserves_locked_shot_plan_cast(client):
    """验收用例 D：锁定镜头的 shot_plan.on_screen_characters 不被覆盖。"""
    context = _setup(client)
    session = _session()
    try:
        laoma = Character(id=ids.character_id(), project_id=context["project_id"],
                          name="老马", description="老师")
        session.add(laoma)
        session.commit()
        laoma_id = laoma.id
        session.add(Shot(
            id="sht_lock_1", scene_id=context["scenes"]["classroom"], episode=1,
            seq=1, shot_kind="visual", duration=5, keyframe_confirmed=True,
            shot_plan={
                "version": 1, "source": "storyboard_plan", "confirmed": True,
                "on_screen_characters": [{"character_id": context["character_id"],
                                          "role": "primary"}],
            },
        ))
        session.commit()
    finally:
        session.close()
    beat = next(
        item for item in context["structure"]["beats"]
        if item["scene_id"] == context["structure"]["scenes"][0]["scene_id"]
    )
    plan = {
        "plan_version": 1,
        "shots": [
            {
                "shot_id": "sht_lock_1",
                "scene_id": context["scenes"]["classroom"],
                "beat_ids": [beat["beat_id"]],
                "sequence": 1, "duration": 5, "shot_kind": "visual",
                "visual_description": "林晓与老马在教室",
                "source_block_ids": beat["source_block_ids"],
                "character_asset_ids": [context["character_id"], laoma_id],
                "on_screen_character_ids": [context["character_id"], laoma_id],
            },
        ],
    }
    response = _post_apply(client, context, plan)
    assert response.status_code == 200, response.text
    session = _session()
    try:
        stored = session.get(Shot, "sht_lock_1")
        # 锁定镜头：on_screen_characters 保持原值（1 人），不被规划覆盖
        assert [c["character_id"] for c in stored.shot_plan["on_screen_characters"]] == [
            context["character_id"],
        ]
    finally:
        session.close()
