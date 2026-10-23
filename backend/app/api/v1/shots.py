"""镜头管理接口：API.md §5 镜头契约。

- 创建时校验 (scene_id, episode) 在 scene_episode 中存在，否则 422
- 修改提示词/时长/结构字段后按依赖矩阵标记相关产物 stale，不自动触发任务
- 修改 duration 超出 H3 范围（4–15 秒）时返回 warning 字段提示 duration_out_of_range，仍接受修改
- 详情含 consistency 与聚合素材（scene/character/props/current_assets）
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models import Asset, AssetRef, Character, DependencyRef, Job, Project, Prop, Scene, SceneEpisode, Shot
from app.schemas.storyboard import ShotCreate, ShotOrderUpdate, ShotOut, ShotUpdate
from app.services import errors

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


def _current_asset_info(db: Session, shot_id: str, asset_type: str) -> dict | None:
    """取依赖版本匹配、validity=current 且来源任务 succeeded 的产物信息。"""
    row = (
        db.query(Asset)
        .join(AssetRef, AssetRef.asset_id == Asset.id)
        .join(Job, Job.id == Asset.source_job_id)
        .filter(
            AssetRef.shot_id == shot_id,
            Asset.asset_type == asset_type,
            Asset.validity == "current",
            Job.status == "succeeded",
        )
        .order_by(Asset.version.desc())
        .first()
    )
    if row is None:
        return None
    return {"asset_id": row.id, "file_path": row.file_path, "thumb_path": row.thumb_path, "version": row.version}


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
    props = (
        db.query(Prop)
        .filter(Prop.project_id == scene.project_id, Prop.anchor_asset_id.isnot(None))
        .all()
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
    return {"items": [ShotOut.model_validate(s).model_dump() for s in rows], "total": total, "page": page, "page_size": page_size}


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
