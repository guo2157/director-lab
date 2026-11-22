"""TTS 工作流与 speaker 解析的唯一权威（2026-11-21）。

背景（为什么需要这个模块）：
`workflow.meta.json` 退场后，生产库里的 `workflow.capabilities` 是
`services/workflow/capabilities.normalize()` 的产物——`voice_mode` 被收进
`capabilities.audio.voice_mode`，`voice_values` 根本不落库。
旧代码有 5 处按 `caps.get("voice_mode") == "preset" and caps.get("voice_values")`
判 preset 工作流，实际全部读到 None，导致试听/生成一律报
`no_matching_workflow`（"无可用 TTS 工作流"）。

因此 preset 判定与 speaker 列表统一走这里，禁止各处再自行读 capabilities：
- preset 判定（按优先级）：
  1. 旧字段 `capabilities.voice_mode` / `capabilities.audio.voice_mode` == "preset"
     （兼容历史库与单测里手写的 capabilities）
  2. `parsed_schema.params` 含 `tts_voice` 且不含 `tts_voice_ref`
     → 以 speaker 名直传的 custom_voice 工作流
  3. 仅含 `tts_voice_ref` → 克隆工作流，不是 preset
- speaker 列表（按优先级）：
  1. 旧字段 `voice_values`
  2. `parsed_schema.params.tts_voice` 的枚举/选项
  3. VoiceLibrary（真实音色库，enabled 记录；优先 source_workflow 命中本工作流的）
停止条件一旦命中即返回，且不会为了"看起来有值"而伪造 speaker。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audio import VoiceLibrary
from app.models.production import Workflow

_TTS_VOICE_KEY = "tts_voice"
_TTS_VOICE_REF_KEY = "tts_voice_ref"


def _caps(wf: Workflow) -> dict:
    return wf.capabilities if isinstance(wf.capabilities, dict) else {}


def _params(wf: Workflow) -> dict:
    schema = wf.parsed_schema if isinstance(wf.parsed_schema, dict) else {}
    params = schema.get("params")
    return params if isinstance(params, dict) else {}


def _audio_block(wf: Workflow) -> dict:
    audio = _caps(wf).get("audio")
    return audio if isinstance(audio, dict) else {}


def _declared_voice_mode(wf: Workflow) -> str | None:
    mode = _audio_block(wf).get("voice_mode") or _caps(wf).get("voice_mode")
    return str(mode) if mode else None


def is_preset_workflow(wf: Workflow) -> bool:
    """是否为「preset speaker 直传」的 TTS 工作流（对应历史 voice_mode=preset）。"""
    mode = _declared_voice_mode(wf)
    if mode == "preset":
        return True
    if mode and mode != "preset":
        # 明确声明为克隆/其它模式，以声明为准，不再按参数猜
        return False
    keys = set(_params(wf).keys())
    return _TTS_VOICE_KEY in keys and _TTS_VOICE_REF_KEY not in keys


def _workflow_identity(wf: Workflow) -> set[str]:
    return {x for x in (wf.id, wf.name, getattr(wf, "alias", None)) if x}


def _library_rows(db: Session, wf: Workflow) -> list[VoiceLibrary]:
    rows = db.execute(
        select(VoiceLibrary)
        .where(VoiceLibrary.enabled.is_(True))
        .order_by(VoiceLibrary.sort_order, VoiceLibrary.created_at)
    ).scalars().all()
    if not rows:
        return []
    ident = _workflow_identity(wf)
    matched = [r for r in rows if (r.source_workflow or "") in ident]
    # source_workflow 未标注（或标注的别名已改）时退化为全库可用音色
    return matched or list(rows)


def speaker_entries(db: Session, wf: Workflow | None) -> list[dict]:
    """当前 TTS 工作流支持的 speaker 全量元数据（speaker/display_name/language/...）。"""
    if wf is None:
        return []
    caps = _caps(wf)
    audio = _audio_block(wf)
    legacy_values = caps.get("voice_values") or audio.get("voice_values") or []

    if legacy_values:
        langs = caps.get("voice_languages") or {}
        descs = caps.get("voice_descriptions") or {}
        genders = caps.get("voice_genders") or {}
        names = caps.get("voice_display_names") or {}
        default_langs = caps.get("languages") or []

        def _langs(name: str) -> list[str]:
            val = langs.get(name) or default_langs
            if isinstance(val, str):
                return [val]
            return list(val or [])

        return [{
            "speaker": str(name),
            "display_name": names.get(name) or str(name),
            "language": _langs(name),
            "description": descs.get(name) or "",
            "gender": genders.get(name) or "",
        } for name in legacy_values]

    param = _params(wf).get(_TTS_VOICE_KEY)
    if isinstance(param, dict):
        options = param.get("options") or param.get("enum") or param.get("values")
        if isinstance(options, list) and options:
            return [{
                "speaker": str(o),
                "display_name": str(o),
                "language": [],
                "description": "",
                "gender": "",
            } for o in options]

    return [{
        "speaker": r.speaker_id,
        "display_name": r.display_name or r.name or r.speaker_id,
        "language": [r.language] if r.language else [],
        "description": r.description or "",
        "gender": r.gender_presentation or "",
    } for r in _library_rows(db, wf) if r.speaker_id]


def workflow_speakers(db: Session, wf: Workflow | None) -> list[str]:
    """当前 TTS 工作流支持的 speaker_id 列表。"""
    seen: set[str] = set()
    out: list[str] = []
    for entry in speaker_entries(db, wf):
        sid = entry["speaker"]
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def list_tts_workflows(db: Session) -> list[Workflow]:
    return list(db.execute(
        select(Workflow)
        .where(Workflow.workflow_type == "tts")
        .order_by(Workflow.created_at)
    ).scalars().all())


def resolve_preset_workflow(db: Session) -> Workflow | None:
    """定位 preset TTS 工作流：优先可用 speaker 非空的，其次任意 preset。"""
    rows = list_tts_workflows(db)
    presets = [w for w in rows if is_preset_workflow(w)]
    for wf in presets:
        if workflow_speakers(db, wf):
            return wf
    return presets[0] if presets else None


def resolve_tts_workflow(db: Session, workflow_id: str | None = None) -> Workflow | None:
    """指定 workflow_id 时用指定项（仍要求存在），否则回落 preset 工作流。"""
    if workflow_id:
        wf = db.get(Workflow, workflow_id)
        if wf is not None:
            return wf
    return resolve_preset_workflow(db)
