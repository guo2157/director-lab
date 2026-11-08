"""V1 音频解耦方案增量改造测试：P1/P2 修复、项目级音频策略、tts_mix 时间轴与混音命令。

- P1：list_speakers 固定取 voice_mode=preset 的 custom_voice workflow（voice_clone 更早也不取）
- P2：regenerate_segment 按 audio_segment_id 去重（同镜头多片段不互相阻塞，同片段重复提交 409）
- audio-config：项目级音频策略读写（native_audio_policy 默认 native，非法值 422）
- tts_mix：对白时长为音画时长权威依据（超长延长镜头，不截断）
- shot_segment_cmd：tts_mix 混音命令（adelay 定位 + amix 求和，不映射内嵌音轨）；discard 丢内嵌音轨补静音
"""
import subprocess
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


def _seed_project(env):
    client = env[0]
    pid = client.post("/api/projects", json={"name": "音频策略测试", "aspect_ratio": "16:9"}).json()["id"]
    return pid


def _seed_tts_workflows(env):
    """插入两条 tts workflow：voice_clone 更早（created_at 在前），custom_voice 在后。"""
    from app.models.production import Workflow
    db = env[2]()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(Workflow(id="wf_clone_test", name="wf_tts_qwen3_voice_clone", workflow_type="tts",
                    definition={}, capabilities={"voice_mode": "clone_reference_audio"},
                    created_at=now - timedelta(hours=1)))
    db.add(Workflow(id="wf_custom_test", name="wf_tts_qwen3_custom_voice", workflow_type="tts",
                    definition={}, capabilities={"voice_mode": "preset", "voice_values": ["Vivian", "Serena"]},
                    created_at=now))
    db.commit()
    db.close()


# ============ P1：speakers 固定取 custom_voice ============

def test_speakers_prefers_custom_voice(env):
    _seed_tts_workflows(env)
    r = env[0].get("/api/audio/speakers")
    assert r.status_code == 200
    data = r.json()
    # voice_clone 更早也不取，固定取 voice_mode=preset 的 custom_voice
    assert data["workflow_id"] == "wf_custom_test"
    speakers = [s["speaker"] for s in data["speakers"]]
    assert "Vivian" in speakers and "Serena" in speakers


# ============ P2：regenerate 按片段去重 ============

def test_regenerate_dedup_by_segment(env):
    client, tmp_path, Session = env
    _seed_tts_workflows(env)  # regenerate 现在提交时即绑定 workflow（fail-fast 校验），需预置种子
    pid = _seed_project(env)
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "教室", "episode": 1}).json()
    shot = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "dialogue",
        "duration": 2, "image_prompt": "测试", "dialogue": "你好",
    }).json()
    from app.models.audio import AudioSegment
    from app.models import ids
    db = Session()
    seg1 = AudioSegment(id=ids.audio_segment_id(), project_id=pid, episode=1, shot_id=shot["id"],
                        track_type="dialogue", segment_type="character_dialogue", text="第一句",
                        generation_status="pending", review_status="pending", version=1)
    seg2 = AudioSegment(id=ids.audio_segment_id(), project_id=pid, episode=1, shot_id=shot["id"],
                        track_type="dialogue", segment_type="character_dialogue", text="第二句",
                        generation_status="pending", review_status="pending", version=1)
    db.add_all([seg1, seg2])
    db.commit()
    db.close()
    # 片段 1 生成任务在途
    r1 = client.post(f"/api/audio/segments/{seg1.id}/regenerate", json={})
    assert r1.status_code == 202, r1.text
    # P2 修复验证：同镜头片段 2 不应被片段 1 阻塞（旧逻辑按 shot+job_type 误判 409）
    r2 = client.post(f"/api/audio/segments/{seg2.id}/regenerate", json={})
    assert r2.status_code == 202, r2.text
    # 同片段重复提交应 409
    r3 = client.post(f"/api/audio/segments/{seg1.id}/regenerate", json={})
    assert r3.status_code == 409


# ============ 项目级音频策略 ============

def test_audio_config_default_and_update(env):
    client = env[0]
    pid = _seed_project(env)
    # 默认 native（H3 内嵌音轨，TTS 仅作 ref_audio 参考）
    r = client.get(f"/api/audio/projects/{pid}/audio-config")
    assert r.status_code == 200
    cfg = r.json()["audio_config"]
    assert cfg["native_audio_policy"] == "native"
    assert cfg["tts_enabled"] is True
    # 更新为 preserve
    r = client.put(f"/api/audio/projects/{pid}/audio-config",
                   json={"audio_config": {"native_audio_policy": "preserve"}})
    assert r.status_code == 200
    assert r.json()["audio_config"]["native_audio_policy"] == "preserve"
    # 持久化生效
    r = client.get(f"/api/audio/projects/{pid}/audio-config")
    assert r.json()["audio_config"]["native_audio_policy"] == "preserve"
    # 非法值 422
    r = client.put(f"/api/audio/projects/{pid}/audio-config",
                   json={"audio_config": {"native_audio_policy": "bogus"}})
    assert r.status_code == 422


