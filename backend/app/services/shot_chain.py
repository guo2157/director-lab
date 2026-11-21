"""镜头链尾帧依赖服务（P0 整改）。

设计约定（对应首尾帧依赖与跨镜头连续性整改）：
- 镜头顺序以 (episode, scene.seq, shot.seq) 为准，与调度/前端口径一致
- 第 N 镜的参考尾帧取自第 N-1 镜实际生成视频解码得到的最后一帧，
  不使用预估帧、分镜图或其他镜头的尾帧
- 尾帧落为 Asset(asset_type="last_frame")，由 shot.last_frame_asset_id 绑定；
  每镜只保留一张尾帧，重新生成时原地覆盖同一资产（同 id、同名、同文件路径），
  不再累积历史版本；命名统一为 sht_{项目内全局镜号}_lastframe
- 尾帧提取失败只记录事件，不阻断 clip 任务成功（由后续重试或人工处理）
- 依赖关系写入 DependencyRef，便于失效传播与排查
"""
import os

from sqlalchemy.orm import Session

from app.models import Asset, DependencyRef, Scene, Shot, ids


def ordered_episode_shots(db: Session, project_id: str, episode: int) -> list:
    """按集内镜头顺序返回：场景出场顺序 → 镜头 seq。"""
    return (
        db.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id, Shot.episode == episode)
        .order_by(Scene.seq, Shot.seq)
        .all()
    )


def shot_project_id(db: Session, shot: Shot) -> str:
    """取镜头所属项目 ID。"""
    scene = db.get(Scene, shot.scene_id)
    return scene.project_id if scene is not None else ""


def previous_shot(db: Session, shot: Shot) -> Shot | None:
    """返回同集内的前一个镜头；没有前序镜头时返回 None。"""
    project_id = shot_project_id(db, shot)
    if not project_id:
        return None
    prev = None
    for item in ordered_episode_shots(db, project_id, shot.episode):
        if item.id == shot.id:
            return prev
        prev = item
    return None


def shot_global_index(db: Session, shot: Shot) -> int:
    """镜头在项目内的全局序号（剧集 → 场景 → 镜次，从 1 开始）。"""
    project_id = shot_project_id(db, shot)
    if not project_id:
        return 0
    rows = (
        db.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
        .order_by(Shot.episode, Scene.seq, Shot.seq)
        .all()
    )
    for index, item in enumerate(rows, start=1):
        if item.id == shot.id:
            return index
    return 0


def last_frame_asset_name(db: Session, shot: Shot) -> str:
    """尾帧资产统一命名：sht_{项目内全局镜号}_lastframe（项目内唯一）。"""
    return f"sht_{shot_global_index(db, shot)}_lastframe"


def _normalize_path(path: str | None) -> str:
    """把历史数据里的 Windows 反斜杠路径转成当前平台可用路径。

    真实故障（2026-11-21）：库里 `asset.file_path` 大量是
    `ast_frames\\sht_1_lastframe.png`，在 Linux 上 `os.path.exists()` 恒为 False，
    于是 `resolve_prev_frame()` 永远返回 None —— 串行链式批量出视频时
    「上一镜尾帧 → 下一镜首帧」这条链**从来没接上过**，而任务照样成功，
    只是模型拿不到连续性参考。这种缺陷只能靠对账发现。
    """
    if not path:
        return ""
    return str(path).replace("\\", "/")


def resolve_prev_frame(db: Session, shot: Shot) -> dict | None:
    """解析当前镜头可用的上一镜实际尾帧。

    仅当前一个镜头的尾帧绑定存在、资产有效、文件可读时才返回；
    否则返回 None，由调用方决定拦截或降级，不用其他图片顶替。
    """
    prev = previous_shot(db, shot)
    if prev is None or not prev.last_frame_asset_id:
        return None
    asset = db.get(Asset, prev.last_frame_asset_id)
    if asset is None or asset.validity != "current" or not asset.file_path:
        return None
    path = _normalize_path(asset.file_path)
    if not os.path.exists(path):
        return None
    return {
        "file_path": path,
        "asset_id": asset.id,
        "version": asset.version,
        "asset_name": asset.name,
        "prev_shot_id": prev.id,
    }


def _last_frame_duplicates(db: Session, shot: Shot, project_id: str, keep_id: str) -> list:
    """找出同镜的其他尾帧资产（历史重复版本/旧命名），用于清理。"""
    from app.models import Job

    rows = (
        db.query(Asset)
        .filter(
            Asset.project_id == project_id,
            Asset.asset_type == "last_frame",
            Asset.id != keep_id,
        )
        .all()
    )
    result = []
    for row in rows:
        snap = row.input_snapshot or {}
        if snap.get("source_shot_id") == shot.id:
            result.append(row)
            continue
        job = db.get(Job, row.source_job_id) if row.source_job_id else None
        if job is not None and job.shot_id == shot.id:
            result.append(row)
    return result


