"""TTS 参数构建（单一事实来源）。

为什么单独成模块：A4 依赖指纹要求「生成时写入的指纹」与「复用判定时重算的指纹」**逐字段同构**。
之前生成侧在 `api/v1/audio.py`、复用侧在 `worker/executor.py::_tts_params_for` 各写一份，
两处已经漂移：

- 生成侧不写 `tts_speed`，复用侧却从 `seg.speed` 补 → 只要片段设过 speed 就必然失配；
- 生成侧 `tts_lang` 取 `voice.language`，复用侧硬编码 `"Chinese"` → 非中文音色必然失配；
- 生成侧无 instruct 时回退 `voice.default_instruct`，复用侧漏了这一步 → 情绪字段必然失配。

三者叠加的后果是「几乎每个片段都被判为 stale」，指纹形同禁用。故合并为一份构建逻辑，
两侧都调用它，从机制上消除漂移（v2 §13.5）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audio import AudioSegment, VoiceProfile
from app.models.narrative import Character, Project


def resolve_voice(db: Session, seg: AudioSegment) -> VoiceProfile | None:
    """片段音色解析：片段覆盖 → 角色默认音色 → 兼容 Character.voice_id → 项目旁白音色。

    与 `api/v1/audio.py::_resolve_voice` 同序；旁白分支返回临时 VoiceProfile（不落库），
    只为统一后续字段读取。
    """
    if seg.voice_profile_id:
        vp = db.get(VoiceProfile, seg.voice_profile_id)
        if vp:
            return vp
    if seg.character_id:
        vp = db.execute(
            select(VoiceProfile).where(
                VoiceProfile.character_id == seg.character_id,
                VoiceProfile.is_default == True,  # noqa: E712
                VoiceProfile.enabled == True,  # noqa: E712
            )
        ).scalars().first()
        if vp:
            return vp
        ch = db.get(Character, seg.character_id)
        if ch and ch.voice_id:
            return VoiceProfile(voice_id=ch.voice_id, voice_name=ch.voice_id, language="Chinese")
    if seg.track_type == "narration":
        project = db.get(Project, seg.project_id)
        nv = ((project.settings or {}) if project else {}).get("narrator_voice") or {}
        if nv.get("speaker"):
            return VoiceProfile(
                voice_id=nv["speaker"],
                voice_name=nv.get("speaker", ""),
                language=nv.get("language", "Chinese"),
                default_instruct=nv.get("default_instruct", ""),
            )
    return None


def build_params(
    seg: AudioSegment,
    voice: VoiceProfile | None,
    *,
    instruct_override: str | None = None,
    emotion_override: str | None = None,
) -> dict:
    """构建影响音频内容的 TTS 参数（生成侧与复用侧共用）。

    优先级与历史行为严格保持一致（不改动真实生成语义）：
    - instruct：显式覆盖 → 片段 instruct
    - 情绪：显式 emotion 覆盖 → 片段 emotion → neutral；若音色配置了 default_instruct
      且片段没有 instruct，则以音色默认 instruct 为准（这是既有行为，此处只做统一，不顺手改）
    - 音色：片段/角色解析出的 speaker（固定，禁止随机）
    - 语言：音色 language，缺省 Chinese
    - 语速：仅当片段显式设置时注入；未设置不写键（避免与未声明该输入的工作流冲突）
    """
    params: dict = {"tts_text": seg.text}

    instruct = instruct_override or seg.instruct
    if instruct:
        params["tts_emotion"] = instruct
    else:
        params["tts_emotion"] = emotion_override or seg.emotion or "neutral"

    if voice is not None:
        params["tts_voice"] = voice.voice_id
        params["tts_lang"] = getattr(voice, "language", None) or "Chinese"
        if not instruct and getattr(voice, "default_instruct", ""):
            params["tts_emotion"] = voice.default_instruct

    if seg.speed is not None:
        params["tts_speed"] = seg.speed
    return params


def params_for_fingerprint(
    seg: AudioSegment,
    voice: VoiceProfile | None,
    *,
    instruct_override: str | None = None,
    emotion_override: str | None = None,
) -> dict:
    """复用判定用参数：与 `build_params` 完全同源，只去掉纯文本字段。

    `tts_text` 已由指纹的 `text` 单独覆盖，保留会让「台词相同但换行不同」也失配。
    """
    p = build_params(
        seg, voice, instruct_override=instruct_override, emotion_override=emotion_override
    )
    p.pop("tts_text", None)
    return p
