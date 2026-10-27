"""clip 产物 ffprobe 检测测试：executor 在 clip 生成成功后回写
Shot.actual_duration / video_actual_duration / audio_source（compose 对齐依赖）。
"""
import subprocess

import pytest
from app.core.database import make_engine
from app.models import Base


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    return Session()


def _make_video(path: str, with_audio: bool, seconds: float = 2.0) -> None:
    """用 ffmpeg lavfi 生成真实测试视频。"""
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x256:rate=10"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-c:v", "libx264", "-c:a", "aac", "-shortest"]
    else:
        cmd += ["-c:v", "libx264"]
    cmd.append(path)
    subprocess.run(cmd, check=True, capture_output=True)


def _make_shot(db, episode=1, seq=1):
    from app.models import Project, Scene, Shot
    p = Project(name="probe 项目", aspect_ratio="9:16")
    db.add(p)
    db.flush()
    scn = Scene(project_id=p.id, title="场景", episode=1, seq=1)
    db.add(scn)
    db.flush()
    shot = Shot(scene_id=scn.id, episode=episode, seq=seq, shot_kind="visual", duration=5)
    db.add(shot)
    db.commit()
    return shot


def _make_clip_job(db, shot):
    from datetime import datetime, timezone
    from app.models import Job, Scene
    scn = db.get(Scene, shot.scene_id)
    job = Job(project_id=scn.project_id,
              shot_id=shot.id, job_type="generate_clip", status="succeeded",
              created_at=datetime.now(timezone.utc))
    db.add(job)
    db.commit()
    return job


def test_probe_clip_with_audio_writes_h3_va(db_session, tmp_path):
    """含音频流的视频：actual_duration/video_actual_duration 回写，audio_source=h3_va。"""
    from app.worker.executor import _probe_clip_and_update_shot
    shot = _make_shot(db_session)
    job = _make_clip_job(db_session, shot)
    video = tmp_path / "clip_va.mp4"
    _make_video(str(video), with_audio=True, seconds=2.0)
    assets = [{"asset_type": "clip", "file_path": str(video)}]
    _probe_clip_and_update_shot(db_session, job, assets)
    db_session.refresh(shot)
    assert shot.audio_source == "h3_va"
    assert shot.actual_duration is not None and 1.5 < shot.actual_duration < 2.5
    assert shot.video_actual_duration == shot.actual_duration


def test_probe_clip_without_audio_writes_none(db_session, tmp_path):
    """无音频流的视频：audio_source=none。"""
    from app.worker.executor import _probe_clip_and_update_shot
    shot = _make_shot(db_session)
    job = _make_clip_job(db_session, shot)
    video = tmp_path / "clip_noaudio.mp4"
    _make_video(str(video), with_audio=False, seconds=3.0)
    assets = [{"asset_type": "clip", "file_path": str(video)}]
    _probe_clip_and_update_shot(db_session, job, assets)
    db_session.refresh(shot)
    assert shot.audio_source == "none"
    assert shot.actual_duration is not None and 2.5 < shot.actual_duration < 3.5


def test_probe_failure_does_not_block(db_session, tmp_path):
    """ffprobe 失败（文件损坏/不存在）：不抛异常，Shot 字段保持原值。"""
    from app.worker.executor import _probe_clip_and_update_shot
    shot = _make_shot(db_session)
    job = _make_clip_job(db_session, shot)
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video")
    assets = [{"asset_type": "clip", "file_path": str(bad)}]
    # 不应抛出异常
    _probe_clip_and_update_shot(db_session, job, assets)
    db_session.refresh(shot)
    assert shot.audio_source is None
    assert shot.actual_duration is None


def test_probe_ignores_non_clip_jobs(db_session, tmp_path):
    """非 generate_clip 任务或无 clip 产物：直接返回，不执行 ffprobe。"""
    from app.worker.executor import _probe_clip_and_update_shot
    shot = _make_shot(db_session)
    job = _make_clip_job(db_session, shot)
    job.job_type = "generate_keyframe"
    db_session.commit()
    # keyframe 任务：不处理
    _probe_clip_and_update_shot(db_session, job, [{"asset_type": "keyframe", "file_path": "/tmp/x.png"}])
    db_session.refresh(shot)
    assert shot.audio_source is None
    # clip 任务但产物列表无 clip：不处理
    job.job_type = "generate_clip"
    db_session.commit()
    _probe_clip_and_update_shot(db_session, job, [{"asset_type": "keyframe", "file_path": "/tmp/x.png"}])
    db_session.refresh(shot)
    assert shot.audio_source is None
