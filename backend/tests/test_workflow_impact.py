"""工作流版本影响预检：版本比对、停用/缺失分类、只读策略与项目隔离。"""
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.database import make_engine
from app.main import app
from app.models import Asset, Base, Job, Scene, Shot, Workflow


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
        Workflow(id="wf_a", name="分镜图工作流", workflow_type="image",
                 definition={"nodes": []}, version=3, status="published"),
        Workflow(id="wf_off", name="停用工作流", workflow_type="image",
                 definition={"nodes": []}, version=2, status="disabled"),
    ])
    db.flush()
    db.add_all([
        Shot(id="sht_a", scene_id="scn_a", episode=1, seq=1, shot_kind="visual", duration=5),
        Shot(id="sht_other", scene_id="scn_other", episode=1, seq=1, shot_kind="visual", duration=5),
        Job(id="job_changed", project_id=project_id, shot_id="sht_a", job_type="storyboard_image",
            workflow_id="wf_a", workflow_version=2, status="succeeded", created_at=now),
        Job(id="job_current", project_id=project_id, shot_id="sht_a", job_type="storyboard_image",
            workflow_id="wf_a", workflow_version=3, status="succeeded", created_at=now),
        Job(id="job_disabled", project_id=project_id, shot_id="sht_a", job_type="storyboard_image",
            workflow_id="wf_off", workflow_version=2, status="succeeded", created_at=now),
        Job(id="job_other", project_id=other_project_id, shot_id="sht_other",
            job_type="storyboard_image", workflow_id="wf_a", workflow_version=2,
            status="succeeded", created_at=now),
        Asset(id="ast_out", project_id=project_id, asset_type="shot_image", name="分镜",
              version=1, validity="current", file_path="/tmp/out.png", source_job_id="job_changed"),
    ])
    db.commit()


def _impact(client: TestClient, project_id: str) -> dict:
    response = client.get(f"/api/projects/{project_id}/workflow-impact")
    assert response.status_code == 200, response.text
    return response.json()


def test_workflow_impact_classifies_version_changed_disabled_and_missing(client_and_session):
    client, factory = client_and_session
    project_id = _project(client, "工作流影响")
    other_project_id = _project(client, "别的项目")
    session = factory()
    try:
        _seed(session, project_id, other_project_id)
    finally:
        session.close()

    body = _impact(client, project_id)
    assert body["algorithm"] == "workflow-impact-v1"
    assert body["policy"]["writes"] is False
    assert body["policy"]["auto_regenerate"] is False

    by_job = {item["job_id"]: item for item in body["items"]}
    assert set(by_job) == {"job_changed", "job_disabled"}

    changed = by_job["job_changed"]
    assert changed["change_status"] == "workflow_changed"
    assert changed["job_workflow_version"] == 2
    assert changed["current_workflow_version"] == 3
    assert changed["shot_id"] == "sht_a"
    assert changed["episode"] == 1
    assert changed["output_asset"]["asset_id"] == "ast_out"

    assert by_job["job_disabled"]["change_status"] == "workflow_disabled"
    assert by_job["job_disabled"]["has_output"] is False

    assert body["summary"] == {
        "checked_job_count": 3,
        "affected_job_count": 2,
        "affected_shot_count": 1,
        "workflow_changed": 1,
        "workflow_disabled": 1,
        "unknown_workflow_version": 0,
    }


def test_workflow_impact_is_read_only_and_project_isolated(client_and_session):
    client, factory = client_and_session
    project_id = _project(client, "工作流影响")
    other_project_id = _project(client, "别的项目")
    session = factory()
    try:
        _seed(session, project_id, other_project_id)
    finally:
        session.close()

    before = _impact(client, project_id)
    after = _impact(client, project_id)
    assert before == after

    # 其他项目的任务不会混入本项目结果
    assert all(item["job_id"] != "job_other" for item in before["items"])


def test_workflow_impact_unknown_project_returns_404(client_and_session):
    client, _ = client_and_session
    response = client.get("/api/projects/prj_missing/workflow-impact")
    assert response.status_code == 404