def store_shot_last_frame(db: Session, shot: Shot, clip_asset: Asset, media_dir: str) -> dict:
    """从 clip 产物提取实际末帧并绑定到 shot.last_frame_asset_id。

    每镜只保留一张尾帧：已有则原地覆盖文件与元数据（同 id / 同名 / 同路径），
    没有才新建；同镜历史重复尾帧会被清理，资产库中每个镜头只有一张
    sht_{全局镜号}_lastframe，重新生成后可被工作台直接选中。
    成功返回 {ok: True, asset_id, name, file_path, frame_hash, timestamp, fallback_used}；
    失败返回 {ok: False, error}，调用方记录事件但不阻断任务成功。
    """
    from datetime import datetime, timezone

    from app.models import AssetRef, JobInputRef
    from app.services.frame_extract import extract_last_frame, validate_frame_image

    if clip_asset is None or not clip_asset.file_path:
        return {"ok": False, "error": "clip_asset_missing"}
    if not os.path.exists(clip_asset.file_path):
        return {"ok": False, "error": "clip_file_missing"}

    project_id = shot_project_id(db, shot)
    name = last_frame_asset_name(db, shot)
    frame_dir = os.path.join(media_dir, "last_frames")
    os.makedirs(frame_dir, exist_ok=True)
    frame_path = os.path.join(frame_dir, f"{name}.png")

    # 先写临时文件，成功后再原子替换，避免提取失败时破坏现有尾帧
    tmp_path = f"{frame_path}.tmp.png"
    result = extract_last_frame(clip_asset.file_path, tmp_path)
    if not result.ok or not result.frame_path:
        return {"ok": False, "error": result.error or "extract_failed"}
    valid, err = validate_frame_image(result.frame_path)
    if not valid:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return {"ok": False, "error": err or "frame_invalid"}
    os.replace(tmp_path, frame_path)

    # 复用本镜已有尾帧（优先指针绑定，其次同名资产），不存在才新建
    asset = db.get(Asset, shot.last_frame_asset_id) if shot.last_frame_asset_id else None
    if asset is not None and asset.asset_type != "last_frame":
        asset = None
    if asset is None:
        asset = (
            db.query(Asset)
            .filter(
                Asset.project_id == project_id,
                Asset.asset_type == "last_frame",
                Asset.name == name,
            )
            .first()
        )
    if asset is None:
        asset = Asset(
            id=ids.asset_id(),
            project_id=project_id,
            asset_type="last_frame",
            name=name,
            version=1,
            validity="current",
        )
        db.add(asset)
        db.flush()

    old_path = asset.file_path
    asset.name = name
    asset.file_path = frame_path
    asset.thumb_path = None
    asset.source_job_id = clip_asset.source_job_id
    asset.validity = "current"
    asset.version = 1
    asset.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    asset.input_snapshot = {
        "source_clip_asset_id": clip_asset.id,
        "source_clip_version": clip_asset.version,
        "source_shot_id": shot.id,
        "frame_timestamp": result.timestamp,
        "frame_hash": result.frame_hash,
        "video_duration": result.video_duration,
        "video_fps": result.video_fps,
        "fallback_used": result.fallback_used,
        "fallback_reason": result.fallback_reason,
    }
    shot.last_frame_asset_id = asset.id
    db.flush()

    # 旧尾帧文件若换了路径则删掉，避免 last_frames 目录里残留同一镜的多张图
    if old_path and old_path != frame_path and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass

    # 清理同镜历史重复尾帧，只保留当前这一张
    for dup in _last_frame_duplicates(db, shot, project_id, keep_id=asset.id):
        db.query(AssetRef).filter(AssetRef.asset_id == dup.id).delete(synchronize_session=False)
        db.query(JobInputRef).filter(JobInputRef.asset_id == dup.id).delete(synchronize_session=False)
        dup_path = dup.file_path
        db.delete(dup)
        if dup_path and dup_path != frame_path and os.path.exists(dup_path):
            try:
                os.remove(dup_path)
            except OSError:
                pass
    db.flush()

    db.add(
        DependencyRef(
            id=ids.dependency_ref_id(),
            source_type="asset",
            source_id=clip_asset.id,
            source_version=clip_asset.version,
            target_type="shot",
            target_id=shot.id,
            dependency_role="last_frame",
            validity="current",
        )
    )
    return {
        "ok": True,
        "asset_id": asset.id,
        "name": name,
        "file_path": frame_path,
        "frame_hash": result.frame_hash,
        "timestamp": result.timestamp,
        "fallback_used": result.fallback_used,
    }


def invalidate_chain_after_regenerate(
    db: Session, shot: Shot, *, reset_keyframe_confirmed: bool = False
) -> dict:
    """当前镜头重生成后的失效传播。

    - 不把本镜尾帧置 superseded：每镜只保留一张尾帧，重新生成成功后原地覆盖，
      生成失败时下游仍需用旧尾帧串联，不能出现链路断裂
    - keyframe 重做时分镜图会被重绘，需回退确认状态；clip 重做不改分镜图，
      不得让本镜关键帧确认失效，否则会把已确认镜头打回未确认造成误拦
    - 返回下游镜头清单供前端提示影响范围，不批量自动重生成
    """
    affected: list = []
    if reset_keyframe_confirmed:
        shot.keyframe_confirmed = False
    project_id = shot_project_id(db, shot)
    if project_id:
        hit = False
        for item in ordered_episode_shots(db, project_id, shot.episode):
            if item.id == shot.id:
                hit = True
                continue
            if hit:
                affected.append(item.id)
    return {
        "cleared_last_frame": False,
        "keyframe_confirmed_reset": bool(reset_keyframe_confirmed),
        "downstream_shot_ids": affected,
    }
