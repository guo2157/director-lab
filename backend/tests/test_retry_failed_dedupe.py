"""锁死「批量重试不得为同一镜头建多个任务」的行为（2026-11-24 缺陷 B）。

背景：`POST /api/jobs/projects/{id}/retry-failed` 的 inflight 集合只在入口预取一次，
循环内新建的 queued 任务没有回写集合。于是同一镜头的两条失败历史（att1 / att2）
会各建一个重试任务 —— 实测 E2E prj_7717c671b15c 出现镜 3 同时排队
job_9bf6f2cd3c88 与 job_678e5c8960ee，白跑一次云端出图。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.database import make_engine
from app.main import app
from app.models import Base, Job, Project


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    from sqlalchemy.orm import sessionmaker

    new_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", new_session)
    import app.worker.parse_executor as pe_mod

    monkeypatch.setattr(pe_mod, "SessionLocal", new_session)
    with TestClient(app) as c:
        yield c, new_session


def _seed(session_factory, shot_id: str, attempts: int, jid: str, status: str = "failed"):
    from datetime import datetime, timezone

    from app.models import Scene, Shot

    db = session_factory()
    try:
        if db.get(Project, "prj_retrytest") is None:
            db.add(
                Project(
                    id="prj_retrytest",
                    name="retry-test",
                    creation_type="script",
                    aspect_ratio="9:16",
                    status="active",
                )
            )
            db.add(
                Scene(
                    id="scn_retrytest",
                    project_id="prj_retrytest",
                    episode=1,
                    seq=1,
                    title="测试场景",
                )
            )
            db.commit()
        if db.get(Shot, shot_id) is None:
            db.add(
                Shot(
                    id=shot_id,
                    scene_id="scn_retrytest",
                    episode=1,
                    seq=int(shot_id.rsplit("_", 1)[-1]) if shot_id.rsplit("_", 1)[-1].isdigit() else 1,
                )
            )
            db.commit()
        db.add(
            Job(
                id=jid,
                project_id="prj_retrytest",
                shot_id=shot_id,
                job_type="generate_keyframe",
                status=status,
                attempts=attempts,
                error_code="comfy_unreachable" if status == "failed" else None,
                error_msg="boom" if status == "failed" else None,
                params={},
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        db.commit()
    finally:
        db.close()


def _queued(session_factory) -> list[str]:
    db = session_factory()
    try:
        return [
            j.id
            for j in db.query(Job)
            .filter(Job.project_id == "prj_retrytest", Job.status == "queued")
            .all()
        ]
    finally:
        db.close()


def test_retry_failed_dedupes_same_shot_in_one_batch(client):
    """同一镜头两条失败历史 → 只建一个重试任务。"""
    c, sf = client
    _seed(sf, "sht_dup", 1, "job_a1")
    _seed(sf, "sht_dup", 2, "job_a2")

    res = c.post("/api/jobs/projects/prj_retrytest/retry-failed", json={})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["retried_count"] == 1, f"应只建 1 个重试任务，实际 {body}"
    assert len(_queued(sf)) == 1, f"queued 应只有 1 条，实际 {_queued(sf)}"


def test_retry_failed_skips_shot_already_inflight(client):
    """已有在途任务的镜头，不应再建重试任务。"""
    c, sf = client
    _seed(sf, "sht_busy", 1, "job_b1")
    _seed(sf, "sht_busy", 1, "job_inflight", status="running")

    res = c.post("/api/jobs/projects/prj_retrytest/retry-failed", json={})
    assert res.status_code == 201, res.text
    assert res.json()["retried_count"] == 0
    assert _queued(sf) == []


def test_retry_failed_keeps_distinct_shots(client):
    """不同镜头各自失败 → 各建一个，不被误去重。"""
    c, sf = client
    _seed(sf, "sht_x", 1, "job_x")
    _seed(sf, "sht_y", 1, "job_y")

    res = c.post("/api/jobs/projects/prj_retrytest/retry-failed", json={})
    assert res.status_code == 201, res.text
    assert res.json()["retried_count"] == 2
    assert len(_queued(sf)) == 2
