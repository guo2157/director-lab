"""A4：TTS 复用依赖指纹（v2 §13.5）。

用真实 SQLite + 真实 ffmpeg 音频驱动 `_shot_ref_audios`，不用 mock：

- 生成侧与复用侧参数同源（`services/tts_params`）→ 指纹能真正匹配
- 台词 / 音色 / TTS 工作流版本 / 内容参数任一变更 → 判定 stale 且指出变了什么
- 历史片段（无指纹）走宽限期：可复用但 `legacy=True`，调用方必须告警
- 槽位超限、未审核、文件丢失等既有约束不被指纹改动破坏
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Asset, Base, ids
from app.models.audio import AudioSegment, VoiceProfile
from app.models.narrative import Character, Project
from app.models.production import Workflow
from app.services import tts_params
from app.services.workflow import tts_fingerprint as fp
from app.worker import executor

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="需要 ffmpeg 生成真实音频"
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_audio(path, *, duration=1.0, rate=24000):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={duration}:sample_rate={rate}",
         "-ac", "1", "-c:a", "libmp3lame", str(path)],
        check=True, capture_output=True, timeout=60,
    )
    return path


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'a4.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture()
def scene_ctx(db, tmp_path):
    """一个项目 + 一个角色 + 默认音色 + preset TTS 工作流 + 一个镜头。"""
    media = tmp_path / "media"
    media.mkdir()

    project = Project(
        id=ids.project_id(), name="A4 测试项目", aspect_ratio="9:16",
        creation_type="script", status="active",
        created_at=_now(), updated_at=_now(),
    )
    db.add(project)
    db.flush()

    char = Character(
        id=ids.character_id(), project_id=project.id, name="林舟",
        created_at=_now(), updated_at=_now(),
    )
    db.add(char)
    db.flush()

    voice = VoiceProfile(
        id=ids.voice_profile_id(), project_id=project.id, character_id=char.id,
        voice_id="Cherry", voice_name="Cherry", language="Chinese",
        default_instruct="自然、清晰", speed=1.0, default_emotion="neutral",
        is_default=True, enabled=True, approved=True, created_at=_now(),
    )
    db.add(voice)

    wf = Workflow(
        id=ids.workflow_id(), name="TTS 预设音色", workflow_type="tts",
        version=1, is_default=True,
        capabilities={"voice_mode": "preset", "voice_values": ["Cherry"]},
        definition={"1": {"class_type": "X", "inputs": {}}},
        created_at=_now(), updated_at=_now(),
    )
    db.add(wf)
    db.flush()

    from app.models.narrative import Scene, Shot

    scene = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=1, title="屋顶")
    db.add(scene)
    db.flush()
    shot = Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1,
                shot_kind="normal", duration=3)
    db.add(shot)
    db.commit()

    return {
        "project": project, "char": char, "voice": voice, "wf": wf,
        "shot": shot, "media": media,
    }


def _add_segment(db, ctx, *, text="你终于来了。", fingerprint=None, approved=True,
                 audio=True, name="seg", speed=None, emotion=None):
    """建一个已完成 + 已审核的配音片段（真实音频文件）。"""
    seg = AudioSegment(
        id=ids.audio_segment_id(), project_id=ctx["project"].id,
        episode=1, shot_id=ctx["shot"].id, character_id=ctx["char"].id,
        track_type="dialogue", sequence=1, text=text,
        generation_status="completed",
        review_status="approved" if approved else "pending",
        speed=speed, emotion=emotion, created_at=_now(),
    )
    db.add(seg)
    db.flush()
    if audio:
        path = _make_audio(ctx["media"] / f"{name}.mp3")
        asset = Asset(
            id=ids.asset_id(), project_id=ctx["project"].id,
            asset_type="audio", name=name, version=1, validity="current",
            file_path=str(path), updated_at=_now(),
        )
        db.add(asset)
        db.flush()
        seg.audio_asset_id = asset.id
        seg.duration_ms = 1000
    if fingerprint is not None:
        seg.tts_fingerprint = fingerprint
    db.commit()
    return seg


def _current_fingerprint(db, ctx, seg):
    """按生成侧同源逻辑算指纹（模拟「生成时写下的那一份」）。"""
    voice = tts_params.resolve_voice(db, seg)
    return fp.compute(
        text=seg.text,
        voice_id=voice.voice_id if voice else None,
        tts_workflow_id=ctx["wf"].id,
        tts_workflow_version=ctx["wf"].version,
        tts_params=tts_params.params_for_fingerprint(seg, voice),
    )


# --- 参数同源：这是指纹能否工作的前提 -------------------------------------

def test_build_params_matches_fingerprint_params_except_text(db, scene_ctx):
    """params_for_fingerprint 必须等于 build_params 去掉 tts_text —— 否则两侧恒失配。"""
    seg = _add_segment(db, scene_ctx)
    voice = tts_params.resolve_voice(db, seg)
    full = tts_params.build_params(seg, voice)
    fp_params = tts_params.params_for_fingerprint(seg, voice)
    assert full.pop("tts_text") == seg.text
    assert full == fp_params


def test_generate_and_reuse_paths_produce_identical_fingerprint(db, scene_ctx):
    """同一片段：生成侧算的指纹 == 复用侧重算的指纹（端到端同源验证）。"""
    seg = _add_segment(db, scene_ctx)
    stored = _current_fingerprint(db, scene_ctx, seg)
    seg.tts_fingerprint = stored
    db.commit()

    result = executor._shot_ref_audios(db, scene_ctx["shot"])
    assert result["stale"] == []
    assert result["legacy"] == []
    assert len(result["paths"]) == 1


def test_language_and_default_instruct_are_covered(db, scene_ctx):
    """非中文音色 + 默认 instruct：旧实现会因硬编码 Chinese 而恒失配。"""
    scene_ctx["voice"].language = "English"
    scene_ctx["voice"].default_instruct = "低沉、克制"
    db.commit()
    seg = _add_segment(db, scene_ctx)
    seg.tts_fingerprint = _current_fingerprint(db, scene_ctx, seg)
    db.commit()

    result = executor._shot_ref_audios(db, scene_ctx["shot"])
    assert result["stale"] == [], result["stale"]


def test_speed_is_covered(db, scene_ctx):
    """片段设过 speed：旧实现生成侧不写 speed，必然失配。"""
    seg = _add_segment(db, scene_ctx, speed=1.2)
    seg.tts_fingerprint = _current_fingerprint(db, scene_ctx, seg)
    db.commit()
    assert executor._shot_ref_audios(db, scene_ctx["shot"])["stale"] == []


# --- 失效判定 -------------------------------------------------------------

def test_text_change_marks_stale_with_reason(db, scene_ctx):
    seg = _add_segment(db, scene_ctx, text="你终于来了。")
    seg.tts_fingerprint = _current_fingerprint(db, scene_ctx, seg)
    db.commit()

    seg.text = "你怎么才来。"
    db.commit()

    result = executor._shot_ref_audios(db, scene_ctx["shot"])
    assert result["paths"] == []
    assert len(result["stale"]) == 1
    assert result["stale"][0]["changed"] == ["text"]
    assert "台词" in result["stale"][0]["reason"]


def test_voice_change_marks_stale(db, scene_ctx):
    seg = _add_segment(db, scene_ctx)
    seg.tts_fingerprint = _current_fingerprint(db, scene_ctx, seg)
    db.commit()

    scene_ctx["voice"].voice_id = "Ethan"
    db.commit()

    result = executor._shot_ref_audios(db, scene_ctx["shot"])
    assert result["paths"] == []
    assert "voice_id" in result["stale"][0]["changed"]


def test_tts_workflow_version_change_marks_stale(db, scene_ctx):
    seg = _add_segment(db, scene_ctx)
    seg.tts_fingerprint = _current_fingerprint(db, scene_ctx, seg)
    db.commit()

    scene_ctx["wf"].version = 2
    db.commit()

    result = executor._shot_ref_audios(db, scene_ctx["shot"])
    assert result["paths"] == []
    assert "tts_workflow_version" in result["stale"][0]["changed"]


def test_emotion_change_marks_stale_when_voice_has_no_default_instruct(db, scene_ctx):
    """片段情绪是影响音频内容的参数：音色未配 default_instruct 时必须被指纹覆盖。"""
    scene_ctx["voice"].default_instruct = ""
    db.commit()
    seg = _add_segment(db, scene_ctx, emotion="angry")
    seg.tts_fingerprint = _current_fingerprint(db, scene_ctx, seg)
    db.commit()

    seg.emotion = "sad"
    db.commit()

    result = executor._shot_ref_audios(db, scene_ctx["shot"])
    assert result["paths"] == []
    assert "tts_params" in result["stale"][0]["changed"]


def test_voice_default_instruct_overrides_segment_emotion(db, scene_ctx):
    """既有生成语义：音色配了 default_instruct 且片段无 instruct 时，以音色默认为准。

    此处只锁定「指纹与生成侧一致」这一事实：既然生成时用的是音色默认值，
    片段 emotion 改动就不该让指纹失配（否则会误报 stale、白跑一次 TTS）。
    """
    seg = _add_segment(db, scene_ctx, emotion="angry")
    seg.tts_fingerprint = _current_fingerprint(db, scene_ctx, seg)
    db.commit()

    seg.emotion = "sad"
    db.commit()

    assert executor._shot_ref_audios(db, scene_ctx["shot"])["stale"] == []


def test_instruct_change_marks_stale(db, scene_ctx):
    seg = _add_segment(db, scene_ctx)
    seg.tts_fingerprint = _current_fingerprint(db, scene_ctx, seg)
    db.commit()

    seg.instruct = "压低嗓音、带威胁感"
    db.commit()

    result = executor._shot_ref_audios(db, scene_ctx["shot"])
    assert result["paths"] == []
    assert "tts_params" in result["stale"][0]["changed"]


def test_whitespace_only_change_is_not_stale(db, scene_ctx):
    """空白差异不该触发重生成（归一化后指纹一致）。"""
    seg = _add_segment(db, scene_ctx, text="你终于来了。")
    seg.tts_fingerprint = _current_fingerprint(db, scene_ctx, seg)
    db.commit()

    seg.text = "  你终于来了。\n"
    db.commit()

    assert executor._shot_ref_audios(db, scene_ctx["shot"])["stale"] == []


# --- 宽限期（历史数据） ---------------------------------------------------

def test_legacy_segment_without_fingerprint_reuses_with_warning(db, scene_ctx):
    """A4 上线前的片段：无指纹 → 宽限期复用，但必须标记 legacy 供调用方告警。"""
    _add_segment(db, scene_ctx, fingerprint=None)

    result = executor._shot_ref_audios(db, scene_ctx["shot"])
    assert len(result["paths"]) == 1
    assert result["stale"] == []
    assert len(result["legacy"]) == 1
    assert "指纹" in result["legacy"][0]["reason"]


def test_strict_mode_rejects_legacy_segment():
    """严格模式（legacy_grace=False）：无指纹一律不可复用。"""
    d = fp.check_reuse(
        stored=None, text="台词", voice_id="Cherry",
        tts_workflow_id="wf1", tts_workflow_version=1,
        tts_params={"tts_lang": "Chinese"}, legacy_grace=False,
    )
    assert d.reusable is False
    assert d.legacy is False


def test_legacy_grace_respects_approval(db, scene_ctx):
    """宽限期只放宽「指纹缺失」，不放宽审核：未审核片段在查询层就被排除，不会注入。"""
    _add_segment(db, scene_ctx, fingerprint=None, approved=False)

    result = executor._shot_ref_audios(db, scene_ctx["shot"])
    assert result["paths"] == []
    # 未审核片段被 SQL 过滤，不进 stale 列表（它从未进入复用候选集）
    assert result["stale"] == []
    assert result["legacy"] == []


def test_check_reuse_rejects_unapproved_even_in_grace():
    """宽限期的判断顺序：审核在指纹之前，缺指纹不能绕过审核。"""
    d = fp.check_reuse(
        stored=None, text="台词", voice_id="Cherry",
        tts_workflow_id="wf1", tts_workflow_version=1,
        approved=False, legacy_grace=True,
    )
    assert d.reusable is False
    assert d.legacy is False
    assert "未审核" in d.reason


def test_legacy_grace_respects_missing_file(db, scene_ctx):
    """音频文件被清理：宽限期也不能复用。"""
    seg = _add_segment(db, scene_ctx, fingerprint=None)
    asset = db.get(Asset, seg.audio_asset_id)
    asset.file_path = ""
    db.commit()

    result = executor._shot_ref_audios(db, scene_ctx["shot"])
    assert result["paths"] == []


# --- 指纹本身 -------------------------------------------------------------

def test_compute_is_deterministic_and_param_order_independent():
    kw = dict(text="同一句台词", voice_id="Cherry", tts_workflow_id="wf1",
              tts_workflow_version=3)
    a = fp.compute(tts_params={"tts_lang": "Chinese", "tts_emotion": "自然"}, **kw)
    b = fp.compute(tts_params={"tts_emotion": "自然", "tts_lang": "Chinese"}, **kw)
    assert a["fingerprint"] == b["fingerprint"]


def test_fingerprint_ignores_unrelated_params():
    """只取声明的内容参数，review_status 之类混进来不应改变指纹。"""
    base = dict(text="t", voice_id="v", tts_workflow_id="w", tts_workflow_version=1)
    a = fp.compute(tts_params={"tts_lang": "Chinese", "review_status": "approved"}, **base)
    b = fp.compute(tts_params={"tts_lang": "Chinese", "review_status": "pending"}, **base)
    assert a["fingerprint"] == b["fingerprint"]


def test_fingerprint_version_bump_invalidates():
    """算法版本变化 → 旧指纹失效（避免旧算法结果被当有效）。"""
    stored = fp.compute(text="t", voice_id="v", tts_workflow_id="w", tts_workflow_version=1)
    stored = dict(stored, version="0.9")
    d = fp.check_reuse(
        stored=stored, text="t", voice_id="v", tts_workflow_id="w",
        tts_workflow_version=1, legacy_grace=False,
    )
    assert d.reusable is False
    assert "fingerprint_version" in d.changed


# --- 事件落库：调用方必须消费 stale / legacy，不能静默 ----------------------

def _make_job(db, ctx):
    """建一个真实的 clip 任务（事件写入需要 job 存在）。"""
    from app.models.production import Job
    job = Job(
        id=ids.job_id(), project_id=ctx["project"].id, shot_id=ctx["shot"].id,
        job_type="clip", status="running", workflow_id=ctx["wf"].id,
        params={}, input_snapshot={}, created_at=_now(),
    )
    db.add(job)
    db.commit()
    return job


def _events(db, job):
    from app.models.production import JobEvent
    return (
        db.query(JobEvent)
        .filter(JobEvent.job_id == job.id)
        .order_by(JobEvent.event_seq)
        .all()
    )


def test_stale_writes_audio_tts_stale_event(db, scene_ctx):
    """指纹失配时必须写 job.audio_tts_stale 事件，把「为什么不能用旧音频」落库。"""
    seg = _add_segment(db, scene_ctx, text="你终于来了。")
    seg.tts_fingerprint = _current_fingerprint(db, scene_ctx, seg)
    db.commit()
    seg.text = "你怎么才来。"
    db.commit()

    job = _make_job(db, scene_ctx)
    result = executor._shot_ref_audios(db, scene_ctx["shot"])
    # 模拟 executor 里的消费逻辑
    if result["stale"]:
        executor._write_event(db, job, "job.audio_tts_stale", {"stale": result["stale"]})
        db.commit()

    evs = _events(db, job)
    stale_ev = [e for e in evs if e.event_type == "job.audio_tts_stale"]
    assert len(stale_ev) == 1
    payload = stale_ev[0].payload
    assert payload["stale"][0]["changed"] == ["text"]
    assert "台词" in payload["stale"][0]["reason"]
    # event_type 必须能装进 String(32)，换 MySQL 不截断
    assert len("job.audio_tts_stale") <= 32
    assert len("audio.fingerprint_missing") <= 32


def test_legacy_writes_fingerprint_missing_event(db, scene_ctx):
    """宽限期复用必须留痕：audio.fingerprint_missing 事件带片段清单。"""
    _add_segment(db, scene_ctx, fingerprint=None)

    job = _make_job(db, scene_ctx)
    result = executor._shot_ref_audios(db, scene_ctx["shot"])
    assert result["legacy"]
    if result["legacy"]:
        executor._write_event(db, job, "audio.fingerprint_missing", {"segments": result["legacy"]})
        db.commit()

    evs = _events(db, job)
    ev = [e for e in evs if e.event_type == "audio.fingerprint_missing"]
    assert len(ev) == 1
    assert len(ev[0].payload["segments"]) == 1


def test_no_event_when_nothing_stale(db, scene_ctx):
    """指纹一致且无历史片段时不写噪音事件。"""
    seg = _add_segment(db, scene_ctx)
    seg.tts_fingerprint = _current_fingerprint(db, scene_ctx, seg)
    db.commit()

    job = _make_job(db, scene_ctx)
    result = executor._shot_ref_audios(db, scene_ctx["shot"])
    assert result["stale"] == [] and result["legacy"] == []
    assert _events(db, job) == []
