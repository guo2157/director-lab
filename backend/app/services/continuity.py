"""跨集连续性服务：管理集间末帧提取、首帧传递和版本失效。

核心流程（对应跨集连续性改造第四~七节）：
1. 合成成功后自动提取末帧并创建/更新 EpisodeContinuity 记录
2. 下一集首镜生成前校验连续性资产并注入 first_frame
3. 上一集重合成时使下游连续性资产失效（版本递增）
4. 依赖调度：无有效末帧时阻止依赖该帧的生成任务
"""
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Asset, Composition, EpisodeContinuity, Project, Shot, Scene
from app.models import ids
from app.services.frame_extract import extract_last_frame, validate_frame_image

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_or_create_continuity(
    db: Session, project_id: str, episode: int
) -> EpisodeContinuity:
    """获取或创建某集的连续性记录。"""
    rec = (
        db.query(EpisodeContinuity)
        .filter(
            EpisodeContinuity.project_id == project_id,
            EpisodeContinuity.episode == episode,
        )
        .first()
    )
    if rec is not None:
        return rec
    rec = EpisodeContinuity(
        id=ids.episode_continuity_id(),
        project_id=project_id,
        episode=episode,
        continuity_status="pending",
        continuity_version=1,
        created_at=_now(),
    )
    db.add(rec)
    db.flush()
    return rec


def get_episodes(db: Session, project_id: str) -> list[int]:
    """获取项目所有剧集号（升序）。"""
    rows = (
        db.query(Shot.episode)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
        .distinct()
        .order_by(Shot.episode)
        .all()
    )
    return [r[0] for r in rows]


def extract_episode_last_frame(
    db: Session,
    project_id: str,
    episode: int,
    composition: Composition,
    media_dir: Path,
) -> EpisodeContinuity:
    """合成成功后提取末帧并更新连续性记录。

    流程（对应第四节）：
    1. 确认视频真实存在且可被 FFprobe 解析
    2. 获取视频时长、帧率和流信息
    3. 提取最后一个有效帧（含黑屏回退）
    4. 验证图片可解码、非空、尺寸符合要求
    5. 记录视频路径、时间戳和校验值
    """
    rec = get_or_create_continuity(db, project_id, episode)
    rec.updated_at = _now()

    video_path = composition.file_path
    if not video_path or not os.path.exists(video_path):
        rec.continuity_status = "failed"
        rec.error_detail = f"合成视频文件不存在: {video_path}"
        db.commit()
        return rec

    frame_dir = media_dir / "continuity"
    frame_dir.mkdir(parents=True, exist_ok=True)
    frame_filename = f"ep{episode}_lastframe_v{rec.continuity_version}.png"
    frame_path = frame_dir / frame_filename

    result = extract_last_frame(str(video_path), str(frame_path))

    if not result.ok:
        rec.continuity_status = "failed"
        rec.error_detail = result.error or "末帧提取失败"
        db.commit()
        return rec

    valid, err = validate_frame_image(str(frame_path))
    if not valid:
        rec.continuity_status = "failed"
        rec.error_detail = f"末帧图片验证失败: {err}"
        db.commit()
        return rec

    rec.source_composition_id = composition.id
    rec.source_video_path = str(video_path)
    rec.source_frame_path = str(frame_path)
    rec.source_frame_hash = result.frame_hash
    rec.source_frame_timestamp = result.timestamp
    rec.source_video_duration = result.video_duration
    rec.source_video_fps = result.video_fps
    rec.fallback_used = "1" if result.fallback_used else "0"
    rec.fallback_reason = result.fallback_reason
    rec.continuity_status = "extracted"
    rec.error_detail = None
    db.commit()
    return rec


def invalidate_downstream_continuity(
    db: Session, project_id: str, episode: int
) -> list[EpisodeContinuity]:
    """某集重合成后，使下游剧集的连续性资产失效（版本递增）。

    对应第七节：如果第 N 集重新生成并改变了末帧，
    应使后续依赖该帧的连续性资产失效。
    同时重置当前集的连续性记录（旧末帧来自旧视频），
    后续 extract_episode_last_frame 会重新提取。
    """
    episodes = get_episodes(db, project_id)
    invalidated: list[EpisodeContinuity] = []

    # 重置当前集的连续性记录（旧末帧来自旧视频，不再有效）
    cur_rec = (
        db.query(EpisodeContinuity)
        .filter(
            EpisodeContinuity.project_id == project_id,
            EpisodeContinuity.episode == episode,
        )
        .first()
    )
    if cur_rec is not None and cur_rec.continuity_status in ("extracted", "passed"):
        cur_rec.continuity_status = "pending"
        cur_rec.source_frame_path = None
        cur_rec.source_frame_hash = None
        cur_rec.source_frame_timestamp = None
        cur_rec.verification_result = None
        cur_rec.error_detail = f"第 {episode} 集重合成，旧末帧已失效"
        cur_rec.updated_at = _now()
        invalidated.append(cur_rec)

    for ep in episodes:
        if ep <= episode:
            continue
        rec = (
            db.query(EpisodeContinuity)
            .filter(
                EpisodeContinuity.project_id == project_id,
                EpisodeContinuity.episode == ep,
            )
            .first()
        )
        if rec is not None and rec.continuity_status in ("extracted", "passed"):
            rec.continuity_status = "pending"
            rec.continuity_version += 1
            rec.source_frame_path = None
            rec.source_frame_hash = None
            rec.source_frame_timestamp = None
            rec.opening_frame_path = None
            rec.opening_frame_asset_id = None
            rec.verification_result = None
            rec.error_detail = f"上游第 {episode} 集重合成，连续性资产已失效"
            rec.updated_at = _now()
            invalidated.append(rec)

    if invalidated:
        db.commit()
    return invalidated


