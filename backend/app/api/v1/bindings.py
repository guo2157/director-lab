"""角色-道具绑定关系管理接口（PROP_BINDING_PLAN.md P1）。
- 绑定是关系数据：CRUD + 审核确认 + 失效（挂生命周期事件边界）
- 组合参考图生成：走现有资产图生成链路（workflow 复用），
  产物 asset_type=character_prop_binding，不进资产库一级分类
- 审核语义复用 pending/confirmed/rejected
"""
from datetime import datetime
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Asset, Character, CharacterPropBinding, Project, Prop
from app.services import errors

router = APIRouter(prefix="/api", tags=["prop-bindings"])


class BindingCreate(BaseModel):
    project_id: str
    character_id: str
    prop_id: str
    relation_type: str = Field(default="carried", pattern="^(carried|held|worn|shared)$")
    valid_from_event: str | None = None
    note: str | None = None


class BindingUpdate(BaseModel):
    relation_type: str | None = Field(default=None, pattern="^(carried|held|worn|shared)$")
    valid_until_event: str | None = None
    review_status: str | None = Field(default=None, pattern="^(confirmed|rejected)$")
    note: str | None = None


def _binding_out(b: CharacterPropBinding, db: Session) -> dict:
    char = db.get(Character, b.character_id)
    prop = db.get(Prop, b.prop_id)
    combo = db.get(Asset, b.anchor_asset_id) if b.anchor_asset_id else None
    return {
        "id": b.id,
        "project_id": b.project_id,
        "character_id": b.character_id,
        "character_name": char.name if char else None,
        "prop_id": b.prop_id,
        "prop_name": prop.name if prop else None,
        "relation_type": b.relation_type,
        "anchor_asset_id": b.anchor_asset_id,
        "combo_image_path": combo.file_path if combo else None,
        "valid_from_event": b.valid_from_event,
        "valid_until_event": b.valid_until_event,
        "review_status": b.review_status,
        "source": b.source,
        "note": b.note,
    }


