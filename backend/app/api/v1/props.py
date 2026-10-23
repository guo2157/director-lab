"""道具管理接口：API.md §5 道具契约。

- name 项目内唯一，重复 409
- 改名同步改写 shot.image_prompt/video_prompt 中的 @旧名 引用
- 被镜头引用时返回 409 asset_in_use（经 asset_ref 绑定关系判定）
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import AssetRef, Project, Prop, Scene, Shot
from app.schemas.assets_narrative import PropCreate, PropOut, PropUpdate
from app.services import errors

router = APIRouter(prefix="/api", tags=["props"])


def _get_prop(db: Session, prop_id: str) -> Prop:
    p = db.get(Prop, prop_id)
    if p is None:
        raise errors.prop_not_found()
    return p


def _rewrite_at_references(db: Session, project_id: str, old_name: str, new_name: str) -> None:
    shots = (
        db.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
        .all()
    )
    for s in shots:
        for field in ("image_prompt", "video_prompt"):
            text = getattr(s, field)
            if text and f"@{old_name}" in text:
                setattr(s, field, text.replace(f"@{old_name}", f"@{new_name}"))


@router.post("/props", status_code=201)
def create_prop(payload: PropCreate, db: Session = Depends(get_db)) -> PropOut:
    if db.get(Project, payload.project_id) is None:
        raise errors.project_not_found()
    dup = (
        db.query(Prop)
        .filter(Prop.project_id == payload.project_id, Prop.name == payload.name)
        .count()
    )
    if dup > 0:
        raise errors.invalid_state_transition(f"道具名 {payload.name} 在项目内已存在")
    p = Prop(**payload.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return PropOut.model_validate(p)


@router.get("/projects/{project_id}/props")
def list_props(project_id: str, db: Session = Depends(get_db)) -> list[PropOut]:
    if db.get(Project, project_id) is None:
        raise errors.project_not_found()
    rows = db.query(Prop).filter(Prop.project_id == project_id).order_by(Prop.created_at).all()
    return [PropOut.model_validate(p) for p in rows]


@router.patch("/props/{prop_id}")
def update_prop(prop_id: str, payload: PropUpdate, db: Session = Depends(get_db)) -> PropOut:
    p = _get_prop(db, prop_id)
    data = payload.model_dump(exclude_unset=True)
    old_name = p.name
    new_name = data.get("name")
    if new_name and new_name != old_name:
        dup = (
            db.query(Prop)
            .filter(Prop.project_id == p.project_id, Prop.name == new_name, Prop.id != p.id)
            .count()
        )
        if dup > 0:
            raise errors.invalid_state_transition(f"道具名 {new_name} 在项目内已存在")
    for k, v in data.items():
        setattr(p, k, v)
    if new_name and new_name != old_name:
        _rewrite_at_references(db, p.project_id, old_name, new_name)
    db.commit()
    db.refresh(p)
    return PropOut.model_validate(p)


@router.delete("/props/{prop_id}", status_code=204)
def delete_prop(prop_id: str, db: Session = Depends(get_db)) -> None:
    p = _get_prop(db, prop_id)
    used = (
        db.query(AssetRef)
        .filter(AssetRef.asset_id == p.anchor_asset_id)
        .count()
        if p.anchor_asset_id
        else 0
    )
    if used > 0:
        raise errors.asset_in_use("道具锚定图被镜头引用，需先解除引用")
    db.delete(p)
    db.commit()
