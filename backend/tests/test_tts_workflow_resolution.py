"""TTS 工作流解析回归测试（2026-11-21）。

事故现场：资产库/角色卡片点「试听音色」报 422「无可用 TTS 工作流，请先在设置中导入」。

根因：`workflow.meta.json` 退场后，生产库 `workflow.capabilities` 是
`services/workflow/capabilities.normalize()` 的产物——`voice_mode` 被收进
`capabilities.audio.voice_mode`（值为 null），`voice_values` 根本不落库。
旧代码在 5 处按 `caps.get("voice_mode") == "preset" and caps.get("voice_values")`
判 preset 工作流，生产数据上恒为 False。

本测试按**生产库真实形态**造数（normalized capabilities + 空 voice_values +
VoiceLibrary 音色库），断言 speaker 列表与试听任务都能定位到 custom_voice 工作流。
"""
import json
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
    return TestClient(app), tmp_path, new_session


def _norm_caps(*, audio_input: bool, voice_mode=None) -> dict:
    """capabilities.normalize() 的真实产物形态（关键：无 voice_values）。"""
    return {
        "workflow_type": "tts",
        "generation_modes": ["text_to_speech"],
        "modalities": {
            "image_input": False, "audio_input": audio_input, "video_input": False,
            "image_output": False, "video_output": False, "audio_output": True,
        },
        "audio": {
            "accepts_tts_reference_audio": audio_input,
            "max_audio_inputs": 1 if audio_input else 0,
            "audio_output_mode": "separate",
            "supports_native_audio": False,
            "voice_mode": voice_mode,
        },
        "video": {},
        "reference": {"supports_multi_reference": False, "max_image_references": 0, "slots": []},
        "constraints": {"resolution": None, "required_params": []},
        "source": {"tier": None, "local_only": None, "cost": None, "schema_errors": []},
    }


def _schema(params: dict) -> dict:
    return {"schema_version": "1.0", "parser_version": "1.0", "params": params,
            "outputs": [{"key": "voice", "label": "生成配音", "media_type": "audio"}],
            "errors": [], "warnings": []}


def _seed_live_shape(env, *, clone_only: bool = False):
    """按生产库形态插入 tts 工作流 + VoiceLibrary 音色库。"""
    from app.models.audio import VoiceLibrary
    from app.models.production import Workflow
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = env[2]()
    # 克隆工作流更早创建（旧逻辑按 created_at 取第一条会不会误取，本测试同时验证）
    db.add(Workflow(
        id="wf_clone_live", name="wf_tts_qwen3_voice_clone", workflow_type="tts",
        definition={"2": {"class_type": "Qwen3TTSVoiceClone"}},
        capabilities=_norm_caps(audio_input=True),
        parsed_schema=_schema({
            "tts_voice_ref": {"key": "tts_voice_ref", "type": "audio", "semantic": "tts_voice_ref"},
            "tts_text": {"key": "tts_text", "type": "textarea", "semantic": "tts_text"},
        }),
        created_at=now - timedelta(hours=1),
    ))
    if not clone_only:
        db.add(Workflow(
            id="wf_custom_live", name="wf_tts_qwen3_custom_voice", workflow_type="tts",
            definition={"2": {"class_type": "Qwen3TTSCustomVoice"}},
            capabilities=_norm_caps(audio_input=False),
            parsed_schema=_schema({
                "tts_text": {"key": "tts_text", "type": "textarea", "semantic": "tts_text"},
                "tts_voice": {"key": "tts_voice", "type": "string", "semantic": "tts_voice",
                              "default": "Vivian"},
                "tts_lang": {"key": "tts_lang", "type": "string", "semantic": "tts_lang"},
            }),
            created_at=now,
        ))
        db.add_all([
            VoiceLibrary(id="vl_test_vivian", voice_code="female_f", name="女性音色 F",
                         speaker_id="Vivian", display_name="薇薇安", gender_presentation="female",
                         perceived_age="young", pitch="unknown", language="中文",
                         description="女声 · 中文", source_workflow="wf_tts_qwen3_custom_voice",
                         enabled=True, sort_order=6),
            VoiceLibrary(id="vl_test_dylan", voice_code="male_b", name="男性音色 B",
                         speaker_id="Dylan", display_name="迪伦", gender_presentation="male",
                         perceived_age="young", pitch="unknown", language="中文（京腔）",
                         description="男声 · 中文（京腔）", source_workflow="wf_tts_qwen3_custom_voice",
                         enabled=True, sort_order=2),
        ])
    db.commit()
    db.close()


