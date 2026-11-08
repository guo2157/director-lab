"""角色音色智能匹配测试（2026-11-07）。

覆盖：
- 系统音色库 API：列表/过滤/结构
- 程序评分：性别/年龄/标签维度计算正确
- 批量匹配：LLM 降级（无密钥）仍可运行；锁定角色排除；speaker_id 只能来自真实音色库
- 音色确认：approved 置位 + waiting_voice_confirmation 对白放行
- 锁定/解锁与绑定详情
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import make_engine
from app.models import Base


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """与 test_audio_policy.env 相同的隔离环境（独立临时库 + 独立媒体目录）。"""
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    from sqlalchemy.orm import sessionmaker
    new_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", new_session)
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    import app.core.config as cfg_mod
    monkeypatch.setenv("DLAB_MEDIA_DIR", str(media_dir))
    cfg_mod.clear_settings_cache()
    client = TestClient(app)
    return client, tmp_path, new_session


def _seed_voices(db):
    from app.models import VoiceLibrary
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [
        VoiceLibrary(id="vl_m1", voice_code="male_a", name="男性音色 A", speaker_id="Uncle_Fu",
                     display_name="傅叔叔", gender_presentation="male", perceived_age="mature",
                     pitch="low", timbre=["低沉", "醇厚"], personality_tags=["成熟"],
                     speaking_styles=[], language="中文", enabled=True, sort_order=1, created_at=now),
        VoiceLibrary(id="vl_m2", voice_code="male_b", name="男性音色 B", speaker_id="Dylan",
                     display_name="迪伦", gender_presentation="male", perceived_age="young",
                     pitch="unknown", timbre=["清亮", "自然"], personality_tags=[],
                     speaking_styles=["自然"], language="中文", enabled=True, sort_order=2, created_at=now),
        VoiceLibrary(id="vl_f1", voice_code="female_f", name="女性音色 F", speaker_id="Vivian",
                     display_name="薇薇安", gender_presentation="female", perceived_age="young",
                     pitch="unknown", timbre=["明亮"], personality_tags=[],
                     speaking_styles=[], language="中文", enabled=True, sort_order=6, created_at=now),
    ]
    db.add_all(rows)
    db.commit()
    return rows


def _seed_project(env):
    client = env[0]
    # 与 test_audio_policy._seed_project 一致：aspect_ratio 为项目创建必填项
    r = client.post("/api/projects", json={"name": "音色测试项目", "aspect_ratio": "16:9"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _seed_characters(db, pid, names):
    from app.models import Character
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    for i, n in enumerate(names):
        c = Character(id=f"chr_test_{i}", project_id=pid, name=n,
                      description=f"测试角色{n}", created_at=now + timedelta(seconds=i))
        db.add(c)
        rows.append(c)
    db.commit()
    return rows


def test_system_voices_list(env):
    client, tmp_path, Session = env
    db = Session()
    _seed_voices(db)
    db.close()
    r = client.get("/api/voices/system")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 3
    assert data["male_count"] == 2 and data["female_count"] == 1
    male_a = next(v for v in data["items"] if v["voice_code"] == "male_a")
    assert male_a["speaker_id"] == "Uncle_Fu"
    assert "低沉" in male_a["timbre"]
    r2 = client.get("/api/voices/system", params={"gender": "female"})
    assert r2.json()["total"] == 1
    r3 = client.get("/api/voices/system", params={"enabled": False})
    assert r3.json()["total"] == 0


def test_score_voice_dimensions():
    from app.models.audio import VoiceLibrary
    from app.services import voice_matching as vm
    v = VoiceLibrary(voice_code="male_a", speaker_id="Uncle_Fu", gender_presentation="male",
                     perceived_age="mature", timbre=["低沉"], personality_tags=["成熟"],
                     speaking_styles=[])
    p = {"gender_presentation": "male", "perceived_age": "mature",
         "timbre": ["低沉"], "personality": ["成熟"], "speaking_style": []}
    s = vm.score_voice(v, p)
    assert s["gender"] == 100 and s["age"] == 100 and s["timbre"] == 100
    p2 = {**p, "gender_presentation": "female"}
    s2 = vm.score_voice(v, p2)
    assert s2["gender"] == 0
    p3 = {"gender_presentation": "unknown", "perceived_age": "unknown",
          "timbre": [], "personality": [], "speaking_style": []}
    s3 = vm.score_voice(v, p3)
    assert s3["gender"] == 60 and s3["age"] == 50


def test_match_degrades_without_llm_and_respects_lock(env):
    client, tmp_path, Session = env
    db = Session()
    _seed_voices(db)
    pid = _seed_project(env)
    chars = _seed_characters(db, pid, ["小马", "小兔"])
    from app.models import VoiceProfile
    from app.models import ids
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    vp_lock = VoiceProfile(id=ids.voice_profile_id(), project_id=pid,
                           character_id=chars[1].id, voice_id="Vivian", voice_name="女性音色 F",
                           is_default=True, approved=True, is_locked=True, source="manual",
                           created_at=now, updated_at=now)
    db.add(vp_lock)
    db.commit()
    db.close()
    r = client.post(f"/api/projects/{pid}/voices/match", json={})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 1
    assert data["skipped"][0]["character_id"] == chars[1].id
    assert data["skipped"][0]["reason"] == "已锁定"
    m = data["matched"][0]
    assert m["character_id"] == chars[0].id
    assert m["match_score"] > 0
    assert m["speaker_id"] in ("Uncle_Fu", "Dylan", "Vivian")


def test_confirm_voice_releases_waiting_segments(env):
    client, tmp_path, Session = env
    db = Session()
    _seed_voices(db)
    pid = _seed_project(env)
    chars = _seed_characters(db, pid, ["小马"])
    from app.models import AudioSegment, VoiceProfile
    from app.models import ids
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    vp = VoiceProfile(id=ids.voice_profile_id(), project_id=pid,
                      character_id=chars[0].id, voice_id="Dylan", voice_name="男性音色 B",
                      is_default=True, approved=False, source="ai",
                      created_at=now, updated_at=now)
    db.add(vp)
    seg = AudioSegment(id=ids.audio_segment_id(), project_id=pid, episode=1,
                       track_type="dialogue", segment_type="character_dialogue",
                       character_id=chars[0].id, text="你好呀",
                       generation_status="waiting_voice_confirmation",
                       review_status="pending", version=1)
    db.add(seg)
    seg2 = AudioSegment(id=ids.audio_segment_id(), project_id=pid, episode=1,
                        track_type="dialogue", segment_type="character_dialogue",
                        character_id=chars[0].id, text="第二条",
                        generation_status="pending", review_status="pending", version=1)
    db.add(seg2)
    db.commit()
    db.close()
    r = client.post(f"/api/projects/{pid}/characters/{chars[0].id}/voice/confirm", json={})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["released_segments"] == 1
    db = Session()
    from sqlalchemy import select
    seg_after = db.get(AudioSegment, seg.id)
    assert seg_after.generation_status == "pending"
    seg2_after = db.get(AudioSegment, seg2.id)
    assert seg2_after.generation_status == "pending"
    vp_after = db.execute(select(VoiceProfile).where(VoiceProfile.character_id == chars[0].id)).scalars().first()
    assert vp_after.approved is True
    db.close()


def test_lock_unlock_and_binding_detail(env):
    client, tmp_path, Session = env
    db = Session()
    _seed_voices(db)
    pid = _seed_project(env)
    chars = _seed_characters(db, pid, ["老牛"])
    from app.models import VoiceProfile
    from app.models import ids
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    vp = VoiceProfile(id=ids.voice_profile_id(), project_id=pid,
                      character_id=chars[0].id, voice_id="Uncle_Fu", voice_name="男性音色 A",
                      is_default=True, approved=False, source="ai", match_score=88.5,
                      match_reason="测试原因", created_at=now, updated_at=now)
    db.add(vp)
    db.commit()
    cid = chars[0].id
    db.close()
    r = client.get(f"/api/projects/{pid}/characters/{cid}/voice/binding")
    assert r.status_code == 200
    b = r.json()["binding"]
    assert b["voice_id"] == "Uncle_Fu"
    assert b["match_score"] == 88.5
    assert b["status"] == "pending"
    r = client.post(f"/api/projects/{pid}/characters/{cid}/voice/lock")
    assert r.status_code == 200 and r.json()["is_locked"] is True
    r = client.post(f"/api/projects/{pid}/characters/{cid}/voice/unlock")
    assert r.status_code == 200 and r.json()["is_locked"] is False
    r = client.post(f"/api/projects/{pid}/characters/chr_none/voice/lock")
    assert r.status_code == 404