# ============ tts_mix 时间轴：对白时长权威依据 ============

def _make_plain_video(path, seconds=2):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=blue:s=320x240:d={seconds}",
                    "-c:v", "libx264", str(path)], check=True, capture_output=True)


def test_build_timeline_tts_mix_extends_shot(tmp_path):
    from app.services.compose import ShotInput, build_timeline
    video = tmp_path / "v.mp4"
    _make_plain_video(video, seconds=2)
    s = ShotInput(shot_id="s1", episode=1, seq=1, scene_seq=1, duration=2,
                  actual_duration=None, audio_source="tts_mix", status="done",
                  dialogue="你好", video_path=str(video), video_asset_id=None,
                  video_asset_version=None, audio_path=None, audio_asset_id=None,
                  audio_asset_version=None, review_released=True,
                  tts_audios=[{"segment_id": "a1", "track_type": "dialogue",
                               "path": "/tmp/a1.wav", "offset_ms": 0, "duration_ms": 3500}])
    result = build_timeline([s], tolerance=0.5)
    assert not result.errors
    assert len(result.items) == 1
    # 对白 3.5s 超计划 2s：目标时长延长到 3.5s（不截断不加速）
    assert result.items[0].target_duration == pytest.approx(3.5)
    assert result.total_duration == pytest.approx(3.5)
    assert any("延长镜头" in w for w in result.warnings)


# ============ shot_segment_cmd：tts_mix 混音命令 ============

def _tts_mix_shot():
    from app.services.compose import ShotInput
    return ShotInput(shot_id="s1", episode=1, seq=1, scene_seq=1, duration=2,
                     actual_duration=None, audio_source="tts_mix", status="done",
                     dialogue="你好", video_path="/tmp/v.mp4", video_asset_id=None,
                     video_asset_version=None, audio_path=None, audio_asset_id=None,
                     audio_asset_version=None,
                     tts_audios=[
                         {"segment_id": "a1", "track_type": "dialogue", "path": "/tmp/a1.wav",
                          "offset_ms": 0, "duration_ms": 1500},
                         {"segment_id": "a2", "track_type": "dialogue", "path": "/tmp/a2.wav",
                          "offset_ms": 1500, "duration_ms": 2000},
                     ])


def test_shot_segment_cmd_tts_mix():
    from app.services.compose import TimelineItem, shot_segment_cmd
    s = _tts_mix_shot()
    item = TimelineItem(shot=s, target_duration=3.5, start_time=0.0, align_mode="pad", video_duration=2.0)
    cmd = shot_segment_cmd(item, "/tmp/out.mp4", width=1280, height=720)
    cmd_str = " ".join(cmd)
    # 两个 TTS 输入文件
    assert "/tmp/a1.wav" in cmd_str and "/tmp/a2.wav" in cmd_str
    # adelay 定位 + amix 求和
    assert "adelay=0" in cmd_str and "adelay=1500" in cmd_str
    assert "amix=inputs=2" in cmd_str
    # 不映射视频内嵌音轨（discard：丢弃 H3 原生音轨）
    assert "[0:a]" not in cmd_str


def test_shot_segment_cmd_drop_native_audio():
    from app.services.compose import ShotInput, TimelineItem, shot_segment_cmd
    s = ShotInput(shot_id="s1", episode=1, seq=1, scene_seq=1, duration=2,
                  actual_duration=2.0, audio_source="h3_va", status="done",
                  dialogue=None, video_path="/tmp/v.mp4", video_asset_id=None,
                  video_asset_version=None, audio_path=None, audio_asset_id=None,
                  audio_asset_version=None, drop_native_audio=True)
    item = TimelineItem(shot=s, target_duration=2.0, start_time=0.0, align_mode="none", video_duration=2.0)
    cmd = shot_segment_cmd(item, "/tmp/out.mp4", width=1280, height=720)
    cmd_str = " ".join(cmd)
    # discard 策略无 TTS 片段：丢弃内嵌音轨，补静音
    assert "anullsrc" in cmd_str
    assert "[0:a]" not in cmd_str