@router.get("/projects/{project_id}/character-prop-bindings")
def list_bindings(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    rows = (
        db.query(CharacterPropBinding)
        .filter(CharacterPropBinding.project_id == project_id)
        .order_by(CharacterPropBinding.created_at.desc())
        .all()
    )
    return [_binding_out(b, db) for b in rows]


@router.post("/character-prop-bindings", status_code=201)
def create_binding(payload: BindingCreate, db: Session = Depends(get_db)) -> dict:
    if db.get(Project, payload.project_id) is None:
        raise errors.project_not_found()
    if db.get(Character, payload.character_id) is None:
        raise errors.character_not_found()
    if db.get(Prop, payload.prop_id) is None:
        raise errors.prop_not_found()
    # 同角色同道具同关系类型去重：已有未失效绑定时不重复创建
    dup = (
        db.query(CharacterPropBinding)
        .filter(
            CharacterPropBinding.project_id == payload.project_id,
            CharacterPropBinding.character_id == payload.character_id,
            CharacterPropBinding.prop_id == payload.prop_id,
            CharacterPropBinding.relation_type == payload.relation_type,
            CharacterPropBinding.valid_until_event.is_(None),
        )
        .first()
    )
    if dup is not None:
        raise errors.AppError("binding_exists", "相同绑定关系已存在且未失效", 409)
    b = CharacterPropBinding(
        project_id=payload.project_id,
        character_id=payload.character_id,
        prop_id=payload.prop_id,
        relation_type=payload.relation_type,
        valid_from_event=payload.valid_from_event,
        source="manual",
        note=payload.note,
        created_at=datetime.now(),
    )
    db.add(b)
    db.commit()
    return _binding_out(b, db)


@router.patch("/character-prop-bindings/{binding_id}")
def update_binding(binding_id: str, payload: BindingUpdate,
                   db: Session = Depends(get_db)) -> dict:
    b = db.get(CharacterPropBinding, binding_id)
    if b is None:
        raise errors.AppError("binding_not_found", "绑定关系不存在", 404)
    if payload.relation_type is not None:
        b.relation_type = payload.relation_type
    if payload.valid_until_event is not None:
        b.valid_until_event = payload.valid_until_event
    if payload.review_status is not None:
        b.review_status = payload.review_status
    if payload.note is not None:
        b.note = payload.note
    b.updated_at = datetime.now()
    db.commit()
    return _binding_out(b, db)


@router.get("/character-prop-bindings/{binding_id}")
def get_binding(binding_id: str, db: Session = Depends(get_db)) -> dict:
    b = db.get(CharacterPropBinding, binding_id)
    if b is None:
        raise errors.AppError("binding_not_found", "绑定关系不存在", 404)
    return _binding_out(b, db)


class ComboImageRequest(BaseModel):
    prompt: str | None = None
    workflow_id: str | None = None


def _compile_combo_prompt(db: Session, b: CharacterPropBinding) -> tuple[str, str]:
    """组合参考图提示词：双主体（角色+道具）+ 关系描述（relation_type 映射，通用词表）。"""
    char = db.get(Character, b.character_id)
    prop = db.get(Prop, b.prop_id)
    rel_desc = {
        "carried": f"{char.name}背着/驮着{prop.name}，{prop.name}稳固贴合在{char.name}背部",
        "held": f"{char.name}手持{prop.name}",
        "worn": f"{char.name}穿戴/佩戴着{prop.name}",
        "shared": f"多个角色共同使用{prop.name}（以{char.name}为主要持握者）",
    }[b.relation_type]
    prompt = (
        f"任务：角色与道具组合参考图（双主体，干净浅灰背景）。\n"
        f"参考图职责：<image2> 为 {char.name} 定妆图；<image3> 为 {prop.name} 定妆图。\n"
        f"目标画面：{rel_desc}；两者外观严格与各自参考图一致，比例自然。\n"
        f"必须保持：{char.name} 的毛色/服饰与 {prop.name} 的颜色/材质/形状/关键特征。\n"
        f"禁止：人物畸形、多余肢体、无关角色、文字水印。\n"
        f"输出：单张横版高清组合参考图，细节清晰。"
    )
    negative = "畸形、多余肢体、无关角色、文字、水印"
    return prompt, negative


@router.post("/character-prop-bindings/{binding_id}/combo-image", status_code=202)
def generate_combo_image(binding_id: str, payload: ComboImageRequest | None = None,
                         db: Session = Depends(get_db)) -> dict:
    """生成角色-道具组合参考图（PROP_BINDING_PLAN P1）。

    - 复用多参考图工作流（角色定妆图 + 道具锚定图为输入参考）
    - 产物 Asset 类型 character_prop_binding（关系附属视觉参考，不进资产库一级分类）
    - 走 regenerate 原地覆盖链路：已有组合图时刷新同文件，绑定关系不重建
    - 不重置公共资产审核（组合图非基础资产）
    """
    from app.models import ids as id_gen
    from app.models import Job, Workflow

    b = db.get(CharacterPropBinding, binding_id)
    if b is None:
        raise errors.AppError("binding_not_found", "绑定关系不存在", 404)
    char = db.get(Character, b.character_id)
    prop = db.get(Prop, b.prop_id)
    char_asset = (
        db.query(Asset).filter(
            Asset.id == char.portrait_asset_id, Asset.validity == "current"
        ).first() if char else None
    )
    prop_asset = (
        db.query(Asset).filter(
            Asset.id == prop.anchor_asset_id, Asset.validity == "current"
        ).first() if prop else None
    )
    if char_asset is None or prop_asset is None:
        raise errors.AppError(
            "reference_missing", "角色定妆图或道具锚定图缺失/失效，无法生成组合图", 409)

    payload = payload or ComboImageRequest()
    # 组合图 Asset：首次生成创建，后续生成原地覆盖（file_path 固定）
    combo = db.get(Asset, b.anchor_asset_id) if b.anchor_asset_id else None
    if combo is None:
        combo = Asset(
            project_id=b.project_id,
            asset_type="character_prop_binding",
            name=f"{char.name}-{b.relation_type}-{prop.name}-组合图",
            version=1,
            file_path=f"data/media/cpb_{b.id[-12:]}.png",
            validity="pending",
        )
        db.add(combo)
        db.flush()
        b.anchor_asset_id = combo.id

    prompt, negative = _compile_combo_prompt(db, b)
    if payload.prompt:
        prompt = payload.prompt
    wf = (
        db.query(Workflow).filter(Workflow.id == payload.workflow_id).first()
        if payload.workflow_id else None
    ) or db.query(Workflow).filter(
        Workflow.name == "wf_image_ref_qwen21_multi"
    ).first()
    if wf is None:
        raise errors.AppError("no_workflow", "未找到多参考图工作流", 409)

    params = {
        "prompt": prompt,
        "negative": negative,
        "__media__": {
            "ref_identity": char_asset.file_path,
            "ref_prop": prop_asset.file_path,
        },
    }
    job = Job(
        id=id_gen.job_id(),
        project_id=b.project_id,
        shot_id=None,
        job_type="generate_keyframe",
        workflow_id=wf.id,
        workflow_version=wf.version,
        params=params,
        status="queued",
        priority=100,
        created_at=datetime.now(),
        input_snapshot={
            "workflow_version": wf.version,
            "params": params,
            "generation_mode": "asset_regenerate",
            "regeneration": {
                "asset_id": combo.id,
                "asset_type": "character_prop_binding",
                "base_version": combo.version,
                "binding_id": b.id,
                "prompt_edited": bool(payload.prompt),
            },
        },
    )
    db.add(job)
    db.commit()
    return {
        "job_id": job.id,
        "status": job.status,
        "binding_id": b.id,
        "combo_asset_id": combo.id,
        "workflow_id": wf.id,
    }


# ---- Prop Visual Validator（P1）：分镜道具视觉校验 ----

@router.get("/shots/{shot_id}/prop-visual-check")
def check_shot_props(shot_id: str, db: Session = Depends(get_db)):
    """单镜头道具视觉校验：PASS/WARNING/FAIL + 检查明细 + 单张重做提示。"""
    from app.services.prop_validator import validate_shot
    return validate_shot(db, shot_id)


@router.get("/projects/{project_id}/prop-visual-check")
def check_project_props(project_id: str, episode: int | None = None,
                        db: Session = Depends(get_db)):
    """项目（可按集）批量道具视觉校验汇总。失败镜头按 redo_hint 走单张重做。"""
    from app.services.prop_validator import validate_project_shots
    return validate_project_shots(db, project_id, episode=episode)
