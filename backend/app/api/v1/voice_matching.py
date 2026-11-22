"""角色音色智能匹配 API（2026-11-07）。

路由前缀 /api/voice-matching（main.py 注册）。能力：
- GET  /voices/system                ：系统默认音色库（9 个，支持 gender/enabled 过滤）
- PATCH /voices/system/{voice_code}  ：管理员维护音色描述/标签/启用状态（speaker_id 不可改）
- POST /voices/system/{voice_code}/preview ：系统音色试听（缓存复用，统一测试句）
- POST /projects/{project_id}/voices/match ：批量自动匹配（LLM 画像 + 程序评分 + 全局分配）
- POST /projects/{project_id}/characters/{character_id}/voice/confirm ：确认音色（触发闸口放行）
- POST /projects/{project_id}/characters/{character_id}/voice/lock|unlock ：锁定/解锁

纪律：
- speaker_id 只读 VoiceLibrary（源头 workflow meta），LLM/前端无权创造
- 锁定角色不被自动匹配覆盖
- 音色确认后放行 waiting_voice_confirmation 的对白片段
"""
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import stored_path
from app.core.database import get_db
from app.models import ids
from app.models.audio import AudioSegment, VoiceLibrary, VoiceProfile
from app.models.narrative import Character
from app.models.production import Asset, Job
from app.services import errors, voice_matching
from app.services.errors import biz_error

router = APIRouter(prefix="/api", tags=["voice-matching"])


def _naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _voice_out(v: VoiceLibrary) -> dict:
    return {
        "id": v.id,
        "voice_code": v.voice_code,
        "name": v.name,
        "speaker_id": v.speaker_id,
        "display_name": v.display_name,
        "gender_presentation": v.gender_presentation,
        "perceived_age": v.perceived_age,
        "pitch": v.pitch,
        "timbre": v.timbre or [],
        "personality_tags": v.personality_tags or [],
        "speaking_styles": v.speaking_styles or [],
        "language": v.language,
        "description": v.description or "",
        "source_workflow": v.source_workflow or "",
        "enabled": v.enabled,
        "sort_order": v.sort_order,
        "has_preview": bool(v.preview_asset_id),
    }


def _get_character(db: Session, character_id: str) -> Character:
    ch = db.get(Character, character_id)
    if not ch:
        raise errors.character_not_found()
    return ch


def _get_default_binding(db: Session, project_id: str, character_id: str) -> VoiceProfile | None:
    return db.execute(
        select(VoiceProfile).where(
            VoiceProfile.project_id == project_id,
            VoiceProfile.character_id == character_id,
            VoiceProfile.is_default == True,  # noqa: E712
        )
    ).scalars().first()


@router.get("/voices/system")
def list_system_voices(
    gender: str | None = None, enabled: bool | None = None, db: Session = Depends(get_db)
) -> dict:
    """系统默认音色库：9 个默认音色（男 5 / 女 4）。speaker_id 溯源 workflow meta。"""
    q = select(VoiceLibrary).order_by(VoiceLibrary.sort_order)
    if gender:
        q = q.where(VoiceLibrary.gender_presentation == gender)
    if enabled is not None:
        q = q.where(VoiceLibrary.enabled == enabled)
    rows = list(db.execute(q).scalars().all())
    male = sum(1 for v in rows if v.gender_presentation == "male")
    female = sum(1 for v in rows if v.gender_presentation == "female")
    return {
        "items": [_voice_out(v) for v in rows],
        "total": len(rows),
        "male_count": male,
        "female_count": female,
    }


