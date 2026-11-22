"""任务快照参考图版本比对（只读）。

快照记录的是生成当时的资产版本；资产后来被替换后，快照仍然有效（历史记录），
但需要明确告诉用户「这张参考图已经不是当前版本」，且不做任何自动重做。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Asset, Base, Job, JobInputRef, Project


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
    db.add(Project(id="prj_ver", name="版本项目", aspect_ratio="9:16"))
    db.add_all([
        Asset(id="ast_cur", project_id="prj_ver", asset_type="character",
              name="女主", version=3, validity="current"),
        Asset(id="ast_same", project_id="prj_ver", asset_type="scene",
              name="客厅", version=1, validity="current"),
    ])
    db.add(Job(id="job_ver", project_id="prj_ver", job_type="generate_keyframe",
               status="succeeded", created_at=_now()))
    db.flush()
    db.add_all([
        # 快照用了 v2，资产已到 v3 → 过期
        JobInputRef(id="jir_1", job_id="job_ver", seq=0, slot_param="ref_identity",
                    asset_id="ast_cur", asset_version=2, asset_type="character",
                    role="character", required=True, semantic="reference", enabled=True,
                    created_at=_now()),
        # 快照版本与当前一致 → 未过期
        JobInputRef(id="jir_2", job_id="job_ver", seq=1, slot_param="ref_scene",
                    asset_id="ast_same", asset_version=1, asset_type="scene",
                    role="scene", required=False, semantic="reference", enabled=True,
                    created_at=_now()),
        # 资产已被删除 → 无法判断版本
        JobInputRef(id="jir_3", job_id="job_ver", seq=2, slot_param="ref_prop",
                    asset_id="ast_gone", asset_version=1, asset_type="prop",
                    role="prop", required=False, semantic="reference", enabled=False,
                    created_at=_now()),
        # 该任务产出的分镜图版本（旧版已 superseded，但记录保留，可作为重做基准）
        Asset(id="ast_out", project_id="prj_ver", asset_type="keyframe",
              name="镜头1分镜图", version=2, validity="superseded",
              source_job_id="job_ver"),
    ])
    db.commit()
    db.close()


def test_snapshot_refs_report_current_version_diff(env):
    client, factory = env
    _seed(factory)

    body = client.get("/api/jobs/job_ver/snapshot").json()
    refs = {r["slot_param"]: r for r in body["refs"]}

    assert refs["ref_identity"]["current_version"] == 3
    assert refs["ref_identity"]["version_stale"] is True
    # 快照里的历史版本不被改写
    assert refs["ref_identity"]["asset_version"] == 2

    assert refs["ref_scene"]["current_version"] == 1
    assert refs["ref_scene"]["version_stale"] is False

    assert refs["ref_prop"]["current_version"] is None
    assert refs["ref_prop"]["version_stale"] is None


def test_snapshot_reports_result_asset_for_rebase(env):
    """快照返回该任务产出的资产版本，供「以历史版本为基准重做」使用。"""
    client, factory = env
    _seed(factory)

    body = client.get("/api/jobs/job_ver/snapshot").json()
    assert body["result_asset"] == {
        "asset_id": "ast_out",
        "asset_type": "keyframe",
        "version": 2,
        "validity": "superseded",
    }


def test_snapshot_result_asset_null_without_output(env):
    """没有产出资产的任务，result_asset 为 None，前端不显示重做入口。"""
    client, factory = env
    _seed(factory)
    with factory() as db:
        db.add(Job(id="job_no_out", project_id="prj_ver", job_type="generate_keyframe",
                   status="succeeded", created_at=_now()))
        db.commit()

    body = client.get("/api/jobs/job_no_out/snapshot").json()
    assert body["result_asset"] is None


def test_snapshot_version_diff_is_read_only(env):
    """比对不修改资产版本、不标记失效、不改快照记录。"""
    client, factory = env
    _seed(factory)
    client.get("/api/jobs/job_ver/snapshot")

    with factory() as db:
        assert db.get(Asset, "ast_cur").version == 3
        assert db.get(Asset, "ast_cur").validity == "current"
        assert db.get(JobInputRef, "jir_1").asset_version == 2
        assert db.get(Job, "job_ver").status == "succeeded"
