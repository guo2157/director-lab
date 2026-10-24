"""角色管理接口：API.md §5 角色契约。

- name 项目内唯一，重复 409
- 改名同步改写 shot.image_prompt/video_prompt 中的 @旧名 引用
- merge 规则：①镜头 character_id 与 dialogue_segments[].speaker_id 改指 target；
  ②提示词 @源名 改写为 @目标名；③源定妆图按目标是否已有定妆图分别处置；
  ④删除源角色，同一事务完成
- 路由顺序：merge 静态路径先于 {character_id} 动态路径（API.md §1 约定）
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Asset, AssetRef, Character, Project, Scene, Shot
from app.schemas.assets_narrative import CharacterCreate, CharacterMerge, CharacterOut, CharacterUpdate
from app.services import errors

router = APIRouter(prefix="/api", tags=["characters"])


def _get_character(db: Session, character_id: str) -> Character:
    c = db.get(Character, character_id)
    if c is None:
        raise errors.character_not_found()
    return c


def _project_shots(db: Session, project_id: str) -> list[Shot]:
    return (
        db.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
        .all()
    )


def _rewrite_at_references(db: Session, project_id: str, old_name: str, new_name: str) -> int:
    """同步改写项目内全部镜头提示词中的 @旧名 引用，返回受影响镜头数。"""
    affected = 0
    for s in _project_shots(db, project_id):
        changed = False
        for field in ("image_prompt", "video_prompt"):
            text = getattr(s, field)
            if text and f"@{old_name}" in text:
                setattr(s, field, text.replace(f"@{old_name}", f"@{new_name}"))
                changed = True
        if changed:
            affected += 1
    return affected


@router.post("/characters/merge")
def merge_characters(payload: CharacterMerge, db: Session = Depends(get_db)) -> dict:
    if payload.source_id == payload.target_id:
        raise errors.invalid_request("source_id 与 target_id 不能相同")
    source = db.get(Character, payload.source_id)
    target = db.get(Character, payload.target_id)
    if source is None or target is None:
        raise errors.character_not_found()
    if source.project_id != target.project_id:
        raise errors.invalid_request("仅支持同一项目内合并角色")

    affected = 0
    for s in _project_shots(db, source.project_id):
        changed = False
        if s.character_id == source.id:
            s.character_id = target.id
            changed = True
        if s.dialogue_segments:
            for seg in s.dialogue_segments:
                if isinstance(seg, dict) and seg.get("speaker_id") == source.id:
                    seg["speaker_id"] = target.id
                    seg["speaker"] = target.name
                    changed = True
        for field in ("image_prompt", "video_prompt"):
            text = getattr(s, field)
            if text and f"@{source.name}" in text:
                setattr(s, field, text.replace(f"@{source.name}", f"@{target.name}"))
                changed = True
        if changed:
            affected += 1

    # 源定妆图处置
    if source.portrait_asset_id:
        src_asset = db.get(Asset, source.portrait_asset_id)
        if target.portrait_asset_id:
            # 目标已有定妆图：源定妆图转为普通 character 资产保留，镜头引用改指目标定妆图
            if src_asset is not None:
                refs = db.query(AssetRef).filter(AssetRef.asset_id == src_asset.id).all()
                for r in refs:
                    r.asset_id = target.portrait_asset_id
                    tgt_asset = db.get(Asset, target.portrait_asset_id)
                    if tgt_asset is not None:
                        r.source_version = tgt_asset.version
        else:
            # 目标无定妆图：源定妆图转为目标定妆图
            target.portrait_asset_id = source.portrait_asset_id

    db.delete(source)
    db.commit()
    db.refresh(target)
    return {"character": CharacterOut.model_validate(target).model_dump(), "affected_shots": affected}


@router.post("/characters", status_code=201)
def create_character(payload: CharacterCreate, db: Session = Depends(get_db)) -> CharacterOut:
    if db.get(Project, payload.project_id) is None:
        raise errors.project_not_found()
    dup = (
        db.query(Character)
        .filter(Character.project_id == payload.project_id, Character.name == payload.name)
        .count()
    )
    if dup > 0:
        raise errors.invalid_state_transition(f"角色名 {payload.name} 在项目内已存在")
    # 同名校验（D-16 补充）：角色与场景名称不得相同，否则 @引用无法区分
    scene_dup = (
        db.query(Scene)
        .filter(Scene.project_id == payload.project_id, Scene.title == payload.name)
        .count()
    )
    if scene_dup > 0:
        raise errors.invalid_state_transition(f"名称 {payload.name} 已被项目内场景使用，角色与场景名称不能相同")
    c = Character(**payload.model_dump())
    db.add(c)
    db.commit()
    db.refresh(c)
    return CharacterOut.model_validate(c)


@router.get("/projects/{project_id}/characters")
def list_characters(project_id: str, db: Session = Depends(get_db)) -> list[CharacterOut]:
    if db.get(Project, project_id) is None:
        raise errors.project_not_found()
    rows = db.query(Character).filter(Character.project_id == project_id).order_by(Character.created_at).all()
    return [CharacterOut.model_validate(c) for c in rows]


@router.patch("/characters/{character_id}")
def update_character(character_id: str, payload: CharacterUpdate, db: Session = Depends(get_db)) -> CharacterOut:
    c = _get_character(db, character_id)
    data = payload.model_dump(exclude_unset=True)
    old_name = c.name
    new_name = data.get("name")
    if new_name and new_name != old_name:
        dup = (
            db.query(Character)
            .filter(Character.project_id == c.project_id, Character.name == new_name, Character.id != c.id)
            .count()
        )
        if dup > 0:
            raise errors.invalid_state_transition(f"角色名 {new_name} 在项目内已存在")
        # 同名校验（D-16 补充）：角色与场景名称不得相同
        scene_dup = (
            db.query(Scene)
            .filter(Scene.project_id == c.project_id, Scene.title == new_name)
            .count()
        )
        if scene_dup > 0:
            raise errors.invalid_state_transition(f"名称 {new_name} 已被项目内场景使用，角色与场景名称不能相同")
    for k, v in data.items():
        setattr(c, k, v)
    if new_name and new_name != old_name:
        _rewrite_at_references(db, c.project_id, old_name, new_name)
    db.commit()
    db.refresh(c)
    return CharacterOut.model_validate(c)


@router.delete("/characters/{character_id}", status_code=204)
def delete_character(character_id: str, db: Session = Depends(get_db)) -> None:
    c = _get_character(db, character_id)
    used = db.query(Shot).filter(Shot.character_id == character_id).count()
    if used > 0:
        raise errors.asset_in_use("角色被镜头引用，需先解除引用")
    db.delete(c)
    db.commit()
