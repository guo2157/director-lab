"""跨集连续性集成测试：末帧提取、首帧注入、依赖校验、版本失效。

测试 A: 第一集末帧提取
测试 B: 第二集首帧输入
测试 C: 输出一致性（首帧路径追踪）
测试 D: 依赖调度（无末帧时阻止生成）
测试 E: 版本更新（重合成后旧末帧失效）
测试 F: 端到端多集连续生成
"""
import subprocess
import pytest
from datetime import datetime, timezone
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
    import app.worker.executor as ex_mod
    monkeypatch.setattr(ce_mod, "SessionLocal", new_session)
    monkeypatch.setattr(ex_mod, "_db", db_mod)
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    import app.core.config as cfg_mod
    cfg_mod.clear_settings_cache()
    monkeypatch.setenv("DLAB_MEDIA_DIR", str(media_dir))
    cfg_mod.clear_settings_cache()
    client = TestClient(app)
    return client, tmp_path, new_session


def _make_video(path, seconds=2, with_audio=True):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"color=c=blue:s=640x480:d={seconds}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


def _seed_two_episode_project(env, *, video_seconds=2):
    """创建一个含两集的项目，每集 1 场景 1 镜头，第一集有合成视频。"""
    client, tmp_path, Session = env
    pid = client.post("/api/projects", json={"name": "连续性测试", "aspect_ratio": "16:9"}).json()["id"]
    # 第 1 集
    scn1 = client.post("/api/scenes", json={"project_id": pid, "title": "河边", "episode": 1}).json()
    shot1 = client.post("/api/shots", json={
        "scene_id": scn1["id"], "episode": 1, "shot_kind": "visual",
        "duration": 2, "image_prompt": "小马站在河边",
    }).json()
    # 第 2 集
    scn2 = client.post("/api/scenes", json={"project_id": pid, "title": "河边", "episode": 2}).json()
    shot2 = client.post("/api/shots", json={
        "scene_id": scn2["id"], "episode": 2, "shot_kind": "visual",
        "duration": 2, "image_prompt": "小马开始过河",
    }).json()

    # 为第 1 集镜头创建合成视频
    from app.models import Asset, AssetRef, Job, Composition, Shot as ShotModel
    from app.models import ids
    db = Session()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    job = Job(id=ids.job_id(), project_id=pid, shot_id=shot1["id"],
              job_type="generate_clip", status="succeeded", created_at=now)
    db.add(job)
    video_path = tmp_path / f"{shot1['id']}.mp4"
    _make_video(video_path, seconds=video_seconds, with_audio=True)
    asset = Asset(id=ids.asset_id(), project_id=pid, asset_type="clip", name="测试视频",
                  version=1, file_path=str(video_path), source_job_id=job.id, validity="current")
    db.add(asset)
    db.add(AssetRef(id=ids.asset_ref_id(), shot_id=shot1["id"], asset_id=asset.id,
                    role="reference", source_version=1, created_at=now))
    s1 = db.get(ShotModel, shot1["id"])
    s1.status = "done"
    s1.actual_duration = float(video_seconds)
    s1.audio_source = "none"

    # 合成记录
    comp_path = tmp_path / "comp_ep1.mp4"
    _make_video(comp_path, seconds=video_seconds, with_audio=True)
    comp = Composition(
        id=ids.composition_id(), project_id=pid, episode=1,
        status="done", stale=False, file_path=str(comp_path),
        duration=float(video_seconds), source_snapshot={}, warnings=[],
        created_at=now,
    )
    db.add(comp)
    db.commit()
    db.close()
    return pid, shot1["id"], shot2["id"], comp.id


def test_a_first_episode_last_frame_extraction(env):
    """测试 A: 第一集末帧提取。"""
    from app.services.continuity import extract_episode_last_frame
    from app.models import EpisodeContinuity
    from pathlib import Path

    pid, shot1, shot2, comp_id = _seed_two_episode_project(env)
    client, tmp_path, Session = env
    db = Session()
    comp = db.query(Composition := __import__("app.models", fromlist=["Composition"]).Composition).filter_by(id=comp_id).first()
    media_dir = Path(str(tmp_path / "media"))

    rec = extract_episode_last_frame(db, pid, 1, comp, media_dir)

    assert rec.continuity_status == "extracted"
    assert rec.source_frame_path is not None
    assert rec.source_frame_hash is not None
    assert rec.source_video_duration is not None
    assert rec.source_video_duration > 0

    import os
    assert os.path.exists(rec.source_frame_path)
    assert os.path.getsize(rec.source_frame_path) > 0
    db.close()


def test_b_second_episode_first_frame_injection(env):
    """测试 B: 第二集首帧输入——连续性校验通过后可获取首帧路径。"""
    from app.services.continuity import extract_episode_last_frame, check_continuity_for_episode
    from pathlib import Path

    pid, shot1, shot2, comp_id = _seed_two_episode_project(env)
    client, tmp_path, Session = env
    db = Session()
    comp = db.query(__import__("app.models", fromlist=["Composition"]).Composition).filter_by(id=comp_id).first()
    media_dir = Path(str(tmp_path / "media"))

    # 先提取第 1 集末帧
    extract_episode_last_frame(db, pid, 1, comp, media_dir)

    # 检查第 2 集连续性
    ok, err, rec = check_continuity_for_episode(db, pid, 2)
    assert ok, f"连续性校验应通过: {err}"
    assert rec is not None
    assert rec.source_frame_path is not None
    db.close()


