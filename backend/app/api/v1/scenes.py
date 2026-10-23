"""场景管理接口：API.md §5 场景契约。

- 创建时自动写入一条 scene_episode 记录；seq 缺省追加项目末尾
- 追加集数重复时 409；响应含 requires_parse（超出已解析范围为 true）
- 移除最后一集拒绝；该集下存在镜头时 409 asset_in_use
- 删除场景级联其下镜头
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Project, Scene, SceneEpisode, Shot
from app.schemas.storyboard import SceneCreate, SceneEpisodeAdd, SceneOut, SceneUpdate
from app.services import errors

router = APIRouter(prefix="/api", tags=["scenes"])


def _get_scene(db: Session, scene_id: str) -> Scene:
    s = db.get(Scene, scene_id)
    if s is None:
        raise errors.scene_not_found()
    return s


def _to_out(db: Session, s: Scene) -> SceneOut:
    out = SceneOut.model_validate(s)
    eps = (
        db.query(SceneEpisode.episode)
        .filter(SceneEpisode.scene_id == s.id)
        .order_by(SceneEpisode.episode)
        .all()
    )
    out.episodes = [e[0] for e in eps]
    out.shot_count = db.query(Shot).filter(Shot.scene_id == s.id).count()
    return out


@router.post("/scenes", status_code=201)
def create_scene(payload: SceneCreate, db: Session = Depends(get_db)) -> SceneOut:
    if db.get(Project, payload.project_id) is None:
        raise errors.project_not_found()
    seq = payload.seq
    if seq is None:
        max_seq = (
            db.query(Scene.seq)
            .filter(Scene.project_id == payload.project_id)
            .order_by(Scene.seq.desc())
            .first()
        )
        seq = (max_seq[0] + 1) if max_seq else 1
    else:
        dup = (
            db.query(Scene)
            .filter(Scene.project_id == payload.project_id, Scene.seq == seq)
            .count()
        )
        if dup > 0:
            raise errors.invalid_state_transition(f"场景序号 {seq} 已被占用")
    s = Scene(
        project_id=payload.project_id,
        title=payload.title,
        description=payload.description,
        episode=payload.episode,
        seq=seq,
    )
    db.add(s)
    db.flush()
    db.add(SceneEpisode(scene_id=s.id, episode=payload.episode, seq_in_episode=1))
    db.commit()
    db.refresh(s)
    return _to_out(db, s)


@router.get("/projects/{project_id}/scenes")
def list_scenes(
    project_id: str,
    episode: int | None = Query(default=None, ge=1),
    include_shots: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> list[SceneOut]:
    if db.get(Project, project_id) is None:
        raise errors.project_not_found()
    q = db.query(Scene).filter(Scene.project_id == project_id)
    if episode is not None:
        scene_ids = select(SceneEpisode.scene_id).where(SceneEpisode.episode == episode)
        q = q.filter(Scene.id.in_(scene_ids))
    rows = q.order_by(Scene.seq).all()
    return [_to_out(db, s) for s in rows]


@router.patch("/scenes/{scene_id}")
def update_scene(scene_id: str, payload: SceneUpdate, db: Session = Depends(get_db)) -> SceneOut:
    s = _get_scene(db, scene_id)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(s, k, v)
    db.commit()
    db.refresh(s)
    return _to_out(db, s)


@router.delete("/scenes/{scene_id}", status_code=204)
def delete_scene(scene_id: str, db: Session = Depends(get_db)) -> None:
    s = _get_scene(db, scene_id)
    db.query(Shot).filter(Shot.scene_id == scene_id).delete(synchronize_session=False)
    db.query(SceneEpisode).filter(SceneEpisode.scene_id == scene_id).delete(synchronize_session=False)
    db.delete(s)
    db.commit()


@router.post("/scenes/{scene_id}/episodes", status_code=201)
def add_scene_episode(scene_id: str, payload: SceneEpisodeAdd, db: Session = Depends(get_db)) -> dict:
    s = _get_scene(db, scene_id)
    dup = (
        db.query(SceneEpisode)
        .filter(SceneEpisode.scene_id == scene_id, SceneEpisode.episode == payload.episode)
        .count()
    )
    if dup > 0:
        raise errors.invalid_state_transition(f"场景已存在于第 {payload.episode} 集")
    db.add(SceneEpisode(scene_id=scene_id, episode=payload.episode, seq_in_episode=payload.seq_in_episode))
    db.commit()
    # requires_parse：追加的集数超出当前已解析范围时为 true
    from app.models import ParseVersion
    current_pv = (
        db.query(ParseVersion)
        .filter(ParseVersion.project_id == s.project_id, ParseVersion.status == "current")
        .first()
    )
    requires_parse = True
    if current_pv and current_pv.diff_summary:
        parsed_episodes = current_pv.diff_summary.get("episodes", [])
        if payload.episode in parsed_episodes:
            requires_parse = False
    return {"scene": _to_out(db, s).model_dump(), "requires_parse": requires_parse}


@router.delete("/scenes/{scene_id}/episodes/{episode}", status_code=204)
def remove_scene_episode(scene_id: str, episode: int, db: Session = Depends(get_db)) -> None:
    s = _get_scene(db, scene_id)
    total = db.query(SceneEpisode).filter(SceneEpisode.scene_id == scene_id).count()
    if total <= 1:
        raise errors.invalid_request("场景仅剩一集出场，须改用删除场景接口")
    rec = (
        db.query(SceneEpisode)
        .filter(SceneEpisode.scene_id == scene_id, SceneEpisode.episode == episode)
        .first()
    )
    if rec is None:
        raise errors.scene_not_found()
    shots_in_ep = (
        db.query(Shot)
        .filter(Shot.scene_id == scene_id, Shot.episode == episode)
        .count()
    )
    if shots_in_ep > 0:
        raise errors.asset_in_use("该集下已存在镜头，需先删除该集镜头")
    db.delete(rec)
    db.commit()
