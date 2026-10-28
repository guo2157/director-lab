"""项目管理接口：API.md §4 字段级契约。

- 创建返回 201，初始 status=draft
- 详情含派生字段 shot_total/shot_done/current_stage
- 删除为软删除；存在进行中任务返回 409
- 归档仅 active 可调用；取消归档仅 archived 可调用；其余 409 invalid_state_transition
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.integrations import comfyui_pool
from app.models import Job, Project
from app.schemas.project import PageOut, ProjectCreate, ProjectOut, ProjectUpdate
from app.services import errors
from app.services.progress import derive_progress

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _to_out(db: Session, p: Project) -> ProjectOut:
    total, done, stage = derive_progress(db, p)
    out = ProjectOut.model_validate(p)
    out.shot_total = total
    out.shot_done = done
    out.current_stage = stage
    return out


def _check_runtime_config() -> None:
    """创建项目前校验语言模型与 ComfyUI 配置（未配置则阻断，引导用户先到设置页配置）。"""
    s = get_settings()
    # 语言模型：base_url + model + api_key 均需配置
    if not (s.llm_base_url and s.llm_model and s.llm_api_key):
        raise errors.AppError(
            "llm_not_configured",
            "语言模型未配置（API URL / API Key / 模型名称），请先在设置页面完成配置",
            409,
            reason="llm_not_configured",
        )
    # ComfyUI：Mock 模式跳过；否则需至少一个启用实例
    if not s.render_mock and not comfyui_pool.enabled_instances():
        raise errors.AppError(
            "comfyui_not_configured",
            "ComfyUI 未配置（无启用实例），请先在设置页面完成配置",
            409,
            reason="comfyui_not_configured",
        )


@router.post("", status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)) -> ProjectOut:
    _check_runtime_config()
    p = Project(
        name=payload.name,
        aspect_ratio=payload.aspect_ratio,
        creation_type=payload.creation_type,
        description=payload.description,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return _to_out(db, p)


@router.get("")
def list_projects(
    status: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PageOut:
    q = db.query(Project).filter(Project.deleted_at.is_(None))
    if status:
        q = q.filter(Project.status == status)
    if keyword:
        q = q.filter(Project.name.contains(keyword))
    total = q.count()
    items = q.order_by(Project.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return PageOut(items=[_to_out(db, p).model_dump() for p in items], total=total, page=page, page_size=page_size)


def _get_project(db: Session, project_id: str) -> Project:
    p = db.get(Project, project_id)
    if p is None or p.deleted_at is not None:
        raise errors.project_not_found()
    return p


@router.get("/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db)) -> ProjectOut:
    return _to_out(db, _get_project(db, project_id))


@router.patch("/{project_id}")
def update_project(project_id: str, payload: ProjectUpdate, db: Session = Depends(get_db)) -> ProjectOut:
    p = _get_project(db, project_id)
    data = payload.model_dump(exclude_unset=True)
    if "settings" in data and data["settings"] is not None:
        merged = dict(p.settings or {})
        merged.update(data["settings"])
        data["settings"] = merged
    for k, v in data.items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return _to_out(db, p)


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, db: Session = Depends(get_db)) -> Response:
    p = _get_project(db, project_id)
    running = (
        db.query(Job)
        .filter(Job.project_id == project_id, Job.status.in_(["queued", "running", "submission_unknown"]))
        .count()
    )
    if running > 0:
        raise errors.invalid_state_transition("存在进行中的任务，禁止删除项目")
    p.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return Response(status_code=204)


@router.post("/{project_id}/archive")
def archive_project(project_id: str, db: Session = Depends(get_db)) -> ProjectOut:
    p = _get_project(db, project_id)
    if p.status != "active":
        raise errors.invalid_state_transition("仅 active 状态的项目可归档")
    p.status = "archived"
    db.commit()
    db.refresh(p)
    return _to_out(db, p)


@router.post("/{project_id}/unarchive")
def unarchive_project(project_id: str, db: Session = Depends(get_db)) -> ProjectOut:
    p = _get_project(db, project_id)
    if p.status != "archived":
        raise errors.invalid_state_transition("仅 archived 状态的项目可取消归档")
    p.status = "active"
    db.commit()
    db.refresh(p)
    return _to_out(db, p)


@router.post("/{project_id}/assets-review")
def review_project_assets(project_id: str, payload: dict, db: Session = Depends(get_db)) -> ProjectOut:
    """公共资产人工审核闸口（D-23）：资产库出图后必须人工确认，才允许生成分镜图。

    payload.action：approve=审核通过（放行分镜图）/ reject=驳回（需返工修改或替换资产）。
    审核通过前要求角色定妆图、场景基准图、道具锚定图全部生成完成。
    """
    p = _get_project(db, project_id)
    action = payload.get("action")
    if action not in ("approve", "reject"):
        raise errors.AppError("invalid_request", "action 必须为 approve 或 reject", 400)
    if action == "approve":
        # 统一资产就绪门禁（D-23 强化）：审核通过前校验必需资产真实状态
        # （指针/版本有效性/文件存在/在途生成任务），不再只查指针非空
        from app.services.assets_gate import evaluate_assets_gate

        gate = evaluate_assets_gate(db, project_id)
        if not gate.ready and gate.reason_code in (
            "assets_incomplete",
            "assets_generating",
        ):
            raise errors.AppError(
                gate.reason_code,
                gate.message,
                409,
                blockers=[b.to_dict() for b in gate.blockers],
                gate_status=gate.status,
            )
    p.assets_review_status = "approved" if action == "approve" else "rejected"
    p.assets_reviewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(p)
    return _to_out(db, p)
