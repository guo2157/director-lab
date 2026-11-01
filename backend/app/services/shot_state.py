"""镜头完成度判定：必需产物齐全才置 done（PRD.md §6.7 镜头类型与音频必需性）。

修正背景（字幕链路阻断）：
    旧规则把「对白/旁白镜头必须有独立 audio 资产」当作 done 的必要条件，但 PRD
    明确音频"优先采用 H3 VA 同次生成的音轨"——H3 VA 的音轨是内嵌在 clip 里的，
    不会产生 audio 资产（TTS 只在音频缺失/无效时才补救）。结果对白镜头永远停在
    pending，合成只收 done/review 镜头，于是对白镜头永远进不了成片、字幕始终为空。

判定口径（与合成层 compose.ShotInput.audio_source 一致）：
    - 分镜图（keyframe）与视频（clip）为必需产物，且须来自 succeeded 任务、current 版本
    - dialogue/narration：H3 VA 内嵌音轨（audio_source == "h3_va" 且已有 clip）或独立 audio 资产
    - visual/transition：音频可选
"""
from sqlalchemy.orm import Session

from app.models import Asset, Job, Shot

_AUDIO_REQUIRED_KINDS = ("dialogue", "narration")


def has_current_asset(db: Session, shot_id: str, asset_type: str) -> bool:
    """镜头是否持有指定类型、当前有效的成功产物（与 ARCHITECTURE §4.2 口径一致）。"""
    return (
        db.query(Asset)
        .join(Job, Job.id == Asset.source_job_id)
        .filter(
            Job.shot_id == shot_id,
            Asset.asset_type == asset_type,
            Asset.validity == "current",
            Job.status == "succeeded",
        )
        .count()
        > 0
    )


def shot_audio_ready(db: Session, shot: Shot, has_clip: bool | None = None) -> bool:
    """音频是否满足该镜头类型的必需性要求。"""
    if shot.shot_kind not in _AUDIO_REQUIRED_KINDS:
        return True
    if shot.audio_source == "h3_va":
        clip_ok = has_current_asset(db, shot.id, "clip") if has_clip is None else has_clip
        if clip_ok:
            return True
    return has_current_asset(db, shot.id, "audio")


def shot_ready(db: Session, shot: Shot) -> bool:
    """必需产物齐全（分镜图 + 视频 + 按类型的音频）→ 可置 done。"""
    if not has_current_asset(db, shot.id, "keyframe"):
        return False
    has_clip = has_current_asset(db, shot.id, "clip")
    if not has_clip:
        return False
    return shot_audio_ready(db, shot, has_clip)