def _seed_voice_profile(env, voice_id="Dylan"):
    """项目 + 角色 + 音色配置（试听接口的输入）。"""
    client = env[0]
    pid = client.post("/api/projects", json={"name": "试听回归", "aspect_ratio": "16:9"}).json()["id"]
    ch = client.post("/api/characters", json={"project_id": pid, "name": "放羊娃"}).json()
    from app.models.audio import VoiceProfile
    from app.models import ids
    db = env[2]()
    vp = VoiceProfile(
        id=ids.voice_profile_id(), project_id=pid, character_id=ch["id"],
        voice_provider="qwen3_tts", voice_model="Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        voice_id=voice_id, voice_name="男性音色 D", speed=1.0, default_emotion="neutral",
        mode="custom_voice", language="Chinese", enabled=True, approved=True,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(vp)
    db.commit()
    vp_id = vp.id
    db.close()
    return pid, ch["id"], vp_id


# ============ 唯一权威解析 ============

def test_resolve_preset_workflow_on_normalized_capabilities(env):
    _seed_live_shape(env)
    from app.services import tts_workflow
    db = env[2]()
    wf = tts_workflow.resolve_preset_workflow(db)
    assert wf is not None, "normalized capabilities 下必须能定位 preset TTS 工作流"
    assert wf.id == "wf_custom_live"  # 克隆工作流更早创建也不能被选中
    assert tts_workflow.workflow_speakers(db, wf) == ["Dylan", "Vivian"]  # 按 sort_order
    db.close()


def test_resolve_preset_workflow_none_when_only_clone(env):
    _seed_live_shape(env, clone_only=True)
    from app.services import tts_workflow
    db = env[2]()
    assert tts_workflow.resolve_preset_workflow(db) is None
    db.close()


def test_executor_resolves_same_workflow(env):
    _seed_live_shape(env)
    from app.worker.executor import _resolve_tts_workflow
    db = env[2]()
    assert _resolve_tts_workflow(db, None).id == "wf_custom_live"
    db.close()


# ============ 接口：speakers / 试听 ============

def test_speakers_endpoint_returns_library_speakers(env):
    _seed_live_shape(env)
    r = env[0].get("/api/audio/speakers")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["workflow_id"] == "wf_custom_live"
    by_id = {s["speaker"]: s for s in data["speakers"]}
    assert set(by_id) == {"Vivian", "Dylan"}
    assert by_id["Dylan"]["display_name"] == "迪伦"
    assert by_id["Dylan"]["language"] == ["中文（京腔）"]
    assert by_id["Dylan"]["enabled"] is True


def test_voice_profile_preview_binds_preset_workflow(env):
    """角色音色试听（资产库卡片上的「试听」）：不再 422，且任务绑定 custom_voice。"""
    _seed_live_shape(env)
    _, _, vp_id = _seed_voice_profile(env, voice_id="Dylan")
    r = env[0].post(f"/api/audio/voices/{vp_id}/preview", json={"text": "你好"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["cached"] is False and body["job_id"]
    from app.models.production import Job
    db = env[2]()
    job = db.get(Job, body["job_id"])
    assert job.workflow_id == "wf_custom_live"
    assert job.params["tts_voice"] == "Dylan"
    db.close()


def test_system_voice_preview_binds_preset_workflow(env):
    _seed_live_shape(env)
    pid = env[0].post("/api/projects", json={"name": "系统音色试听", "aspect_ratio": "16:9"}).json()["id"]
    r = env[0].post("/api/voices/system/male_b/preview", json={"project_id": pid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cached"] is False and body["job_id"]
    from app.models.production import Job
    db = env[2]()
    job = db.get(Job, body["job_id"])
    assert job.workflow_id == "wf_custom_live"
    assert job.params["tts_voice"] == "Dylan"
    db.close()


def test_preview_still_rejects_when_no_preset_workflow(env):
    """无 preset 工作流时仍明确报错，不静默拿克隆工作流顶替。"""
    _seed_live_shape(env, clone_only=True)
    _, _, vp_id = _seed_voice_profile(env)
    r = env[0].post(f"/api/audio/voices/{vp_id}/preview", json={"text": "你好"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "no_matching_workflow"


# ============ 排队体验：试听插队 + 连点去重 ============

def test_preview_job_is_prioritized(env):
    """试听是秒级短任务，priority=50，不能排在长视频/关键帧后面。"""
    _seed_live_shape(env)
    _, _, vp_id = _seed_voice_profile(env, voice_id="Dylan")
    r = env[0].post(f"/api/audio/voices/{vp_id}/preview", json={"text": "你好"})
    assert r.status_code == 202, r.text
    from app.models.production import Job
    db = env[2]()
    job = db.get(Job, r.json()["job_id"])
    assert job.priority == 50
    db.close()


def test_preview_dedupe_reuses_inflight_job(env):
    """连点「试听/重新生成」不应堆出多条排队任务：同音色在途任务直接复用。"""
    _seed_live_shape(env)
    _, _, vp_id = _seed_voice_profile(env, voice_id="Dylan")
    r1 = env[0].post(f"/api/audio/voices/{vp_id}/preview", json={"text": "你好"})
    r2 = env[0].post(f"/api/audio/voices/{vp_id}/preview", json={"text": "你好", "force": True})
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["job_id"] == r2.json()["job_id"]
    assert r2.json().get("reused") is True
    from app.models.production import Job
    db = env[2]()
    count = db.query(Job).filter(Job.job_type == "generate_audio").count()
    assert count == 1, f"在途试听应复用，实际建了 {count} 条任务"
    db.close()


def test_system_voice_preview_dedupe(env):
    _seed_live_shape(env)
    pid = env[0].post("/api/projects", json={"name": "系统音色去重", "aspect_ratio": "16:9"}).json()["id"]
    r1 = env[0].post("/api/voices/system/male_b/preview", json={"project_id": pid})
    r2 = env[0].post("/api/voices/system/male_b/preview", json={"project_id": pid})
    assert r1.status_code == 200 and r2.status_code == 200, r2.text
    assert r1.json()["job_id"] == r2.json()["job_id"]
    from app.models.production import Job
    db = env[2]()
    assert db.query(Job).filter(Job.job_type == "generate_audio").count() == 1
    db.close()
