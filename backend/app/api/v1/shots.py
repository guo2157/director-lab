"""镜头管理接口：API.md §5 镜头契约。

- 创建时校验 (scene_id, episode) 在 scene_episode 中存在，否则 422
- 修改提示词/时长/结构字段后按依赖矩阵标记相关产物 stale，不自动触发任务
- 修改 duration 超出 H3 范围（4–15 秒）时返回 warning 字段提示 duration_out_of_range，仍接受修改
- 详情含 consistency 与聚合素材（scene/character/props/current_assets）
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models import Asset, AssetRef, Character, DependencyRef, Job, Project, Prop, Scene, SceneEpisode, Shot
from app.models import ids
from app.schemas.storyboard import ShotCreate, ShotOrderUpdate, ShotOut, ShotUpdate
from app.services import errors
from app.services.shot_refs import build_shot_asset_manifest, inject_asset_refs

router = APIRouter(prefix="/api", tags=["shots"])


def _get_shot(db: Session, shot_id: str) -> Shot:
    s = db.get(Shot, shot_id)
    if s is None:
        raise errors.shot_not_found()
    return s


def _mark_downstream_stale(db: Session, shot_id: str, roles: list[str] | None = None) -> None:
    """按依赖矩阵将镜头下游产物标 stale（仅视频或全部，视修改字段而定）。"""
    refs = db.query(AssetRef).filter(AssetRef.shot_id == shot_id).all()
    for r in refs:
        if roles is not None and r.role not in roles:
            continue
        asset = db.get(Asset, r.asset_id)
        if asset is not None and asset.validity == "current":
            asset.validity = "stale"
            # 分镜图失效时确认自动回退（D-13）：需重新生成并重新确认
            if asset.asset_type == "keyframe":
                shot = db.get(Shot, shot_id)
                if shot is not None:
                    shot.keyframe_confirmed = False


def _current_asset_info(db: Session, shot_id: str, asset_type: str) -> dict | None:
    """取依赖版本匹配、validity=current 且来源任务 succeeded 的产物信息。"""
    row = (
        db.query(Asset)
        .join(Job, Job.id == Asset.source_job_id)
        .filter(
            Job.shot_id == shot_id,
            Asset.asset_type == asset_type,
            Asset.validity == "current",
            Job.status == "succeeded",
        )
        .order_by(Asset.version.desc())
        .first()
    )
    if row is None:
        return None
    # 替换上传是原地覆盖文件、asset_id 与路径都不变；不带版本参数时
    # 浏览器会继续用缓存里的旧图，页面看起来"替换没生效"
    stamp = int(row.updated_at.timestamp()) if row.updated_at else 0
    return {
        "asset_id": row.id,
        "file_path": row.file_path,
        "thumb_path": row.thumb_path,
        "version": row.version,
        "url": f"/api/assets/{row.id}/file?v={stamp}",
        "thumb_url": f"/api/assets/{row.id}/file?thumb=true&v={stamp}" if row.thumb_path else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        # 锁定状态：分镜图工作台据此显示锁定/解锁入口（锁定后禁止重新生成覆盖）
        "locked": bool(row.locked),
    }


@router.post("/shots", status_code=201)
def create_shot(payload: ShotCreate, db: Session = Depends(get_db)) -> ShotOut:
    scene = db.get(Scene, payload.scene_id)
    if scene is None:
        raise errors.scene_not_found()
    exists = (
        db.query(SceneEpisode)
        .filter(SceneEpisode.scene_id == payload.scene_id, SceneEpisode.episode == payload.episode)
        .count()
    )
    if exists == 0:
        raise errors.biz_error(code="invalid_request", http=422, message="场景在该集不存在出场记录，无法创建镜头")
    if payload.shot_kind in ("dialogue", "narration") and not payload.dialogue:
        raise errors.invalid_request(f"shot_kind 为 {payload.shot_kind} 时 dialogue 必填")
    max_seq = (
        db.query(Shot.seq)
        .filter(Shot.scene_id == payload.scene_id, Shot.episode == payload.episode)
        .order_by(Shot.seq.desc())
        .first()
    )
    seq = (max_seq[0] + 1) if max_seq else 1
    s = Shot(
        scene_id=payload.scene_id,
        episode=payload.episode,
        shot_kind=payload.shot_kind,
        dialogue=payload.dialogue,
        image_prompt=payload.image_prompt,
        video_prompt=payload.video_prompt,
        duration=payload.duration,
        character_id=payload.character_id,
        requires_prev_frame=payload.requires_prev_frame,
        seq=seq,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return ShotOut.model_validate(s)


@router.get("/shots/{shot_id}")
def get_shot(shot_id: str, db: Session = Depends(get_db)) -> dict:
    s = _get_shot(db, shot_id)
    scene = db.get(Scene, s.scene_id)
    character = db.get(Character, s.character_id) if s.character_id else None
    # 道具按镜头资产清单过滤：只返回该镜头实际涉及的道具（提示词提及或显式 @引用），
    # 避免项目全量道具灌入详情页
    manifest = build_shot_asset_manifest(db, s)
    manifest_props = set(manifest.get("props") or [])
    props = (
        [
            p
            for p in db.query(Prop)
            .filter(Prop.project_id == scene.project_id, Prop.anchor_asset_id.isnot(None))
            .all()
            if p.name in manifest_props
        ]
        if scene
        else []
    )
    out = ShotOut.model_validate(s).model_dump()
    out["consistency"] = {"score": None, "verdict": "not_applicable"}
    out["scene"] = {"id": scene.id, "seq": scene.seq, "title": scene.title, "episode": s.episode} if scene else None
    out["character"] = (
        {"id": character.id, "name": character.name, "appearance": character.appearance,
         "portrait_asset_id": character.portrait_asset_id}
        if character else None
    )
    out["props"] = [{"id": p.id, "name": p.name, "anchor_asset_id": p.anchor_asset_id} for p in props]
    # 实际生成用提示词：注入 @引用（含道具）后的增强版本，与任务提交链路一致
    out["effective_image_prompt"] = (
        inject_asset_refs(db, s, s.image_prompt) if s.image_prompt else None
    )
    out["current_assets"] = {
        "keyframe": _current_asset_info(db, shot_id, "keyframe"),
        "clip": _current_asset_info(db, shot_id, "clip"),
        "audio": _current_asset_info(db, shot_id, "audio"),
    }
    return out


@router.get("/projects/{project_id}/shots")
def list_project_shots(
    project_id: str,
    episode: int = Query(ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Project, project_id) is None:
        raise errors.project_not_found()
    q = (
        db.query(Shot, Scene, SceneEpisode)
        .join(Scene, Shot.scene_id == Scene.id)
        .join(SceneEpisode, (SceneEpisode.scene_id == Scene.id) & (SceneEpisode.episode == Shot.episode))
        .filter(Scene.project_id == project_id, Shot.episode == episode)
        .order_by(SceneEpisode.seq_in_episode, Shot.seq)
    )
    total = q.count()
    rows = q.offset((page - 1) * page_size).limit(page_size).all()
    items = []
    for shot, scene, _se in rows:
        d = ShotOut.model_validate(shot).model_dump()
        d["scene_title"] = scene.title
        d["scene_seq"] = scene.seq
        d["current_assets"] = {
            "keyframe": _current_asset_info(db, shot.id, "keyframe"),
            "clip": _current_asset_info(db, shot.id, "clip"),
            "audio": _current_asset_info(db, shot.id, "audio"),
        }
        items.append(d)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/scenes/{scene_id}/shots")
def list_scene_shots(
    scene_id: str,
    episode: int = Query(ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Scene, scene_id) is None:
        raise errors.scene_not_found()
    q = (
        db.query(Shot)
        .filter(Shot.scene_id == scene_id, Shot.episode == episode)
        .order_by(Shot.seq)
    )
    total = q.count()
    rows = q.offset((page - 1) * page_size).limit(page_size).all()
    items = []
    for s in rows:
        d = ShotOut.model_validate(s).model_dump()
        d["current_assets"] = {
            "keyframe": _current_asset_info(db, s.id, "keyframe"),
            "clip": _current_asset_info(db, s.id, "clip"),
            "audio": _current_asset_info(db, s.id, "audio"),
        }
        items.append(d)
    return {"items": items, "total": total, "page": page, "page_size": page_size}








@router.patch("/shots/{shot_id}")
def update_shot(shot_id: str, payload: ShotUpdate, db: Session = Depends(get_db)) -> dict:
    s = _get_shot(db, shot_id)
    data = payload.model_dump(exclude_unset=True)
    warning = None
    settings = get_settings()

    # duration 范围警示（H3 4–15 秒）
    if "duration" in data and data["duration"] is not None:
        d = data["duration"]
        if d < settings.h3_duration_min_seconds or d > settings.h3_duration_max_seconds:
            warning = "duration_out_of_range"

    # 失效传播：仅 video_prompt → 只影响视频；其余提示词/时长/结构字段 → 影响全部产物
    video_only = set(data.keys()) == {"video_prompt"}
    for k, v in data.items():
        setattr(s, k, v)
    if data:
        _mark_downstream_stale(db, shot_id, roles=["clip"] if video_only else None)

    db.commit()
    db.refresh(s)
    out = ShotOut.model_validate(s).model_dump()
    if warning:
        out["warning"] = warning
    return out


@router.delete("/shots/{shot_id}/assets/{asset_id}", status_code=204)
def unbind_asset_from_shot(shot_id: str, asset_id: str, db: Session = Depends(get_db)) -> None:
    _get_shot(db, shot_id)
    ref = (
        db.query(AssetRef)
        .filter(AssetRef.shot_id == shot_id, AssetRef.asset_id == asset_id)
        .first()
    )
    if ref is None:
        raise errors.invalid_request("该资产未绑定到此镜头")
    db.delete(ref)
    db.commit()


@router.delete("/shots/{shot_id}", status_code=204)
def delete_shot(shot_id: str, db: Session = Depends(get_db)) -> None:
    s = _get_shot(db, shot_id)
    db.query(AssetRef).filter(AssetRef.shot_id == shot_id).delete(synchronize_session=False)
    db.query(DependencyRef).filter(DependencyRef.target_id == shot_id).delete(synchronize_session=False)
    db.delete(s)
    db.commit()


@router.put("/scenes/{scene_id}/shots/order")
def reorder_shots(scene_id: str, payload: ShotOrderUpdate, db: Session = Depends(get_db)) -> dict:
    if db.get(Scene, scene_id) is None:
        raise errors.scene_not_found()
    shots = (
        db.query(Shot)
        .filter(Shot.scene_id == scene_id, Shot.episode == payload.episode)
        .all()
    )
    shot_map = {s.id: s for s in shots}
    for sid in payload.shot_ids:
        if sid not in shot_map:
            raise errors.invalid_request("shot_ids 含其他场景或其他集的镜头 ID")
    for idx, sid in enumerate(payload.shot_ids, start=1):
        shot_map[sid].seq = idx
    db.commit()
    return {"reordered": len(payload.shot_ids)}


@router.get("/shots/{shot_id}/reference-preview")
def get_shot_reference_preview(shot_id: str, db: Session = Depends(get_db)) -> dict:
    """分镜生成前的参考图预览（PROP_BINDING_PLAN P0）。

    调用 Reference Resolver（dry-run，不创建任务不写库），返回：
    - slots：本次将使用的参考图清单（语义槽位、asset_id、asset_type、来源、选择原因）
    - fallbacks：回退记录（无组合图/画外/不存在等场景的原因码）
    前端据此展示参考图与人工替换入口；不影响资产库原始图片。
    """
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise errors.shot_not_found()
    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        raise errors.scene_not_found()
    from app.services.reference_resolver import resolve_shot_references
    manifest = resolve_shot_references(db, shot.id, scene.project_id)
    return manifest.to_dict()