def check_continuity_for_episode(
    db: Session, project_id: str, episode: int
) -> tuple[bool, str | None, EpisodeContinuity | None]:
    """检查某集是否满足首帧衔接条件。

    返回 (可否生成, 错误信息, 连续性记录)。
    第 1 集无需检查（无上一集）。
    """
    if episode <= 1:
        return True, None, None

    prev_episode = episode - 1
    rec = (
        db.query(EpisodeContinuity)
        .filter(
            EpisodeContinuity.project_id == project_id,
            EpisodeContinuity.episode == prev_episode,
        )
        .first()
    )

    if rec is None:
        return False, f"上一集（第 {prev_episode} 集）的连续性记录不存在", None

    if rec.continuity_status not in ("extracted", "passed"):
        return False, f"上一集（第 {prev_episode} 集）末帧未提取成功（状态: {rec.continuity_status}）", rec

    if not rec.source_frame_path or not os.path.exists(rec.source_frame_path):
        return False, f"上一集末帧图片路径无效: {rec.source_frame_path}", rec

    valid, err = validate_frame_image(rec.source_frame_path)
    if not valid:
        return False, f"上一集末帧图片不可用: {err}", rec

    return True, None, rec


def get_first_frame_for_episode(
    db: Session, project_id: str, episode: int
) -> str | None:
    """获取某集应使用的首帧图片路径。

    返回 None 表示无需首帧约束（第 1 集或连续性不满足）。
    """
    ok, _, rec = check_continuity_for_episode(db, project_id, episode)
    if not ok or rec is None:
        return None
    return rec.source_frame_path


def record_opening_frame_usage(
    db: Session,
    project_id: str,
    episode: int,
    opening_frame_path: str,
    asset_id: str | None = None,
) -> None:
    """记录某集实际使用的首帧路径和资产 ID。"""
    rec = get_or_create_continuity(db, project_id, episode)
    rec.opening_frame_path = opening_frame_path
    rec.opening_frame_asset_id = asset_id
    rec.updated_at = _now()
    db.commit()


def record_verification_result(
    db: Session,
    project_id: str,
    episode: int,
    result: dict,
) -> None:
    """记录首帧一致性验证结果。"""
    rec = get_or_create_continuity(db, project_id, episode)
    rec.verification_result = result
    if result.get("passed"):
        rec.continuity_status = "passed"
    else:
        rec.continuity_status = "failed"
        rec.error_detail = result.get("error", "首帧一致性验证未通过")
    rec.updated_at = _now()
    db.commit()


def build_continuity_summary(
    db: Session,
    project_id: str,
    episode: int,
    shot: Shot | None = None,
) -> dict:
    """构建结构化跨集连续性摘要（对应第六节）。

    从实际末帧及剧本上下文获取，不硬编码。
    """
    prev_ep = episode - 1
    rec = (
        db.query(EpisodeContinuity)
        .filter(
            EpisodeContinuity.project_id == project_id,
            EpisodeContinuity.episode == prev_ep,
        )
        .first()
    )

    summary: dict = {
        "episode": episode,
        "previous_episode": prev_ep if prev_ep >= 1 else None,
        "scene": None,
        "characters": [],
        "props": [],
        "lighting": None,
        "next_episode_action": None,
        "fixed_constraints": [
            "角色外观、服装、道具和场景身份不发生无依据变化",
        ],
    }

    if rec is not None and rec.source_frame_path:
        summary["source_frame"] = rec.source_frame_path
        summary["source_frame_hash"] = rec.source_frame_hash
        summary["source_video_duration"] = rec.source_video_duration

    if shot is not None:
        scene = db.get(Scene, shot.scene_id)
        if scene is not None:
            summary["scene"] = scene.title
        summary["next_episode_action"] = shot.video_prompt or shot.image_prompt or None

    return summary
