"""依赖变更影响预演：版本比较、下游传播、只读策略与项目隔离。"""
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.database import make_engine
from app.main import app
from app.models import (
    Asset,
    AssetRef,
    Base,
    DependencyRef,
    Job,
    JobInputRef,
    Scene,
    Shot,
)


@pytest.fixture()
def client_and_session(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import sessionmaker

    import app.core.database as db_mod

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    return TestClient(app), factory


def _project(client: TestClient, name: str) -> str:
    response = client.post("/api/projects", json={"name": name, "aspect_ratio": "9:16"})
    assert response.status_code == 201
    return response.json()["id"]


def _seed(db, project_id: str, other_project_id: str) -> None:
    now = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None)
    db.add_all([
        Scene(id="scn_a", project_id=project_id, episode=1, seq=1, title="教室"),
        Scene(id="scn_other", project_id=other_project_id, episode=1, seq=1, title="外部场景"),
    ])
    db.flush()
    db.add_all([
        Shot(id="sht_a", scene_id="scn_a", episode=1, seq=1, shot_kind="visual", duration=5),
        Asset(id="ast_v1", project_id=project_id, asset_type="character", name="小马",
              version=1, validity="superseded", file_path="/tmp/v1.png"),
        Asset(id="ast_v2", project_id=project_id, asset_type="character", name="小马",
              version=2, validity="current", file_path="/tmp/v2.png"),
        Asset(id="ast_sb", project_id=project_id, asset_type="shot_image", name="分镜",
              version=1, validity="current", file_path="/tmp/sb.png"),
        Asset(id="ast_isolated", project_id=project_id, asset_type="prop", name="钥匙",
              version=1, validity="current", file_path="/tmp/key.png"),
        Asset(id="ast_other", project_id=other_project_id, asset_type="character",
              name="外部角色", version=1, validity="current", file_path="/tmp/other.png"),
        Job(id="job_a", project_id=project_id, job_type="storyboard_image",
            status="succeeded", created_at=now),
    ])
    db.add_all([
        AssetRef(id="asr_a", shot_id="sht_a", asset_id="ast_v1", role="identity",
                 source_version=1, created_at=now),
        JobInputRef(id="jir_a", job_id="job_a", slot_param="ref_identity",
                    asset_id="ast_v1", asset_version=1, required=True, enabled=True,
                    created_at=now),
        DependencyRef(id="dep_a", source_type="job", source_id="job_a",
                      source_version=1, target_type="asset", target_id="ast_sb",
                      dependency_role="generated"),
    ])
    db.commit()


def _impact(client: TestClient, project_id: str, node_type: str, node_id: str) -> dict:
    response = client.get(
        f"/api/projects/{project_id}/dependency-graph/{node_type}/{node_id}/impact-preview",
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_asset_lineage_change_marks_redo_and_rebind(client_and_session):
    client, factory = client_and_session
    project_id = _project(client, "影响预演")
    other_project_id = _project(client, "别的项目")
    session = factory()
    try:
        _seed(session, project_id, other_project_id)
    finally:
        session.close()

    body = _impact(client, project_id, "asset", "ast_v2")
    assert body["algorithm"] == "dependency-impact-v1"
    assert body["root"]["lineage_versions"] == [1, 2]
    assert body["policy"] == {
        "auto_regenerate": False, "mark_stale": False, "delete_existing": False,
        "writes": False,
        "note": "仅预演影响范围；重做、标记失效与重生成必须由用户确认后单独触发",
    }

    by_key = {item["key"]: item for item in body["affected"]}
    assert set(by_key) == {"shot:sht_a", "job:job_a", "asset:ast_sb"}

    shot = by_key["shot:sht_a"]
    assert shot["change_status"] == "rebind_required"
    assert shot["has_output"] is False
    assert shot["requires_regeneration"] is False
    assert shot["depth"] == 1
    assert shot["path"] == ["asset:ast_v1", "shot:sht_a"]
    assert shot["stale_inputs"][0]["reason"] == "source_version_changed"
    assert shot["stale_inputs"][0]["current_version"] == 2

    job = by_key["job:job_a"]
    assert job["change_status"] == "redo_required"
    assert job["has_output"] is True
    assert job["requires_regeneration"] is True

    storyboard = by_key["asset:ast_sb"]
    assert storyboard["depth"] == 2
    assert storyboard["change_status"] == "redo_required"
    assert storyboard["stale_inputs"][0]["reason"] == "upstream_needs_redo"

    assert body["summary"] == {
        "affected_count": 3, "redo_required": 2, "rebind_required": 1,
        "not_generated": 0, "up_to_date": 0, "unknown_version": 0,
    }


def test_isolated_node_has_empty_impact_and_foreign_node_is_isolated(client_and_session):
    client, factory = client_and_session
    project_id = _project(client, "影响预演")
    other_project_id = _project(client, "别的项目")
    session = factory()
    try:
        _seed(session, project_id, other_project_id)
    finally:
        session.close()

    isolated = _impact(client, project_id, "asset", "ast_isolated")
    assert isolated["affected"] == []
    assert isolated["summary"]["affected_count"] == 0

    missing = client.get(
        f"/api/projects/{project_id}/dependency-graph/asset/ast_missing/impact-preview",
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "dependency_node_not_found"

    foreign = client.get(
        f"/api/projects/{project_id}/dependency-graph/asset/ast_other/impact-preview",
    )
    assert foreign.status_code == 404
    assert foreign.json()["detail"]["code"] == "dependency_node_not_found"

    unknown_project = client.get(
        "/api/projects/prj_missing/dependency-graph/asset/ast_v1/impact-preview",
    )
    assert unknown_project.status_code == 404


def test_project_impact_preview_aggregates_stale_roots_without_writes(client_and_session):
    client, factory = client_and_session
    project_id = _project(client, "项目总览")
    other_project_id = _project(client, "别的项目")
    session = factory()
    try:
        _seed(session, project_id, other_project_id)
    finally:
        session.close()

    response = client.get(f"/api/projects/{project_id}/dependency-graph/impact-preview")
    assert response.status_code == 200, response.text
    body = response.json()

    # 过期上游只有 ast_v1（边缘记录 source_version=1，血缘当前版本=2）
    assert [root["key"] for root in body["changed_roots"]] == ["asset:ast_v1"]
    assert body["policy"]["writes"] is False
    by_key = {item["key"]: item for item in body["affected"]}
    assert set(by_key) == {"shot:sht_a", "job:job_a", "asset:ast_sb"}
    assert by_key["job:job_a"]["change_status"] == "redo_required"
    assert by_key["shot:sht_a"]["change_status"] == "rebind_required"
    assert body["summary"] == {
        "affected_count": 3, "redo_required": 2, "rebind_required": 1,
        "not_generated": 0, "up_to_date": 0, "unknown_version": 0,
    }

    # 只读：调用前后依赖记录与资产版本均不变
    with factory() as check:
        assert check.query(AssetRef).filter(AssetRef.id == "asr_a").one().source_version == 1
        assert check.query(Asset).filter(Asset.id == "ast_v2").one().validity == "current"

    assert client.get(
        "/api/projects/prj_missing/dependency-graph/impact-preview",
    ).status_code == 404
