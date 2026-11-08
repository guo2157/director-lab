"""音频规划音色确认闸口测试（2026-11-07）。

覆盖：
- 角色音色已确认 → 该角色对白片段落 pending
- 角色音色未确认/不存在 → 该角色对白片段落 waiting_voice_confirmation
- 旁白（无角色）不受闸口限制 → pending
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import make_engine
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
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    import app.core.config as cfg_mod
    monkeypatch.setenv("DLAB_MEDIA_DIR", str(media_dir))
    cfg_mod.clear_settings_cache()
    client = TestClient(app)
    return client, tmp_path, new_session


def _setup_project_with_shots(env, approved_char: bool):
    """构造项目 + 场景 + 两个角色的对白镜头 + 一个旁白镜头。

    小马：approved_char=True 时预置已确认默认音色。
    小兔：始终无音色绑定。
    """
    client, _, Session = env
    pid = client.post("/api/projects", json={"name": "闸口测试", "aspect_ratio": "16:9"}).json()["id"]
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "教室", "episode": 1}).json()
    ch1 = client.post("/api/characters", json={"project_id": pid, "name": "小马"}).json()
    ch2 = client.post("/api/characters", json={"project_id": pid, "name": "小兔"}).json()
    shot1 = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "dialogue",
        "duration": 2, "image_prompt": "a", "dialogue": "你好",
        "character_id": ch1["id"],
    }).json()
    # dialogue_segments 由剧本解析流程写入（ShotCreate 契约不含该字段），测试按真实数据流直写
    from app.models import Shot as ShotModel
    db0 = Session()
    _s1 = db0.get(ShotModel, shot1["id"])
    _s1.dialogue_segments = [
        {"speaker": "小马", "text": "你好呀", "order": 1},
        {"speaker": "小兔", "text": "早上好", "order": 2},
    ]
    db0.commit()
    db0.close()
    shot2 = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "narration",
        "duration": 2, "image_prompt": "b", "dialogue": "旁白文本",
    }).json()
    if approved_char:
        from app.models import VoiceProfile
        from app.models import ids
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db = Session()
        db.add(VoiceProfile(
            id=ids.voice_profile_id(), project_id=pid, character_id=ch1["id"],
            voice_id="Dylan", voice_name="男性音色 B", is_default=True,
            approved=True, created_at=now, updated_at=now,
        ))
        db.commit()
        db.close()
    return pid, ch1["id"], ch2["id"], shot1["id"], shot2["id"]


def test_plan_gate_waiting_and_released(env):
    client, _, Session = env
    pid, ch1, ch2, _, _ = _setup_project_with_shots(env, approved_char=True)
    r = client.post(f"/api/audio/projects/{pid}/episodes/1/plan")
    assert r.status_code == 200, r.text
    plan = client.get(f"/api/audio/projects/{pid}/episodes/1/plan").json()
    dialogue = plan["tracks"].get("dialogue") or []
    by_char = {d["character_id"]: d for d in dialogue}
    # 小马音色已确认 → pending；小兔未确认 → waiting_voice_confirmation
    assert by_char[ch1]["generation_status"] == "pending"
    assert by_char[ch2]["generation_status"] == "waiting_voice_confirmation"
    # 旁白不受闸口限制
    narration = plan["tracks"].get("narration") or []
    assert all(n["generation_status"] == "pending" for n in narration)
    # 确认小兔音色后放行其等待中的对白
    from app.models import AudioSegment, VoiceProfile
    from app.models import ids
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = Session()
    db.add(VoiceProfile(
        id=ids.voice_profile_id(), project_id=pid, character_id=ch2,
        voice_id="Vivian", voice_name="女性音色 F", is_default=True,
        approved=False, source="ai", created_at=now, updated_at=now,
    ))
    db.commit()
    db.close()
    r = client.post(f"/api/projects/{pid}/characters/{ch2}/voice/confirm", json={})
    assert r.status_code == 200, r.text
    assert r.json()["released_segments"] == 1
    db = Session()
    seg = db.query(AudioSegment).filter(
        AudioSegment.character_id == ch2, AudioSegment.track_type == "dialogue"
    ).first()
    assert seg.generation_status == "pending"
    db.close()


def test_plan_progress_counts_waiting(env):
    client, _, Session = env
    pid, _, _, _, _ = _setup_project_with_shots(env, approved_char=False)
    client.post(f"/api/audio/projects/{pid}/episodes/1/plan")
    r = client.get(f"/api/audio/projects/{pid}/audio/progress")
    assert r.status_code == 200
    dlg = r.json()["progress"]["dialogue"]
    assert dlg["total"] == 2
    assert dlg.get("waiting_voice_confirmation") == 2
    # 明细接口支持按状态过滤
    r2 = client.get(
        f"/api/audio/projects/{pid}/audio/segments",
        params={"generation_status": "waiting_voice_confirmation"},
    )
    assert r2.status_code == 200
    assert r2.json()["total"] == 2
