"""原文行级索引、证据校验与 additive API 回归。"""
import hashlib

import pytest
from fastapi.testclient import TestClient

from app.core.database import make_engine
from app.main import app
from app.models import Base
from app.services.source_index import build_source_index, validate_source_references

CONTENT = "第1集\r\n\r\n  第一场 教室 日 内  \r\n林晓：你好！\r\n旁白：雨停了。\r\n最后动作"


def test_source_index_is_stable_and_preserves_original_ranges():
    content = CONTENT
    first = build_source_index(content, script_version_id="sv_1", version=3)
    second = build_source_index(content, script_version_id="sv_1", version=3)

    assert first == second
    assert [block.kind for block in first.blocks] == [
        "episode", "scene", "dialogue", "narration", "paragraph",
    ]
    assert [block.seq for block in first.blocks] == [1, 2, 3, 4, 5]
    assert [block.line for block in first.blocks] == [1, 3, 4, 5, 6]
    for block in first.blocks:
        assert content[block.start:block.end] == block.text
        assert "\r" not in block.text and "\n" not in block.text
        assert block.text_sha256 == hashlib.sha256(block.text.encode("utf-8")).hexdigest()
    assert first.blocks[2].text == "林晓：你好！"
    assert first.blocks[1].text == "  第一场 教室 日 内  "


def test_source_index_ids_include_version_and_ignore_blank_lines():
    original = build_source_index(CONTENT, script_version_id="sv_1", version=1)
    new_version = build_source_index(CONTENT, script_version_id="sv_1", version=2)
    other_script = build_source_index(CONTENT, script_version_id="sv_2", version=1)

    assert [block.source_block_id for block in original.blocks] != [
        block.source_block_id for block in new_version.blocks
    ]
    assert [block.source_block_id for block in original.blocks] != [
        block.source_block_id for block in other_script.blocks
    ]
    assert build_source_index("", script_version_id="sv_1", version=1).blocks == ()
    assert build_source_index("\r\n \n\t\r", script_version_id="sv_1", version=1).blocks == ()


def test_source_reference_validation_accepts_evidence_and_reports_forgery():
    index = build_source_index(CONTENT, script_version_id="sv_1", version=1)
    block = index.blocks[2]

    valid = validate_source_references(index, [block.to_dict()])
    assert valid == {"valid": True, "checked": 1, "issues": []}

    invalid = validate_source_references(index, [
        {
            "source_block_id": block.source_block_id,
            "script_version_id": "sv_other",
            "script_version": 2,
            "start": block.start + 1,
            "end": block.end + 1,
            "text": "伪造文本",
        },
        {"source_block_id": "sb_forged"},
    ])
    assert invalid["valid"] is False
    assert invalid["checked"] == 2
    assert {issue["code"] for issue in invalid["issues"]} == {
        "script_version_id_mismatch", "script_version_mismatch", "start_mismatch",
        "end_mismatch", "text_mismatch", "source_block_not_found",
    }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import sessionmaker

    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    return TestClient(app)


def _project(client: TestClient, name: str = "原文索引") -> str:
    response = client.post("/api/projects", json={"name": name, "aspect_ratio": "9:16"})
    assert response.status_code == 201
    return response.json()["id"]


def _script(client: TestClient, project_id: str, text: str = CONTENT) -> dict:
    response = client.post(f"/api/projects/{project_id}/script-versions", json={"text": text})
    assert response.status_code == 201
    return response.json()


def test_source_blocks_api_queries_and_validates_current_snapshot(client: TestClient):
    project_id = _project(client)
    saved = _script(client, project_id)
    url = f"/api/projects/{project_id}/script-versions/{saved['script_version_id']}/source-blocks"

    response = client.get(url)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["algorithm"] == "source-line-v1"
    assert body["script_version"] == saved["version"]
    assert body["block_count"] == 5

    block = body["blocks"][0]
    valid = client.post(f"{url}/validate", json={"references": [block]})
    assert valid.status_code == 200
    assert valid.json()["valid"] is True

    forged = client.post(f"{url}/validate", json={"references": [{
        "source_block_id": block["source_block_id"],
        "script_version": block["script_version"] + 1,
        "start": block["start"] + 1,
        "text": "伪造证据",
    }]})
    assert forged.status_code == 200
    assert forged.json()["valid"] is False
    assert {issue["code"] for issue in forged.json()["issues"]} == {
        "script_version_mismatch", "start_mismatch", "text_mismatch",
    }

    saved_v2 = _script(client, project_id, CONTENT + "\n新增一行")
    updated = client.get(url).json()
    assert updated["script_version"] == saved_v2["version"] == 2
    assert updated["block_count"] == 6
    assert updated["blocks"][-1]["text"] == "新增一行"
    assert updated["blocks"][0]["source_block_id"] != block["source_block_id"]


def test_source_blocks_api_rejects_missing_and_cross_project_versions(client: TestClient):
    project_id = _project(client)
    other_project_id = _project(client, "其他项目")
    saved = _script(client, project_id)
    other_url = (
        f"/api/projects/{other_project_id}/script-versions/"
        f"{saved['script_version_id']}/source-blocks"
    )

    assert client.get(other_url).status_code == 404
    assert client.post(f"{other_url}/validate", json={"references": []}).status_code == 404
    missing_url = f"/api/projects/{project_id}/script-versions/sv_missing/source-blocks"
    assert client.get(missing_url).status_code == 404
    unknown_project_url = f"/api/projects/prj_missing/script-versions/{saved['script_version_id']}/source-blocks"
    assert client.get(unknown_project_url).status_code == 404
