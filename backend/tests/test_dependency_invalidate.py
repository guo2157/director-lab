"""依赖失效标记：显式写入 DependencyRef.validity、幂等、项目隔离与读路径联动。"""
import pytest
from fastapi.testclient import TestClient

from app.core.database import make_engine
from app.main import app
from app.models import Asset, Base, DependencyRef, Scene, Shot


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
    db.add_all([
        Scene(id="scn_a", project_id=project_id, episode=1, seq=1, title="教室"),
        Scene(id="scn_other", project_id=other_project_id, episode=1, seq=1, title="外部场景"),
    ])
    db.flush()
    db.add_all([
        Shot(id="sht_a", scene_id="scn_a", episode=1, seq=1, shot_kind="visual", duration=5),
        Shot(id="sht_b", scene_id="scn_a", episode=1, seq=2, shot_kind="visual", duration=5),
        Shot(id="sht_other", scene_id="scn_other", episode=1, seq=1,
             shot_kind="visual", duration=5),
        Asset(id="ast_v1", project_id=project_id, asset_type="shot_image", name="关键帧",
              version=1, validity="superseded", file_path="/tmp/v1.png"),
        Asset(id="ast_v2", project_id=project_id, asset_type="shot_image", name="关键帧",
              version=2, validity="current", file_path="/tmp/v2.png"),
        Asset(id="ast_other", project_id=other_project_id, asset_type="shot_image",
              name="外部资产", version=1, validity="current", file_path="/tmp/o.png"),
        # 旧版本引用（可标记）+ 当前版本引用（保持）
        DependencyRef(id="dep_old", source_type="asset", source_id="ast_v1",
                      source_version=1, target_type="shot", target_id="sht_b",
                      dependency_role="last_frame", validity="current"),
        DependencyRef(id="dep_new", source_type="asset", source_id="ast_v2",
                      source_version=2, target_type="shot", target_id="sht_b",
                      dependency_role="last_frame", validity="current"),
        # 非资产来源（镜头）：没有版本概念，source_version 仅为必填占位
        DependencyRef(id="dep_shot", source_type="shot", source_id="sht_a",
                      source_version=1, target_type="shot", target_id="sht_b",
                      dependency_role="previous_shot", validity="current"),
    ])
    db.commit()


def _invalidate(client: TestClient, project_id: str, node_type: str, node_id: str):
    return client.post(
        f"/api/projects/{project_id}/dependency-graph/{node_type}/{node_id}/invalidate",
    )


def test_invalidate_marks_only_outdated_edges_and_updates_preview(client_and_session):
    client, factory = client_and_session
    project_id = _project(client, "失效标记")
    other_project_id = _project(client, "别的项目")
    session = factory()
    try:
        _seed(session, project_id, other_project_id)
    finally:
        session.close()

    # 标记前：旧版本边可标记，且已出现在待处理根里
    before = client.get(f"/api/projects/{project_id}/dependency-graph/impact-preview").json()
    roots = {root["key"]: root for root in before["changed_roots"]}
    assert roots["asset:ast_v1"]["markable_edge_count"] == 1

    response = _invalidate(client, project_id, "asset", "ast_v1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["algorithm"] == "dependency-invalidate-v1"
    assert body["policy"]["writes"] is True
    assert body["policy"]["auto_regenerate"] is False
    assert body["summary"] == {"marked": 1, "already_stale": 0, "kept_current": 1}
    assert [item["edge_id"] for item in body["marked"]] == ["dep_old"]

    # 读路径联动：依赖图里该边已失效并产生 stale 诊断
    graph = client.get(f"/api/projects/{project_id}/dependency-graph").json()
    edges = {edge["id"]: edge for edge in graph["edges"]}
    assert edges["dependency_ref:dep_old"]["validity"] == "stale"
    assert edges["dependency_ref:dep_new"]["validity"] == "current"
    assert any(
        issue["code"] == "dependency_invalid" and issue["edge_id"] == "dependency_ref:dep_old"
        for issue in graph["diagnostics"]
    )

    # 标记后：该根不再有可写边
    after = client.get(f"/api/projects/{project_id}/dependency-graph/impact-preview").json()
    roots = {root["key"]: root for root in after["changed_roots"]}
    assert roots["asset:ast_v1"]["markable_edge_count"] == 0


def test_invalidate_is_idempotent(client_and_session):
    client, factory = client_and_session
    project_id = _project(client, "失效标记")
    other_project_id = _project(client, "别的项目")
    session = factory()
    try:
        _seed(session, project_id, other_project_id)
    finally:
        session.close()

    assert _invalidate(client, project_id, "asset", "ast_v1").status_code == 200
    second = _invalidate(client, project_id, "asset", "ast_v1")
    assert second.status_code == 200
    assert second.json()["summary"] == {"marked": 0, "already_stale": 1, "kept_current": 1}
    assert second.json()["marked"] == []


def test_invalidate_non_asset_source_marks_all_current_edges(client_and_session):
    client, factory = client_and_session
    project_id = _project(client, "失效标记")
    other_project_id = _project(client, "别的项目")
    session = factory()
    try:
        _seed(session, project_id, other_project_id)
    finally:
        session.close()

    body = _invalidate(client, project_id, "shot", "sht_a").json()
    assert body["root"]["version"] is None
    assert body["summary"] == {"marked": 1, "already_stale": 0, "kept_current": 0}
    assert body["marked"][0]["edge_id"] == "dep_shot"


def test_invalidate_rejects_foreign_or_unknown_nodes(client_and_session):
    client, factory = client_and_session
    project_id = _project(client, "失效标记")
    other_project_id = _project(client, "别的项目")
    session = factory()
    try:
        _seed(session, project_id, other_project_id)
    finally:
        session.close()

    assert _invalidate(client, project_id, "asset", "ast_other").status_code == 404
    assert _invalidate(client, project_id, "shot", "sht_other").status_code == 404
    assert _invalidate(client, project_id, "asset", "ast_missing").status_code == 404
    assert _invalidate(client, "prj_missing", "asset", "ast_v1").status_code == 404
