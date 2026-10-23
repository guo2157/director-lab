"""阶段 4 合成与导出链路集成测试：compose/export 接口 + 执行器（真实 FFmpeg）。
镜头产物用程序生成的极简视频文件驱动，验证时间轴校验、合成落库、导出封装与下载。
"""
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import make_engine, SessionLocal
from app.models import Base


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    from sqlalchemy.orm import sessionmaker
    new_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", new_session)
    import app.worker.compose_executor as ce_mod
    import app.worker.export_executor as ee_mod
    monkeypatch.setattr(ce_mod, "SessionLocal", new_session)
    monkeypatch.setattr(ee_mod, "SessionLocal", new_session)
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    import app.core.config as cfg_mod
    cfg_mod.get_settings.cache_clear()
    monkeypatch.setenv("DLAB_MEDIA_DIR", str(media_dir))
    cfg_mod.get_settings.cache_clear()
    client = TestClient(app)
    return client, tmp_path, new_session


def _make_video(path, seconds=2, with_audio=True):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"color=c=blue:s=320x240:d={seconds}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


def _seed_project_with_shot(env, *, shot_status="done", video_seconds=2, actual=2.0, audio_source="none"):
    """造一个项目：1 场景 1 镜头，镜头带一个成功任务产出的视频资产。返回 (pid, shot_id, job_id)。"""
    client, tmp_path, Session = env
    pid = client.post("/api/projects", json={"name": "合成测试", "aspect_ratio": "16:9"}).json()["id"]
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "教室", "episode": 1}).json()
    shot = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "visual",
        "duration": 2, "image_prompt": "测试画面",
    }).json()
    # 直接落库：成功任务 + 视频资产 + 绑定
    from app.models import Asset, AssetRef, Job, Shot as ShotModel
    from app.models import ids
    from datetime import datetime, timezone
    db = Session()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    job = Job(id=ids.job_id(), project_id=pid, shot_id=shot["id"], job_type="generate_clip",
              status="succeeded", created_at=now)
    db.add(job)
    video_path = tmp_path / f"{shot['id']}.mp4"
    _make_video(video_path, seconds=video_seconds, with_audio=True)
    asset = Asset(id=ids.asset_id(), project_id=pid, asset_type="clip", name="测试视频",
                  version=1, file_path=str(video_path), source_job_id=job.id, validity="current")
    db.add(asset)
    db.add(AssetRef(id=ids.asset_ref_id(), shot_id=shot["id"], asset_id=asset.id,
                    role="reference", source_version=1, created_at=now))
    s = db.get(ShotModel, shot["id"])
    s.status = shot_status
    s.audio_source = audio_source
    s.actual_duration = actual
    db.commit()
    db.close()
    return pid, shot["id"], job.id


def test_compose_rejects_unreviewed_mismatch(env):
    pid, shot_id, _ = _seed_project_with_shot(env, shot_status="review")
    r = env[0].post(f"/api/projects/{pid}/compose", json={})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "duration_mismatch_review_required"


def test_compose_rejects_archived(env):
    client, tmp_path, Session = env
    pid, _, _ = _seed_project_with_shot(env)
    # 归档项目
    from app.models import Project
    db = Session()
    db.get(Project, pid).status = "archived"
    db.commit(); db.close()
    r = client.post(f"/api/projects/{pid}/compose", json={})
    assert r.status_code == 409


def test_compose_full_pipeline(env):
    client = env[0]
    pid, shot_id, _ = _seed_project_with_shot(env, video_seconds=2, audio_source="none")
    r = client.post(f"/api/projects/{pid}/compose",
                    json={"include_subtitle": False, "compose_tolerance": 0.5})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    import app.worker.compose_executor as ce
    result = ce.execute_compose_job(job_id)
    assert result["ok"] is True, result
    assert result["duration"] > 1.5
    # 合成版本落库
    comps = client.get(f"/api/projects/{pid}/compositions").json()
    assert comps["total"] == 1
    comp = comps["items"][0]
    assert comp["status"] == "done" and comp["stale"] is False
    assert comp["duration"] and comp["duration"] > 1.5
    # 详情含快照
    detail = client.get(f"/api/projects/{pid}/compositions/{comp['composition_id']}").json()
    assert "1" in (detail["source_snapshot"] or {})
    assert detail["source_snapshot"]["1"][0]["shot_id"] == shot_id


def test_export_requires_valid_composition(env):
    client = env[0]
    pid = client.post("/api/projects", json={"name": "导出测试", "aspect_ratio": "16:9"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/export", json={"composition_id": "cmp_not_exist"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "composition_required"


def test_export_full_pipeline(env, tmp_path):
    client = env[0]
    pid, shot_id, _ = _seed_project_with_shot(env, video_seconds=2)
    r = client.post(f"/api/projects/{pid}/compose", json={"include_subtitle": False})
    import app.worker.compose_executor as ce
    ce.execute_compose_job(r.json()["job_id"])
    comp_id = client.get(f"/api/projects/{pid}/compositions").json()["items"][0]["composition_id"]
    # 创建导出任务并执行
    r = client.post(f"/api/projects/{pid}/export",
                    json={"composition_id": comp_id, "delivery_profile": "standard"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    import app.worker.export_executor as ee
    result = ee.execute_export_job(job_id)
    assert result["ok"] is True, result
    assert result["size"] > 0
    # 导出记录与下载
    exports = client.get(f"/api/projects/{pid}/exports").json()
    assert exports["total"] == 1
    item = exports["items"][0]
    assert item["status"] == "done" and item["size"] > 0
    dl = client.get(item["download_url"])
    assert dl.status_code == 200
    assert len(dl.content) == item["size"]
    # 过期合成不可导出
    from app.models import Composition
    db = env[2]()
    db.get(Composition, comp_id).stale = True
    db.commit(); db.close()
    r = client.post(f"/api/projects/{pid}/export", json={"composition_id": comp_id})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "composition_stale"