def test_c_output_consistency_tracking(env):
    """测试 C: 首帧路径追踪——记录实际使用的首帧路径。"""
    from app.services.continuity import (
        extract_episode_last_frame, check_continuity_for_episode,
        record_opening_frame_usage, get_or_create_continuity,
    )
    from pathlib import Path

    pid, shot1, shot2, comp_id = _seed_two_episode_project(env)
    client, tmp_path, Session = env
    db = Session()
    comp = db.query(__import__("app.models", fromlist=["Composition"]).Composition).filter_by(id=comp_id).first()
    media_dir = Path(str(tmp_path / "media"))

    extract_episode_last_frame(db, pid, 1, comp, media_dir)
    ok, _, rec = check_continuity_for_episode(db, pid, 2)
    assert ok

    # 模拟记录首帧使用
    record_opening_frame_usage(db, pid, 2, rec.source_frame_path, asset_id="ast_test123")
    ep2_rec = get_or_create_continuity(db, pid, 2)
    assert ep2_rec.opening_frame_path == rec.source_frame_path
    assert ep2_rec.opening_frame_asset_id == "ast_test123"
    db.close()


def test_d_dependency_check_blocks_without_frame(env):
    """测试 D: 依赖调度——无末帧时阻止生成。"""
    from app.services.continuity import check_continuity_for_episode

    pid, shot1, shot2, comp_id = _seed_two_episode_project(env)
    client, tmp_path, Session = env
    db = Session()

    # 不提取末帧，直接检查第 2 集
    ok, err, rec = check_continuity_for_episode(db, pid, 2)
    assert not ok
    assert err is not None
    assert "末帧" in err or "连续性" in err
    db.close()


def test_e_version_invalidation_on_recompose(env):
    """测试 E: 版本更新——重合成后旧末帧失效。"""
    from app.services.continuity import (
        extract_episode_last_frame, invalidate_downstream_continuity,
        get_or_create_continuity,
    )
    from pathlib import Path

    pid, shot1, shot2, comp_id = _seed_two_episode_project(env)
    client, tmp_path, Session = env
    db = Session()
    comp = db.query(__import__("app.models", fromlist=["Composition"]).Composition).filter_by(id=comp_id).first()
    media_dir = Path(str(tmp_path / "media"))

    # 提取第 1 集末帧
    rec1 = extract_episode_last_frame(db, pid, 1, comp, media_dir)
    assert rec1.continuity_status == "extracted"
    original_version = rec1.continuity_version

    # 模拟第 2 集也提取了末帧
    from app.models import ids
    from app.services.continuity import get_or_create_continuity
    rec2 = get_or_create_continuity(db, pid, 2)
    rec2.continuity_status = "extracted"
    rec2.source_frame_path = "/fake/path.png"
    db.commit()

    # 第 1 集重合成，使下游失效
    invalidated = invalidate_downstream_continuity(db, pid, 1)
    assert len(invalidated) >= 1

    # 检查第 2 集记录已失效
    rec2_after = get_or_create_continuity(db, pid, 2)
    assert rec2_after.continuity_status == "pending"
    assert rec2_after.source_frame_path is None
    assert rec2_after.continuity_version > 1
    db.close()


def test_f_end_to_end_multi_episode(env):
    """测试 F: 端到端——多集连续提取末帧、传递首帧、记录结果。"""
    from app.services.continuity import (
        extract_episode_last_frame, check_continuity_for_episode,
        record_opening_frame_usage, record_verification_result,
        get_or_create_continuity,
    )
    from app.models import Composition, ids
    from pathlib import Path
    import os

    pid, shot1, shot2, comp_id = _seed_two_episode_project(env, video_seconds=3)
    client, tmp_path, Session = env
    db = Session()
    media_dir = Path(str(tmp_path / "media"))

    # Step 1: 第 1 集合成成功 → 提取末帧
    comp1 = db.query(Composition).filter_by(id=comp_id).first()
    rec1 = extract_episode_last_frame(db, pid, 1, comp1, media_dir)
    assert rec1.continuity_status == "extracted"
    assert os.path.exists(rec1.source_frame_path)

    # Step 2: 第 2 集检查连续性 → 通过
    ok, err, prev_rec = check_continuity_for_episode(db, pid, 2)
    assert ok, f"第 2 集连续性校验失败: {err}"

    # Step 3: 模拟第 2 集使用首帧生成
    record_opening_frame_usage(db, pid, 2, prev_rec.source_frame_path, asset_id="ast_clip_ep2")

    # Step 4: 模拟验证结果
    record_verification_result(db, pid, 2, {
        "passed": True,
        "first_frame_match": True,
        "note": "首帧与上一集末帧一致",
    })

    # Step 5: 确认记录完整
    ep2_rec = get_or_create_continuity(db, pid, 2)
    assert ep2_rec.continuity_status == "passed"
    assert ep2_rec.opening_frame_path == prev_rec.source_frame_path
    assert ep2_rec.verification_result["passed"] is True
    db.close()