@router.patch("/voices/system/{voice_code}")
def update_system_voice(voice_code: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """维护系统音色：仅允许修改描述/标签/启用状态/排序；speaker_id 不可改（防虚构）。"""
    v = db.execute(
        select(VoiceLibrary).where(VoiceLibrary.voice_code == voice_code)
    ).scalars().first()
    if not v:
        raise biz_error(code="voice_not_found", http=404, message="系统音色不存在")
    allowed = ("name", "perceived_age", "pitch", "timbre", "personality_tags",
               "speaking_styles", "description", "enabled", "sort_order")
    for k in allowed:
        if k in (payload or {}):
            setattr(v, k, payload[k])
    db.commit()
    return _voice_out(v)


@router.post("/voices/system/{voice_code}/preview")
def preview_system_voice(
    voice_code: str, payload: dict | None = None, db: Session = Depends(get_db)
) -> dict:
    """系统音色试听：统一测试句（保证不同音色可比较），缓存复用不重复生成。"""
    v = db.execute(
        select(VoiceLibrary).where(VoiceLibrary.voice_code == voice_code)
    ).scalars().first()
    if not v:
        raise biz_error(code="voice_not_found", http=404, message="系统音色不存在")
    _payload = payload or {}
    text = _payload.get("text") or "你好，我是这个故事中的角色。"
    # Job.project_id 非空约束：系统级试听任务需挂靠一个项目（优先显式传入，
    # 否则取最早创建的项目；库中无项目时明确报错，不伪造）
    from app.models.narrative import Project
    preview_project_id = _payload.get("project_id")
    if preview_project_id:
        if not db.get(Project, preview_project_id):
            raise biz_error(code="project_not_found", http=404, message="项目不存在")
    else:
        _first = db.execute(select(Project).order_by(Project.created_at)).scalars().first()
        if _first is None:
            raise biz_error(code="project_required", http=422,
                            message="系统中暂无项目，无法创建试听任务；请先创建项目或显式传入 project_id")
        preview_project_id = _first.id
    sig_src = "|".join([v.speaker_id, v.language, "system_preview", text])
    signature = hashlib.sha256(sig_src.encode("utf-8")).hexdigest()
    if not bool(_payload.get("force")) and v.preview_asset_id:
        cached = db.get(Asset, v.preview_asset_id)
        if cached and cached.file_path and stored_path(cached.file_path).exists():
            return {
                "cached": True,
                "voice_code": v.voice_code,
                "asset_id": cached.id,
                "audio_url": f"/assets/{cached.id}/file",
                "text": text,
                "signature": signature,
            }
    # 定位 preset TTS workflow（与 /api/audio/speakers 同一权威服务）
    from app.services import tts_workflow as tts_wf_service
    wf = tts_wf_service.resolve_preset_workflow(db)
    if wf is None:
        raise biz_error(code="no_matching_workflow", http=422, message="无可用 TTS 工作流，请先在设置中导入")
    supported = tts_wf_service.workflow_speakers(db, wf)
    if v.speaker_id not in supported:
        raise biz_error(code="speaker_not_in_workflow", http=422,
                        message=f"speaker {v.speaker_id} 不在当前 TTS workflow 支持列表中")
    # 去重：同一系统音色已有在途试听任务时复用，避免连点堆出多条排队任务
    _inflight = db.execute(
        select(Job).where(
            Job.job_type == "generate_audio",
            Job.status.in_(("queued", "running")),
        )
    ).scalars().all()
    for _j in _inflight:
        _snap = _j.input_snapshot or {}
        if _snap.get("voice_preview") and _snap.get("system_voice_code") == v.voice_code:
            return {
                "cached": False,
                "reused": True,
                "job_id": _j.id,
                "voice_code": v.voice_code,
                "signature": signature,
            }
    job = Job(
        id=ids.job_id(),
        project_id=preview_project_id,
        shot_id=None,
        job_type="generate_audio",
        status="queued",
        workflow_id=wf.id,
        params={
            "tts_text": text,
            "tts_voice": v.speaker_id,
            "tts_lang": v.language or "Chinese",
            "tts_emotion": "",
        },
        input_snapshot={
            "voice_preview": True,
            "system_voice_code": v.voice_code,
            "preview_signature": signature,
            "preview_text": text,
        },
        priority=50,  # 与角色试听一致：短任务插队，避免排在长生成任务后面
        created_at=_naive_now(),
    )
    db.add(job)
    db.commit()
    return {
        "cached": False,
        "job_id": job.id,
        "voice_code": v.voice_code,
        "signature": signature,
    }


@router.post("/projects/{project_id}/voices/match")
def match_project_voices(project_id: str, payload: dict | None = None, db: Session = Depends(get_db)) -> dict:
    """批量自动匹配：LLM 声音画像 → 程序评分 → 全局分配 → 持久化候选。

    默认只匹配未锁定角色；include_locked=True 时连人工指定且已确认的也重新匹配
    （锁定角色无论开关都不参与）。匹配结果仅写候选字段，approved 不变，待用户确认。
    """
    from app.models.narrative import Project
    project = db.get(Project, project_id)
    if not project:
        raise biz_error(code="project_not_found", http=404, message="项目不存在")
    payload = payload or {}
    try:
        result = voice_matching.run_match(
            db,
            project_id,
            character_ids=payload.get("character_ids") or None,
            include_locked=bool(payload.get("include_locked")),
        )
    except ValueError as e:
        raise biz_error(code="voice_library_empty", http=422, message=str(e))
    return result


@router.get("/projects/{project_id}/characters/{character_id}/voice/binding")
def get_voice_binding(
    project_id: str, character_id: str, db: Session = Depends(get_db)
) -> dict:
    """角色音色绑定详情：含匹配评分、原因、候选音色、画像、锁定状态。"""
    ch = _get_character(db, character_id)
    vp = _get_default_binding(db, project_id, character_id)
    if not vp:
        return {
            "character_id": character_id,
            "character_name": ch.name,
            "binding": None,
        }
    alts = vp.alternatives if isinstance(vp.alternatives, list) else []
    return {
        "character_id": character_id,
        "character_name": ch.name,
        "binding": {
            "voice_profile_id": vp.id,
            "voice_library_id": vp.voice_library_id,
            "voice_id": vp.voice_id,
            "voice_name": vp.voice_name,
            "match_score": vp.match_score,
            "match_reason": vp.match_reason,
            "alternatives": alts,
            "voice_profile": vp.voice_profile_json or {},
            "status": "locked" if vp.is_locked else ("confirmed" if vp.approved else "pending"),
            "is_manual": vp.source == "manual",
            "source": vp.source,
            "approved": vp.approved,
            "is_locked": vp.is_locked,
        },
    }


@router.post("/projects/{project_id}/characters/{character_id}/voice/confirm")
def confirm_voice(
    project_id: str, character_id: str, payload: dict | None = None, db: Session = Depends(get_db)
) -> dict:
    """确认角色音色：置 approved，并放行该角色 waiting_voice_confirmation 的对白片段。"""
    _get_character(db, character_id)
    vp = _get_default_binding(db, project_id, character_id)
    if not vp:
        raise biz_error(code="voice_profile_not_found", http=404, message="角色尚未配置音色，请先匹配或手动指定")
    payload = payload or {}
    voice_library_id = payload.get("voice_library_id")
    if voice_library_id and voice_library_id != vp.voice_library_id:
        # 用户在候选中选择了其他音色：校验真实存在并应用
        v = db.get(VoiceLibrary, voice_library_id)
        if not v or not v.enabled:
            raise biz_error(code="voice_not_found", http=422, message="所选音色不存在或已禁用")
        vp.voice_library_id = v.id
        vp.voice_id = v.speaker_id
        vp.voice_name = v.name
    vp.approved = True
    if payload.get("lock"):
        vp.is_locked = True
    vp.updated_at = _naive_now()
    # 闸口放行：该角色等待音色确认的对白片段 → pending
    released = db.execute(
        select(AudioSegment).where(
            AudioSegment.project_id == project_id,
            AudioSegment.character_id == character_id,
            AudioSegment.track_type == "dialogue",
            AudioSegment.generation_status == "waiting_voice_confirmation",
        )
    ).scalars().all()
    for seg in released:
        seg.generation_status = "pending"
    db.commit()
    return {
        "character_id": character_id,
        "voice_id": vp.voice_id,
        "voice_name": vp.voice_name,
        "approved": True,
        "is_locked": vp.is_locked,
        "released_segments": len(released),
    }


@router.post("/projects/{project_id}/characters/{character_id}/voice/lock")
def lock_voice(project_id: str, character_id: str, db: Session = Depends(get_db)) -> dict:
    """锁定角色音色：锁定后自动匹配绝不覆盖。"""
    _get_character(db, character_id)
    vp = _get_default_binding(db, project_id, character_id)
    if not vp:
        raise biz_error(code="voice_profile_not_found", http=404, message="角色尚未配置音色")
    vp.is_locked = True
    vp.source = "manual"
    vp.updated_at = _naive_now()
    db.commit()
    return {"character_id": character_id, "is_locked": True}


@router.post("/projects/{project_id}/characters/{character_id}/voice/unlock")
def unlock_voice(project_id: str, character_id: str, db: Session = Depends(get_db)) -> dict:
    """解锁角色音色：解锁后可参与自动匹配。"""
    _get_character(db, character_id)
    vp = _get_default_binding(db, project_id, character_id)
    if not vp:
        raise biz_error(code="voice_profile_not_found", http=404, message="角色尚未配置音色")
    vp.is_locked = False
    vp.updated_at = _naive_now()
    db.commit()
    return {"character_id": character_id, "is_locked": False}
