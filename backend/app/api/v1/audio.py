"""音频模块 API：规划、时间轴、片段、审核、音色配置。

路由前缀 /api/audio（main.py 注册）。设计约束：
- 音频规划从解析结果确定性生成，不虚构数量
- 生成走统一 Job 系统（复用 generate_audio/generate_bgm/generate_sfx）
- 审核绑定具体 asset_id + version
- 音色配置支持试听（异步 Job）
"""
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func as sa_func, select
from sqlalchemy.orm import Session

from app.core.config import stored_path
from app.core.database import get_db
from app.models import ids
from app.models.audio import AudioReview, AudioSegment, VoiceProfile
from app.models.enums import (
    AUDIO_REVIEW_STATUS,
    AUDIO_GENERATION_STATUS,
    TRACK_TYPE,
)
from app.models.narrative import Character
from app.models.production import Asset, Job, Workflow
from app.services import audio_plan, tts_params, tts_workflow
from app.services.errors import biz_error, character_not_found

router = APIRouter(prefix="/api/audio", tags=["audio"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_naive() -> datetime:
    """Job.created_at 为 DateTime 且无默认值，需显式赋 naive UTC 时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _get_project_or_404(db: Session, project_id: str):
    from app.models.narrative import Project
    project = db.get(Project, project_id)
    if not project:
        raise biz_error(code="project_not_found", http=404, message="项目不存在")
    return project


# ============ 音频规划 ============

@router.post("/projects/{project_id}/episodes/{episode}/plan")
def create_audio_plan(project_id: str, episode: int, db: Session = Depends(get_db)) -> dict:
    """生成/重建音频规划（幂等：清旧重建，native_audio 片段保留）。"""
    _get_project_or_404(db, project_id)
    result = audio_plan.build_audio_plan(db, project_id, episode)
    return result


@router.get("/projects/{project_id}/episodes/{episode}/plan")
def get_audio_plan(project_id: str, episode: int, db: Session = Depends(get_db)) -> dict:
    """获取音频规划：按轨道分组 + 统计。"""
    _get_project_or_404(db, project_id)
    return audio_plan.get_audio_plan(db, project_id, episode)


@router.get("/projects/{project_id}/episodes/{episode}/timeline")
def get_audio_timeline(project_id: str, episode: int, db: Session = Depends(get_db)) -> dict:
    """获取音频时间轴（最终合成页轨道配置数据源）。"""
    _get_project_or_404(db, project_id)
    return audio_plan.get_audio_timeline(db, project_id, episode)


# ============ 片段 ============

def _probe_audio_duration_ms(path: str) -> int | None:
    """ffprobe 探测音频时长（毫秒）；探测失败返回 None，不阻断替换动作。"""
    import subprocess
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", path],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        import json as _json
        dur = float((_json.loads(proc.stdout).get("format") or {}).get("duration") or 0)
        return int(round(dur * 1000)) if dur > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _get_segment(db: Session, segment_id: str) -> AudioSegment:
    seg = db.get(AudioSegment, segment_id)
    if not seg:
        raise biz_error(code="audio_segment_not_found", http=404, message="音频片段不存在")
    return seg


@router.post("/segments/{segment_id}/regenerate", status_code=202)
def regenerate_segment(segment_id: str, payload: dict | None = None, db: Session = Depends(get_db)) -> dict:
    """重新生成片段：创建 generate_audio/generate_bgm/generate_sfx Job（新版本，不覆盖旧资产）。"""
    seg = _get_segment(db, segment_id)
    payload = payload or {}
    # 轨道 → job_type 映射
    job_type_map = {
        "dialogue": "generate_audio",
        "narration": "generate_audio",
        "bgm": "generate_bgm",
        "sfx": "generate_sfx",
        "ambient": "generate_sfx",
    }
    job_type = job_type_map.get(seg.track_type)
    if not job_type:
        raise biz_error(code="invalid_track", http=422, message=f"轨道 {seg.track_type} 不支持生成")
    if not (seg.text or "").strip():
        raise biz_error(code="empty_text", http=422, message="片段无文本内容，无法生成")

    # 幂等键：同片段在途任务去重（P2 修复：按 audio_segment_id 粒度。
    # 旧逻辑按 shot+job_type 粒度，同镜头多对白片段互相误判 409，shot_id 为 None 时去重失效）
    inflight_jobs = db.execute(
        select(Job).where(
            Job.project_id == seg.project_id,
            Job.job_type == job_type,
            Job.status.in_(["queued", "running"]),
        )
    ).scalars().all()
    inflight = next(
        (j for j in inflight_jobs
         if (j.input_snapshot or {}).get("audio_segment_id") == seg.id),
        None,
    )
    if inflight:
        raise biz_error(code="job_inflight", http=409, message="该片段任务在途，请稍后再试")

    params: dict = {}
    if job_type == "generate_audio":
        # 参数构建统一走 services/tts_params.build_params：与 H3 复用判定的指纹重算同源，
        # 避免两处各写一份导致指纹恒失配（A4 §13.5）。
        voice = _resolve_voice(db, seg)
        params = tts_params.build_params(
            seg,
            voice,
            instruct_override=payload.get("instruct"),
            emotion_override=payload.get("emotion"),
        )
    elif job_type == "generate_bgm":
        params["bgm_style"] = seg.text
        params["duration"] = payload.get("duration")
    else:
        params["sfx_prompt"] = seg.text

    # 绑定工作流：执行器对 workflow_id 为空的任务直接以 no_matching_workflow 失败。
    # 按轨道类型路由：对白/旁白 → voice_mode=preset 的 TTS workflow；BGM → bgm 类型；
    # SFX 当前无已部署 Provider，明确拒绝并引导使用素材库（replace 接口）
    workflow_id = None
    if job_type == "generate_audio":
        _wf = tts_workflow.resolve_preset_workflow(db)
        if _wf is None:
            raise biz_error(code="no_matching_workflow", http=422, message="无可用 TTS 工作流，请先在设置中导入")
        workflow_id = _wf.id
    elif job_type == "generate_bgm":
        _bgm = db.execute(
            select(Workflow).where(Workflow.workflow_type == "bgm").order_by(Workflow.created_at)
        ).scalars().first()
        if _bgm is None:
            raise biz_error(code="no_matching_workflow", http=422, message="BGM 生成服务未配置")
        workflow_id = _bgm.id
    else:
        raise biz_error(
            code="sfx_provider_not_configured", http=422,
            message="SFX 生成服务未部署，请使用素材库音频（replace 接口）或稍后再试",
        )

    job = Job(
        id=ids.job_id(),
        project_id=seg.project_id,
        shot_id=seg.shot_id,
        job_type=job_type,
        status="queued",
        workflow_id=workflow_id,
        params=params,
        input_snapshot={"audio_segment_id": seg.id, "regeneration": True},
        created_at=_now_naive(),
    )
    db.add(job)
    seg.generation_status = "queued"
    db.commit()
    return {"job_id": job.id, "segment_id": seg.id, "job_type": job_type}


def _resolve_voice(db: Session, seg: AudioSegment) -> VoiceProfile | None:
    """片段音色解析：片段覆盖 → 角色默认音色 → 兼容 Character.voice_id → 项目旁白音色。

    实现已合并到 `services/tts_params.resolve_voice`：生成侧与 H3 复用判定必须同源，
    否则指纹会因语言/默认 instruct 不同而恒失配（A4 §13.5）。
    """
    return tts_params.resolve_voice(db, seg)


@router.post("/segments/{segment_id}/replace", status_code=202)
def replace_segment(segment_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """上传替换音频：登记新版本 audio Asset，片段指向新资产。"""
    seg = _get_segment(db, segment_id)
    file_path = (payload or {}).get("file_path")
    if not file_path:
        raise biz_error(code="invalid_request", http=422, message="file_path 必填")
    # 新版本资产（不覆盖旧文件）
    old = db.get(Asset, seg.audio_asset_id) if seg.audio_asset_id else None
    version = (old.version + 1) if old else 1
    asset = Asset(
        id=ids.asset_id(),
        project_id=seg.project_id,
        asset_type="audio",
        name=f"{seg.track_type}_{seg.id[:8]}_v{version}",
        version=version,
        parent_id=old.id if old else None,
        file_path=file_path,
        source_job_id=None,
    )
    db.add(asset)
    # 旧资产 superseded
    if old:
        old.validity = "superseded"
    seg.audio_asset_id = asset.id
    seg.generation_status = "completed"
    seg.review_status = "pending"  # 新版本回到待审核
    # 修复：替换后必须重测时长。合成阶段 offset 累加与镜头目标时长都以 duration_ms 为准，
    # 沿用旧值会导致音画错位且不报错（替换为更长音频时尤甚）。探测失败回写 None 并提示。
    seg.duration_ms = _probe_audio_duration_ms(file_path)
    db.commit()
    return {
        "asset_id": asset.id,
        "version": version,
        "segment_id": seg.id,
        "duration_ms": seg.duration_ms,
    }


# ============ 审核 ============

@router.post("/assets/{asset_id}/review", status_code=201)
def review_asset(asset_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """审核具体音频资产版本：approved / rejected。审核记录绑定 asset_id + version。"""
    asset = db.get(Asset, asset_id)
    if not asset or asset.asset_type != "audio":
        raise biz_error(code="audio_asset_not_found", http=404, message="音频资产不存在")
    status = (payload or {}).get("status")
    if status not in ("approved", "rejected"):
        raise biz_error(code="invalid_request", http=422, message="status 必须为 approved/rejected")
    comment = (payload or {}).get("comment")
    review = AudioReview(
        id=ids.audio_review_id(),
        project_id=asset.project_id,
        audio_asset_id=asset.id,
        asset_version=asset.version,
        segment_id=(payload or {}).get("segment_id"),
        status=status,
        comment=comment,
        reviewed_by=(payload or {}).get("reviewed_by"),
    )
    db.add(review)
    # 同步片段审核状态（仅当片段当前指向该资产版本）
    seg_id = (payload or {}).get("segment_id")
    if seg_id:
        seg = db.get(AudioSegment, seg_id)
        if seg and seg.audio_asset_id == asset.id:
            seg.review_status = status
    db.commit()
    return {"review_id": review.id, "asset_id": asset.id, "status": status}


# ============ speaker 列表（来自 workflow meta，禁止硬编码前端） ============

@router.get("/speakers")
def list_speakers(project_id: str | None = None, db: Session = Depends(get_db)) -> dict:
    """列出当前 preset TTS workflow 支持的 speaker。

    speaker 与元数据解析统一委托 services/tts_workflow（唯一权威）：
    capabilities 旧字段 → parsed_schema 枚举 → VoiceLibrary 音色库。
    历史实现直接读 `caps["voice_values"]`，meta.json 退场后该键不再落库，
    导致 /speakers 恒为空列表（试听报"无可用 TTS 工作流"的根因之一）。

    - 支持传入 project_id，用于按项目级配置屏蔽被禁用的 speaker
      （Project.settings.audio_config.disabled_speakers）；不传则返回全局可用列表
    - language / description / display_name 均来自真实数据，缺失返回空值，
      前端按空值降级展示，不伪造
    """
    wf = tts_workflow.resolve_preset_workflow(db)
    # 项目级 speaker 禁用列表（真实存在才过滤，不做默认放行的伪字段）
    disabled: set[str] = set()
    if project_id:
        from app.models.narrative import Project
        project = db.get(Project, project_id)
        if not project:
            raise biz_error(code="project_not_found", http=404, message="项目不存在")
        cfg = ((project.settings or {}).get("audio_config") or {})
        disabled = {str(x) for x in (cfg.get("disabled_speakers") or [])}
    speakers: list[dict] = []
    for entry in tts_workflow.speaker_entries(db, wf):
        speakers.append({
            **entry,
            "enabled": entry["speaker"] not in disabled,
        })
    return {"workflow_id": wf.id if wf else None, "speakers": speakers}


# ============ 项目旁白配置 ============

@router.get("/projects/{project_id}/narrator-voice")
def get_narrator_voice(project_id: str, db: Session = Depends(get_db)) -> dict:
    """获取项目级旁白 speaker 配置（存于 Project.settings.narrator_voice）。"""
    from app.models.narrative import Project
    project = db.get(Project, project_id)
    if not project:
        raise biz_error(code="project_not_found", http=404, message="项目不存在")
    nv = (project.settings or {}).get("narrator_voice") or {}
    return {"project_id": project_id, "narrator_voice": nv}


@router.put("/projects/{project_id}/narrator-voice")
def set_narrator_voice(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """设置项目级旁白 speaker 配置。speaker 必填且须来自支持列表。"""
    from app.models.narrative import Project
    project = db.get(Project, project_id)
    if not project:
        raise biz_error(code="project_not_found", http=404, message="项目不存在")
    nv = (payload or {}).get("narrator_voice") or {}
    speaker = nv.get("speaker")
    if not speaker:
        raise biz_error(code="invalid_request", http=422, message="speaker 必填")
    settings = dict(project.settings or {})
    settings["narrator_voice"] = {
        "provider": nv.get("provider", "qwen3_tts"),
        "model": nv.get("model", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"),
        "speaker": speaker,
        "language": nv.get("language", "Chinese"),
        "default_instruct": nv.get("default_instruct", ""),
    }
    project.settings = settings
    db.commit()
    return {"project_id": project_id, "narrator_voice": settings["narrator_voice"]}


# ============ 项目级音频策略 ============

_NATIVE_AUDIO_POLICIES = ("discard", "preserve", "preserve_ambience_only", "native")


def _default_audio_config(cfg: dict) -> dict:
    """项目级音频策略默认值（默认 native：使用 H3 内嵌音轨，TTS 仅作 ref_audio 参考）。"""
    return {
        "tts_enabled": bool(cfg.get("tts_enabled", True)),
        "tts_provider": cfg.get("tts_provider", "qwen3-tts"),
        "tts_model": cfg.get("tts_model", "CustomVoice"),
        "native_audio_policy": cfg.get("native_audio_policy", "native"),
        "bgm_enabled": bool(cfg.get("bgm_enabled", True)),
        "sfx_enabled": bool(cfg.get("sfx_enabled", True)),
    }


@router.get("/projects/{project_id}/audio-config")
def get_audio_config(project_id: str, db: Session = Depends(get_db)) -> dict:
    """获取项目级音频策略（Project.settings.audio_config）。"""
    from app.models.narrative import Project
    project = db.get(Project, project_id)
    if not project:
        raise biz_error(code="project_not_found", http=404, message="项目不存在")
    cfg = _default_audio_config((project.settings or {}).get("audio_config") or {})
    return {"project_id": project_id, "audio_config": cfg}


@router.put("/projects/{project_id}/audio-config")
def set_audio_config(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """设置项目级音频策略。native_audio_policy ∈ discard/preserve/preserve_ambience_only/native。"""
    from app.models.narrative import Project
    project = db.get(Project, project_id)
    if not project:
        raise biz_error(code="project_not_found", http=404, message="项目不存在")
    cfg = (payload or {}).get("audio_config") or {}
    policy = cfg.get("native_audio_policy", "discard")
    if policy not in _NATIVE_AUDIO_POLICIES:
        raise biz_error(code="invalid_request", http=422,
                        message="native_audio_policy 必须为 discard/preserve/preserve_ambience_only/native")
    merged = _default_audio_config((project.settings or {}).get("audio_config") or {})
    merged.update({
        "tts_enabled": bool(cfg.get("tts_enabled", merged["tts_enabled"])),
        "tts_provider": cfg.get("tts_provider") or merged["tts_provider"],
        "tts_model": cfg.get("tts_model") or merged["tts_model"],
        "native_audio_policy": policy,
        "bgm_enabled": bool(cfg.get("bgm_enabled", merged["bgm_enabled"])),
        "sfx_enabled": bool(cfg.get("sfx_enabled", merged["sfx_enabled"])),
    })
    settings = dict(project.settings or {})
    settings["audio_config"] = merged
    project.settings = settings
    db.commit()
    return {"project_id": project_id, "audio_config": merged}


# ============ 音色配置 ============

@router.get("/characters/{character_id}/voices")
def list_voices(character_id: str, db: Session = Depends(get_db)) -> dict:
    """列出角色音色配置。"""
    ch = db.get(Character, character_id)
    if not ch:
        raise character_not_found()
    rows = db.execute(
        select(VoiceProfile).where(VoiceProfile.character_id == character_id)
        .order_by(VoiceProfile.is_default.desc(), VoiceProfile.created_at)
    ).scalars().all()
    return {"character_id": character_id, "voices": [_voice_out(v) for v in rows]}


@router.post("/characters/{character_id}/voices", status_code=201)
def create_voice(character_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """设置角色音色（**单条覆盖语义，无版本概念**）。

    音色只有「当前这一条」：该角色已有配置时原地覆盖全部字段（保留原 id），
    没有才新建；历史遗留的多条记录在本次保存时一并清理，只保留最新一条。
    历史音频的可追溯性由 AudioSegment.audio_asset_id + Composition.source_snapshot
    承担，不依赖音色配置版本。
    """
    ch = db.get(Character, character_id)
    if not ch:
        raise character_not_found()
    voice_id = (payload or {}).get("voice_id")
    if not voice_id:
        raise biz_error(code="invalid_request", http=422, message="voice_id 必填")
    fields = {
        "voice_provider": (payload or {}).get("voice_provider", "qwen3_tts"),
        "voice_model": (payload or {}).get(
            "voice_model", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"),
        "mode": (payload or {}).get("mode", "custom_voice"),
        "voice_id": voice_id,
        "voice_name": (payload or {}).get("voice_name", voice_id),
        "language": (payload or {}).get("language", "Chinese"),
        "default_instruct": (payload or {}).get("default_instruct", ""),
        "speed": float((payload or {}).get("speed", 1.0)),
        "pitch": (payload or {}).get("pitch"),
        "default_emotion": (payload or {}).get("default_emotion", "neutral"),
        "voice_reference_asset_id": (payload or {}).get("voice_reference_asset_id"),
        "enabled": bool((payload or {}).get("enabled", True)),
        "approved": bool((payload or {}).get("approved", False)),
    }
    existing = db.execute(
        select(VoiceProfile).where(VoiceProfile.character_id == character_id)
        .order_by(VoiceProfile.created_at)
    ).scalars().all()
    vp = existing[0] if existing else None
    if vp is None:
        # voice_profile 表的实际建表语句没有 server_default（模型声明了 func.now()
        # 但迁移未落到库上），新建时必须显式写入时间，否则 NOT NULL 约束失败
        vp = VoiceProfile(
            id=ids.voice_profile_id(), project_id=ch.project_id,
            character_id=character_id, created_at=_now_naive(),
            updated_at=_now_naive(), **fields,
        )
        db.add(vp)
    else:
        # 覆盖：以本次提交为准整体重写，不做增量合并
        for k, v in fields.items():
            setattr(vp, k, v)
        # 清理历史遗留的多条记录（旧逻辑每次新增都会累积），只保留最新一条
        for extra in existing[1:]:
            db.delete(extra)
    db.flush()
    # 保证该角色仅此一条为默认
    _clear_default(db, character_id)
    vp.is_default = True
    db.commit()
    return _voice_out(vp)


@router.patch("/voices/{voice_profile_id}")
def update_voice(voice_profile_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """更新音色配置。修改默认音色不影响已生成并审核通过的历史音频。"""
    vp = db.get(VoiceProfile, voice_profile_id)
    if not vp:
        raise biz_error(code="voice_profile_not_found", http=404, message="音色配置不存在")
    allowed = ("voice_provider", "voice_model", "mode", "voice_id", "voice_name", "language",
               "default_instruct", "speed", "pitch", "default_emotion",
               "voice_reference_asset_id", "enabled", "approved")
    for k in allowed:
        if k in (payload or {}):
            setattr(vp, k, payload[k])
    if (payload or {}).get("is_default") is True:
        _clear_default(db, vp.character_id)
        vp.is_default = True
    db.commit()
    return _voice_out(vp)


@router.post("/voices/{voice_profile_id}/preview", status_code=202)
def preview_voice(voice_profile_id: str, payload: dict | None = None, db: Session = Depends(get_db)) -> dict:
    """试听音色：创建 generate_audio Job（异步）。"""
    vp = db.get(VoiceProfile, voice_profile_id)
    if not vp:
        raise biz_error(code="voice_profile_not_found", http=404, message="音色配置不存在")
    _payload = payload or {}
    text = _payload.get("text") or "你好，这是音色试听。"
    # 试听缓存：speaker + 语言 + 情绪指令 + 文本 任一变化即视为失效。
    # 命中缓存时不建 Job、不调 ComfyUI，直接复用已生成的音频资产。
    _instruct = vp.default_instruct or vp.default_emotion or "neutral"
    _sig_src = "|".join([vp.voice_id or "", vp.language or "", _instruct, text])
    preview_signature = hashlib.sha256(_sig_src.encode("utf-8")).hexdigest()
    _force = bool(_payload.get("force"))
    if not _force and vp.preview_asset_id and vp.preview_signature == preview_signature:
        _cached = db.get(Asset, vp.preview_asset_id)
        _cached_path = stored_path(_cached.file_path) if (_cached and _cached.file_path) else None
        if _cached_path is not None and _cached_path.exists():
            return {
                "cached": True,
                "voice_profile_id": vp.id,
                "asset_id": _cached.id,
                "audio_url": f"/assets/{_cached.id}/file",
                "text": vp.preview_text or text,
                "signature": preview_signature,
            }
    # 试听任务必须绑定 TTS 工作流：执行器对 workflow_id 为空的任务直接以
    # no_matching_workflow 失败。匹配口径与 /speakers 一致（同一权威服务），
    # 优先 voice_mode=preset 的 custom_voice 工作流，保证 vp.voice_id 一定是该工作流支持的 speaker。
    wf = tts_workflow.resolve_tts_workflow(db, (payload or {}).get("workflow_id"))
    if wf is None:
        raise biz_error(
            code="no_matching_workflow", http=422, message="无可用 TTS 工作流，请先在设置中导入"
        )
    # 去重：同一音色已有在途试听任务时复用，避免「试听 + 重新生成」连点堆出多条排队任务
    # （单并发 worker 上每条都是一次真实合成，堆积后按钮会长时间停留在"合成中"）
    _inflight = db.execute(
        select(Job).where(
            Job.job_type == "generate_audio",
            Job.status.in_(("queued", "running")),
        )
    ).scalars().all()
    for _j in _inflight:
        _snap = _j.input_snapshot or {}
        if _snap.get("voice_preview") and _snap.get("voice_profile_id") == vp.id:
            return {
                "cached": False,
                "reused": True,
                "job_id": _j.id,
                "voice_profile_id": vp.id,
                "signature": preview_signature,
            }
    job = Job(
        id=ids.job_id(),
        project_id=vp.project_id,
        shot_id=None,
        job_type="generate_audio",
        status="queued",
        workflow_id=wf.id,
        params={
            "tts_text": text,
            "tts_voice": vp.voice_id,
            "tts_lang": vp.language or "Chinese",
            "tts_emotion": vp.default_instruct or vp.default_emotion or "neutral",
        },
        input_snapshot={
            "voice_preview": True,
            "voice_profile_id": vp.id,
            # 供执行完成后回写缓存：指纹与文本随任务固化，避免回写时参数已被改动
            "preview_signature": preview_signature,
            "preview_text": text,
        },
        # 试听只合成一句话（~0.5 分钟级），必须插到长视频/关键帧队列前面，
        # 否则用户在单并发 worker 上要等几分钟才能听到声音（表现为按钮一直转圈）
        priority=50,
        created_at=_now_naive(),
    )
    db.add(job)
    db.commit()
    return {
        "cached": False,
        "job_id": job.id,
        "voice_profile_id": vp.id,
        "signature": preview_signature,
    }


@router.get("/voices/{voice_profile_id}/preview/cache")
def get_voice_preview_cache(
    voice_profile_id: str, text: str | None = None, db: Session = Depends(get_db)
) -> dict:
    """只读查询试听缓存：不创建任务、不调用 ComfyUI（2026-11-07）。

    前端打开音色抽屉时调用本接口：命中则直接播放已生成的音频，
    未命中只返回 cached=false，必须由用户显式点击「试听」才会真正合成。
    与 POST 试听接口的关键区别是本接口永不产生算力消耗，
    用于修复『打开页面就重新合成一遍』的问题。
    """
    vp = db.get(VoiceProfile, voice_profile_id)
    if not vp:
        raise biz_error(code="voice_profile_not_found", http=404, message="音色配置不存在")
    _text = text or "你好，这是音色试听。"
    _instruct = vp.default_instruct or vp.default_emotion or "neutral"
    _sig_src = "|".join([vp.voice_id or "", vp.language or "", _instruct, _text])
    preview_signature = hashlib.sha256(_sig_src.encode("utf-8")).hexdigest()
    if vp.preview_asset_id and vp.preview_signature == preview_signature:
        _cached = db.get(Asset, vp.preview_asset_id)
        _cached_path = stored_path(_cached.file_path) if (_cached and _cached.file_path) else None
        # 文件被清理过则视为未命中，避免返回死链
        if _cached_path is not None and _cached_path.exists():
            return {
                "cached": True,
                "voice_profile_id": vp.id,
                "asset_id": _cached.id,
                "audio_url": f"/assets/{_cached.id}/file",
                "text": vp.preview_text or _text,
                "signature": preview_signature,
            }
    return {"cached": False, "voice_profile_id": vp.id, "signature": preview_signature}


def _clear_default(db: Session, character_id: str) -> None:
    rows = db.execute(
        select(VoiceProfile).where(
            VoiceProfile.character_id == character_id,
            VoiceProfile.is_default == True,  # noqa: E712
        )
    ).scalars().all()
    for v in rows:
        v.is_default = False


def _voice_out(v: VoiceProfile) -> dict:
    return {
        "id": v.id,
        "project_id": v.project_id,
        "character_id": v.character_id,
        "voice_provider": v.voice_provider,
        "voice_model": v.voice_model,
        "mode": v.mode,
        "voice_id": v.voice_id,
        "voice_name": v.voice_name,
        "language": v.language,
        "default_instruct": v.default_instruct,
        "speed": v.speed,
        "pitch": v.pitch,
        "default_emotion": v.default_emotion,
        "voice_reference_asset_id": v.voice_reference_asset_id,
        "enabled": v.enabled,
        "approved": v.approved,
        "is_default": v.is_default,
    }


# ============ 音频生产进度（导演台，2026-11-07）============

@router.get("/projects/{project_id}/audio/progress")
def audio_production_progress(project_id: str, db: Session = Depends(get_db)) -> dict:
    """音频生产进度聚合：按轨道类型分组统计各生成状态数量。

    - 数据来源：audio_segment 表实时聚合，不伪造进度
    - ambient（环境声）并入 sfx 展示（同为音效类轨道）；native_audio 不参与统计
    """
    _get_project_or_404(db, project_id)
    rows = db.execute(
        select(
            AudioSegment.track_type,
            AudioSegment.generation_status,
            sa_func.count(AudioSegment.id),
        )
        .where(
            AudioSegment.project_id == project_id,
            AudioSegment.track_type != "native_audio",
        )
        .group_by(AudioSegment.track_type, AudioSegment.generation_status)
    ).all()
    track_map = {"dialogue": "dialogue", "narration": "narration", "bgm": "bgm",
                 "sfx": "sfx", "ambient": "sfx"}
    result: dict[str, dict] = {v: {"total": 0} for v in ("dialogue", "narration", "bgm", "sfx")}
    for track, status, n in rows:
        key = track_map.get(str(track))
        if key is None:
            continue
        bucket = result[key]
        n = int(n)
        bucket["total"] += n
        bucket[str(status)] = bucket.get(str(status), 0) + n
    return {"project_id": project_id, "progress": result}


@router.get("/projects/{project_id}/audio/segments")
def audio_segments_detail(
    project_id: str,
    episode: int | None = None,
    character_id: str | None = None,
    shot_id: str | None = None,
    track_type: str | None = None,
    segment_type: str | None = None,
    generation_status: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
) -> dict:
    """音频片段明细（分页 + 过滤）：导演台音频进度展开视图的数据源。"""
    _get_project_or_404(db, project_id)
    conds = [AudioSegment.project_id == project_id]
    if episode is not None:
        conds.append(AudioSegment.episode == episode)
    if character_id:
        conds.append(AudioSegment.character_id == character_id)
    if shot_id:
        conds.append(AudioSegment.shot_id == shot_id)
    if track_type:
        conds.append(AudioSegment.track_type == track_type)
    if segment_type:
        conds.append(AudioSegment.segment_type == segment_type)
    if generation_status:
        conds.append(AudioSegment.generation_status == generation_status)
    total = int(
        db.execute(select(sa_func.count()).select_from(AudioSegment).where(*conds)).scalar() or 0
    )
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    rows = list(
        db.execute(
            select(AudioSegment).where(*conds)
            .order_by(AudioSegment.episode, AudioSegment.created_at)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).scalars().all()
    )
    cids = {r.character_id for r in rows if r.character_id}
    char_names: dict[str, str] = {}
    if cids:
        char_names = {
            c.id: c.name
            for c in db.execute(select(Character).where(Character.id.in_(cids))).scalars().all()
        }
    items = [
        {
            "id": r.id,
            "episode": r.episode,
            "shot_id": r.shot_id,
            "track_type": r.track_type,
            "segment_type": r.segment_type,
            "character_id": r.character_id,
            "character_name": char_names.get(r.character_id) if r.character_id else None,
            "text": r.text,
            "voice_profile_id": r.voice_profile_id,
            "sequence": r.sequence,
            "generation_status": r.generation_status,
            "review_status": r.review_status,
            "audio_asset_id": r.audio_asset_id,
            "duration_ms": r.duration_ms,
            "note": r.note,
        }
        for r in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}
