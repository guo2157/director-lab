"""统一只读依赖图：聚合、遍历、诊断与项目隔离。"""
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.database import make_engine
from app.main import app
from app.models import (
    Asset,
    AssetPlan,
    AssetPlanDependency,
    AssetRef,
    Base,
    DependencyRef,
    Job,
    JobInputRef,
    Scene,
    Shot,
)
from app.services.dependency_graph import build_dependency_graph, traverse_dependency_graph


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


def _seed_graph(db, project_id: str, other_project_id: str) -> None:
    now = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None)
    db.add_all([
        Scene(id="scn_a", project_id=project_id, episode=1, seq=1, title="教室"),
        Scene(id="scn_other", project_id=other_project_id, episode=1, seq=1, title="外部场景"),
    ])
    db.flush()
    db.add_all([
        Shot(id="sht_a", scene_id="scn_a", episode=1, seq=1, shot_kind="dialogue", duration=5),
        Shot(id="sht_other", scene_id="scn_other", episode=1, seq=1, shot_kind="dialogue", duration=5),
        Asset(id="ast_a", project_id=project_id, asset_type="character", name="角色", version=2),
        Asset(id="ast_other", project_id=other_project_id, asset_type="character", name="外部角色", version=1),
        Job(id="job_a", project_id=project_id, job_type="generate", created_at=now),
        Job(id="job_other", project_id=other_project_id, job_type="generate", created_at=now),
        AssetPlan(
            id="apl_ref", project_id=project_id, plan_type="character", asset_key="chr_ref",
            media_path="ref.png",
        ),
        AssetPlan(
            id="apl_target", project_id=project_id, plan_type="keyframe", asset_key="key_target",
            media_path="target.png",
        ),
        AssetPlan(
            id="apl_other", project_id=other_project_id, plan_type="character", asset_key="chr_other",
            media_path="other.png",
        ),
    ])
    db.add_all([
        AssetRef(
            id="asr_a", shot_id="sht_a", asset_id="ast_a", role="reference",
            source_version=1, created_at=now,
        ),
        AssetRef(
            id="asr_other", shot_id="sht_other", asset_id="ast_other", role="reference",
            source_version=1, created_at=now,
        ),
        AssetRef(
            id="asr_cross", shot_id="sht_a", asset_id="ast_other", role="reference",
            source_version=1, created_at=now,
        ),
        DependencyRef(
            id="dep_missing", source_type="asset", source_id="ast_missing", source_version=1,
            target_type="shot", target_id="sht_a", dependency_role="reference",
        ),
        DependencyRef(
            id="dep_shot_job", source_type="shot", source_id="sht_a", source_version=1,
            target_type="job", target_id="job_a", dependency_role="continuation",
        ),
        DependencyRef(
            id="dep_job_asset", source_type="job", source_id="job_a", source_version=1,
            target_type="asset", target_id="ast_a", dependency_role="cycle", validity="stale",
        ),
        DependencyRef(
            id="dep_other", source_type="asset", source_id="ast_other", source_version=1,
            target_type="shot", target_id="sht_other", dependency_role="reference",
        ),
        DependencyRef(
            id="dep_cross", source_type="asset", source_id="ast_other", source_version=1,
            target_type="shot", target_id="sht_a", dependency_role="reference",
        ),
        AssetPlanDependency(
            id="apd_a", plan_id="apl_target", slot="ref_identity", ref_plan_id="apl_ref",
            ref_key="chr_ref", ref_path="ref.png",
        ),
        AssetPlanDependency(
            id="apd_other", plan_id="apl_other", slot="ref_identity", ref_plan_id=None,
            ref_key="missing_external", ref_path=None,
        ),
        JobInputRef(
            id="jir_a", job_id="job_a", slot_param="ref_identity", asset_id="ast_a",
            asset_version=2, role="character", required=True, enabled=True, created_at=now,
        ),
        JobInputRef(
            id="jir_missing", job_id="job_a", slot_param="ref_scene", asset_id=None,
            role="scene", required=True, enabled=True, created_at=now,
        ),
        JobInputRef(
            id="jir_disabled", job_id="job_a", slot_param="ref_prop", asset_id="ast_other",
            asset_version=1, role="prop", required=False, enabled=False, created_at=now,
        ),
        JobInputRef(
            id="jir_other", job_id="job_other", slot_param="ref_identity", asset_id="ast_other",
            asset_version=1, role="character", required=True, enabled=True, created_at=now,
        ),
    ])
    db.commit()


def _seeded(client_and_session):
    client, factory = client_and_session
    project_id = _project(client, "依赖图")
    other_project_id = _project(client, "其他项目")
    with factory() as db:
        _seed_graph(db, project_id, other_project_id)
    return client, factory, project_id, other_project_id


def test_dependency_graph_aggregates_records_and_diagnoses_without_cross_project_leaks(
    client_and_session,
):
    _, factory, project_id, _ = _seeded(client_and_session)
    with factory() as db:
        graph = build_dependency_graph(db, project_id)

    node_keys = {node["key"] for node in graph["nodes"]}
    assert {"asset:ast_a", "shot:sht_a", "job:job_a", "asset_plan:apl_ref",
            "asset_plan:apl_target", "asset:ast_missing"} <= node_keys
    assert not any("other" in key for key in node_keys)
    assert {edge["record_type"] for edge in graph["edges"]} == {
        "asset_ref", "dependency_ref", "asset_plan_dependency", "job_input_ref",
    }
    assert not any("other" in edge["id"] or "cross" in edge["id"] for edge in graph["edges"])
    assert graph["cycles"] == [["asset:ast_a", "job:job_a", "shot:sht_a"]]
    assert {issue["code"] for issue in graph["diagnostics"]} == {
        "orphan_reference", "stale_version", "dependency_invalid", "required_input_missing",
    }


def test_dependency_graph_traverses_prerequisites_and_impact(client_and_session):
    _, factory, project_id, _ = _seeded(client_and_session)
    with factory() as db:
        impact = traverse_dependency_graph(
            db, project_id, node_type="asset", node_id="ast_a", direction="impact",
        )
        prerequisites = traverse_dependency_graph(
            db, project_id, node_type="job", node_id="job_a", direction="prerequisites",
        )

    assert impact["root"] == "asset:ast_a"
    assert {node["key"] for node in impact["nodes"]} == {
        "asset:ast_a", "shot:sht_a", "job:job_a",
    }
    assert prerequisites["root"] == "job:job_a"
    assert {node["key"] for node in prerequisites["nodes"]} == {
        "asset:ast_a", "asset:ast_missing", "shot:sht_a", "job:job_a",
    }


def test_dependency_graph_api_and_project_boundaries(client_and_session):
    client, _, project_id, _ = _seeded(client_and_session)
    base = f"/api/projects/{project_id}/dependency-graph"

    response = client.get(base)
    assert response.status_code == 200, response.text
    assert response.json()["direction"] == "upstream_to_downstream"
    assert response.json()["summary"]["cycle_count"] == 1

    impact = client.get(f"{base}/asset/ast_a/impact")
    assert impact.status_code == 200
    assert impact.json()["traversal"] == "impact"
    prerequisites = client.get(f"{base}/job/job_a/prerequisites")
    assert prerequisites.status_code == 200
    assert prerequisites.json()["traversal"] == "prerequisites"

    assert client.get(f"{base}/asset/ast_other/impact").status_code == 404
    assert client.get(f"{base}/asset/ast_unknown/impact").status_code == 404
    assert client.get("/api/projects/prj_missing/dependency-graph").status_code == 404
