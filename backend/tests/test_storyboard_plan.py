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
    assert first["plan_meta"]["visual_description"].startswith("林晓")

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
