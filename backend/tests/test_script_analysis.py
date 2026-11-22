"""剧本结构分析骨架、分析文档校验与 Shadow Run API 回归（Phase 1 切片 3）。"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Base, Character, Scene, Shot, ids
from app.services.script_analysis import (
    build_structure,
    parse_scene_heading,
    scene_key,
    validate_analysis,
)
from app.services.source_index import build_source_index

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
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
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


def _project(client: TestClient, name: str = "结构分析") -> str:
    response = client.post("/api/projects", json={"name": name, "aspect_ratio": "9:16"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _script(client: TestClient, project_id: str, text: str = CONTENT) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/script-versions", json={"text": text},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _shadow_run(client: TestClient, project_id: str, script_version_id: str) -> dict:
    response = client.get(
        f"/api/projects/{project_id}/script-versions/{script_version_id}/analysis/shadow-run",
    )
    assert response.status_code == 200, response.text
    return response.json()


def _valid_document(structure: dict) -> dict:
    scene = structure["scenes"][0]
    beat = structure["beats"][1]
    return {
        "story": {
            "synopsis": {
                "text": "林晓在教室遇见老马",
                "origin": "source",
                "source_block_ids": [scene["source_block_ids"][0]],
            },
            "theme": {
                "text": "重逢",
                "origin": "inference",
                "source_block_ids": [],
            },
        },
        "scenes": [{
            "scene_id": scene["scene_id"],
            "title": scene["title"],
            "source_block_ids": scene["source_block_ids"],
        }],
        "beats": [{
            "beat_id": beat["beat_id"],
            "scene_id": scene["scene_id"],
            "event": "林晓推门进入教室",
            "source_block_ids": beat["source_block_ids"],
        }],
        "assets": {
            "characters": [{
                "name": "林晓",
                "source_block_ids": beat["source_block_ids"],
            }],
            "locations": [],
            "props": [],
        },
        "risks": [{
            "code": "multi_character_scene",
            "level": "medium",
            "message": "同场出现两名角色",
            "suggestion": "确认角色参考槽位是否够用",
            "source_block_ids": beat["source_block_ids"],
        }],
    }


def test_structure_is_deterministic_and_bound_to_source_blocks():
    index = build_source_index(CONTENT, script_version_id="sv_1", version=2)
    first = build_structure(index)
    second = build_structure(index)

    assert first == second
    assert [block.kind for block in index.blocks] == [
        "episode", "scene", "narration", "paragraph", "dialogue",
        "dialogue", "scene", "paragraph",
    ]
    assert len(first["episodes"]) == 1
    assert len(first["scenes"]) == 2
    assert len(first["beats"]) == 5
    assert first["scenes"][0]["scene_id"].startswith("asc_")
    assert first["beats"][0]["beat_id"].startswith("abt_")
    assert first["episodes"][0]["episode_ref"].startswith("aep_")

    block_ids = {block.source_block_id for block in index.blocks}
    for scene in first["scenes"]:
        assert set(scene["source_block_ids"]) <= block_ids
        assert scene["episode_ref"] == first["episodes"][0]["episode_ref"]
    for beat in first["beats"]:
        assert set(beat["source_block_ids"]) <= block_ids
        assert beat["scene_id"] in {scene["scene_id"] for scene in first["scenes"]}

    first_scene, second_scene = first["scenes"]
    assert first_scene["location"] == "教室"
    assert first_scene["time_of_day"] == "日"
    assert first_scene["interior_exterior"] == "内"
    assert second_scene["location"] == "走廊"
    assert second_scene["time_of_day"] == "夜"
    assert second_scene["interior_exterior"] == "外"
    assert first["coverage"]["blocks_outside_scene"] == []
    assert set(first["mentioned_names"]) == {"林晓", "老马"}


def test_scene_heading_and_key_are_conservative():
    assert parse_scene_heading("第一场 教室 日 内") == {
        "location": "教室", "time_of_day": "日", "interior_exterior": "内",
    }
    assert parse_scene_heading("INT. 客厅 - 夜") == {
        "location": "客厅", "time_of_day": "夜", "interior_exterior": None,
    }
    assert parse_scene_heading("未编号的标题") == {
        "location": "未编号的标题", "time_of_day": None, "interior_exterior": None,
    }
    assert scene_key("第一场 教室 日 内") == scene_key("教室")


def test_analysis_validation_accepts_evidence_and_reports_forgery():
    index = build_source_index(CONTENT, script_version_id="sv_1", version=1)
    structure = build_structure(index)
    document = _valid_document(structure)

    accepted = validate_analysis(document, index)
    assert accepted["valid"] is True
    assert accepted["checked"] == {"scenes": 1, "beats": 1, "assets": 1, "risks": 1}

    forged = _valid_document(structure)
    forged["scenes"].append(dict(forged["scenes"][0]))
    forged["beats"][0]["scene_id"] = "asc_missing"
    forged["beats"][0]["source_block_ids"] = ["sb_forged"]
    forged["story"]["synopsis"]["source_block_ids"] = []
    forged["risks"][0]["level"] = "critical"
    del forged["assets"]

    rejected = validate_analysis(forged, index)
    assert rejected["valid"] is False
    codes = {issue["code"] for issue in rejected["issues"]}
    assert {
        "missing_field", "source_claim_without_evidence", "duplicate_id",
        "beat_scene_not_found", "source_block_not_found", "invalid_value",
    } <= codes

    assert validate_analysis("not-an-object", index)["issues"][0]["code"] == "invalid_document"


def test_shadow_run_compares_structure_with_production(client):
    project_id = _project(client)
    saved = _script(client, project_id)
    session = _session()
    try:
        scene = Scene(id=ids.scene_id(), project_id=project_id, episode=1, seq=1,
                      title="教室", description="课桌椅")
        character = Character(id=ids.character_id(), project_id=project_id,
                              name="林晓", description="学生")
        session.add_all([scene, character])
        session.flush()
        session.add(Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1,
                         shot_kind="visual", duration=5, character_id=character.id))
        session.commit()
    finally:
        session.close()

    body = _shadow_run(client, project_id, saved["script_version_id"])
    assert body["script_version_id"] == saved["script_version_id"]
    assert body["structure"]["algorithm"] == "source-structure-v1"
    assert body["comparison"]["scenes"] == {
        "matched": ["教室"], "only_in_analysis": ["走廊"], "only_in_production": [],
    }
    assert body["comparison"]["characters"] == {
        "matched": ["林晓"], "only_in_analysis": [], "only_in_production": [],
    }
    assert body["comparison"]["unlinked_references"] == ["老马"]
    assert body["comparison"]["counts"]["analysis_beats"] == 5
    assert body["comparison"]["counts"]["production_shots"] == 1
    assert body["production_baseline"]["scene_count"] == 1
    assert body["structure"]["entities"]["characters"][0]["name"] == "林晓"
    assert body["structure"]["unlinked_references"] == [{
        "name": "老马",
        "source_block_ids": body["structure"]["unlinked_references"][0]["source_block_ids"],
    }]
    assert body["structure"]["risks"][0]["code"] == "reference_unlinked"
    assert body["validation"] is None


def test_shadow_run_api_isolates_projects_and_validates_documents(client):
    project_id = _project(client)
    other_project = _project(client, "别的项目")
    saved = _script(client, project_id)
    structure = _shadow_run(client, project_id, saved["script_version_id"])["structure"]
    url = f"/api/projects/{project_id}/script-versions/{saved['script_version_id']}"

    accepted = client.post(f"{url}/analysis/validate",
                           json={"analysis": _valid_document(structure)})
    assert accepted.status_code == 200
    assert accepted.json()["valid"] is True

    empty_sections = client.post(f"{url}/analysis/validate",
                                 json={"analysis": {"story": {}, "scenes": [], "beats": [],
                                                    "assets": {}, "risks": []}})
    assert empty_sections.status_code == 200
    assert empty_sections.json()["valid"] is True

    forged = client.post(f"{url}/analysis/validate",
                         json={"analysis": {"scenes": [{"scene_id": "asc_1"}]}})
    assert forged.status_code == 200
    assert forged.json()["valid"] is False

    assert client.get(
        f"/api/projects/{other_project}/script-versions/{saved['script_version_id']}"
        "/analysis/shadow-run",
    ).status_code == 404
    assert client.get(
        f"/api/projects/{project_id}/script-versions/sv_missing/analysis/shadow-run",
    ).status_code == 404
    assert client.get(
        "/api/projects/prj_missing/script-versions/sv_missing/analysis/shadow-run",
    ).status_code == 404
