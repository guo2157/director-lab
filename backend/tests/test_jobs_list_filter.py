"""任务列表按镜头过滤（快照查看页的数据入口）。

镜头详情页需要列出「该镜头历次生成任务」才能选择查看哪一次的快照，
原先 ``GET /api/jobs`` 只支持项目/类型/状态过滤，前端只能拉全项目任务再筛。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Base, Job, Project, Scene, Shot


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    return TestClient(app), factory


def _seed(factory):
    db = factory()
    p = Project(id="prj_filter", name="过滤项目", aspect_ratio="9:16")
    db.add(p)
    db.flush()
    db.add(Scene(id="scn_a", project_id=p.id, episode=1, seq=1, title="场景A"))
    db.flush()
    for sid, seq in (("sht_a", 1), ("sht_b", 2)):
        db.add(Shot(id=sid, scene_id="scn_a", episode=1, seq=seq,
                    shot_kind="dialogue", duration=3))
    db.flush()
    db.add_all([
        Job(id="job_a1", project_id=p.id, shot_id="sht_a", job_type="generate_keyframe",
            status="succeeded", created_at=_now()),
        Job(id="job_a2", project_id=p.id, shot_id="sht_a", job_type="generate_clip",
            status="failed", created_at=_now()),
        Job(id="job_b1", project_id=p.id, shot_id="sht_b", job_type="generate_keyframe",
            status="succeeded", created_at=_now()),
        Job(id="job_none", project_id=p.id, job_type="parse_script",
            status="succeeded", created_at=_now()),
    ])
    db.commit()
    db.close()


def test_list_jobs_filters_by_shot(env):
    client, factory = env
    _seed(factory)

    body = client.get("/api/jobs", params={"shot_id": "sht_a"}).json()
    assert {j["id"] for j in body["items"]} == {"job_a1", "job_a2"}
    assert body["total"] == 2
    # 每条都带 shot_id，前端可直接用于快照选择
    assert all(j["shot_id"] == "sht_a" for j in body["items"])


def test_list_jobs_shot_filter_combined_with_project(env):
    """shot_id 与 project_id 同时给出时取交集，跨项目不会串数据。"""
    client, factory = env
    _seed(factory)

    body = client.get(
        "/api/jobs", params={"project_id": "prj_other", "shot_id": "sht_a"},
    ).json()
    assert body["items"] == []
    assert body["total"] == 0
