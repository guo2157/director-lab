"""镜头完成度判定测试（PRD §6.7 音频必需性）。

核心回归：对白/旁白镜头只要 H3 VA 音轨内嵌在 clip 里，就应置 done，
不得再要求独立的 audio 资产（否则合成收不到对白镜头，字幕永远为空）。
"""
import pytest
from sqlalchemy.orm import sessionmaker

from datetime import datetime, timezone

from app.core.database import make_engine
from app.models import Asset, Base, Job, Project, Scene, Shot
from app.models import ids
from app.services.shot_state import has_current_asset, shot_audio_ready, shot_ready


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    yield session
    session.close()


def _project_scene_shot(db, *, shot_kind="dialogue", dialogue="台词", audio_source=None):
    p = Project(id=ids.project_id(), name="镜头状态测试", aspect_ratio="9:16")
    db.add(p)
    db.flush()
    scn = Scene(id=ids.scene_id(), project_id=p.id, title="场景", episode=1, seq=1)
    db.add(scn)
    db.flush()
    shot = Shot(id=ids.shot_id(), scene_id=scn.id, episode=1, seq=1, shot_kind=shot_kind,
                dialogue=dialogue, duration=5, audio_source=audio_source)
    db.add(shot)
    db.commit()
    return p, scn, shot


def _add_asset(db, project_id, shot, asset_type, *, job_status="succeeded", validity="current"):
    job = Job(id=ids.job_id(), project_id=project_id, shot_id=shot.id,
              job_type=f"generate_{asset_type}", status=job_status,
              created_at=datetime.now(timezone.utc).replace(tzinfo=None))
    db.add(job)
    db.flush()
    asset = Asset(id=ids.asset_id(), project_id=project_id, asset_type=asset_type,
                  name=f"{asset_type}", version=1, file_path=f"/tmp/{asset_type}.bin",
                  source_job_id=job.id, validity=validity)
    db.add(asset)
    db.commit()
    return asset


def test_h3_va_dialogue_shot_ready_without_audio_asset(db):
    """对白镜头：分镜图 + 带 H3 VA 音轨的 clip 即视为产物齐全。"""
    p, _, shot = _project_scene_shot(db, audio_source="h3_va")
    _add_asset(db, p.id, shot, "keyframe")
    _add_asset(db, p.id, shot, "clip")
    assert shot_audio_ready(db, shot) is True
    assert shot_ready(db, shot) is True


def test_dialogue_shot_without_any_audio_not_ready(db):
    """对白镜头：无内嵌音轨且无 audio 资产 → 继续等待配音。"""
    p, _, shot = _project_scene_shot(db, audio_source=None)
    _add_asset(db, p.id, shot, "keyframe")
    _add_asset(db, p.id, shot, "clip")
    assert shot_audio_ready(db, shot) is False
    assert shot_ready(db, shot) is False


def test_dialogue_shot_with_tts_audio_ready(db):
    """TTS 补救路径：h3_va 缺失但有 audio 资产同样满足音频必需性。"""
    p, _, shot = _project_scene_shot(db, audio_source=None)
    _add_asset(db, p.id, shot, "keyframe")
    _add_asset(db, p.id, shot, "clip")
    _add_asset(db, p.id, shot, "audio")
    assert shot_ready(db, shot) is True


def test_visual_shot_ready_without_audio(db):
    p, _, shot = _project_scene_shot(db, shot_kind="visual", dialogue="")
    _add_asset(db, p.id, shot, "keyframe")
    _add_asset(db, p.id, shot, "clip")
    assert shot_ready(db, shot) is True


def test_missing_keyframe_or_clip_not_ready(db):
    p, _, shot = _project_scene_shot(db, audio_source="h3_va")
    _add_asset(db, p.id, shot, "clip")
    assert shot_ready(db, shot) is False
    _add_asset(db, p.id, shot, "keyframe")
    assert shot_ready(db, shot) is True


def test_failed_job_or_stale_asset_not_counted(db):
    """产物有效性口径：来源任务失败或版本非 current 都不算数。"""
    p, _, shot = _project_scene_shot(db, audio_source="h3_va")
    _add_asset(db, p.id, shot, "keyframe", job_status="failed")
    _add_asset(db, p.id, shot, "clip", validity="superseded")
    assert has_current_asset(db, shot.id, "keyframe") is False
    assert has_current_asset(db, shot.id, "clip") is False
    assert shot_ready(db, shot) is False
