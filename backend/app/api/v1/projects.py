"""项目管理接口：API.md §4 字段级契约。

- 创建返回 201，初始 status=draft
- 详情含派生字段 shot_total/shot_done/current_stage
- 删除为软删除；存在进行中任务返回 409
- 硬删除（purge）彻底清理关联数据与媒体文件，不可恢复；存在进行中任务返回 409
- 归档仅 active 可调用；取消归档仅 archived 可调用；其余 409 invalid_state_transition
"""
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.integrations import comfyui_pool
from app.models import Job, Project
from app.schemas.project import PageOut, ProjectCreate, ProjectOut, ProjectUpdate
from app.services import errors, style_config
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
    # 风格配置：以默认值为基线，合并用户自定义后统一规范化；未提供时保持默认。
    settings = style_config.default_style_config()
    if payload.style_config:
        merged = dict(settings)
        merged.update(payload.style_config)
        normalized, _ = style_config.validate_style_config(merged)
        settings.update(normalized)
    settings["style_config_version"] = style_config.STYLE_CONFIG_VERSION
    p = Project(
        name=payload.name,
        aspect_ratio=payload.aspect_ratio,
        creation_type=payload.creation_type,
        description=payload.description,
        settings=settings,
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


def _has_style_fields(payload: dict | None) -> bool:
    # 判断提交的 settings 是否包含风格相关字段（含旧键名 genre/audience/requirements）
    if not isinstance(payload, dict):
        return False
    keys = set(style_config.default_style_config()) | {"genre", "audience", "requirements"}
    return bool(keys & set(payload))


@router.get("/style-options")
def get_style_options() -> dict:
    # 风格配置可选项（展示名 / 说明 / 默认值），供前端渲染，避免前端硬编码
    return style_config.options_payload()


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
        if _has_style_fields(data["settings"]):
            normalized, _ = style_config.validate_style_config(merged)
            merged.update(normalized)
            merged["style_config_version"] = style_config.STYLE_CONFIG_VERSION
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


@router.get("/{project_id}/purge-preview")
def purge_project_preview(project_id: str, db: Session = Depends(get_db)) -> dict:
    """硬删除影响面预览：返回将被清理的关联数据量与阻塞状态。"""
    from app.services.project_purge import count_project_data, find_blocking_jobs

    p = _get_project(db, project_id)
    blocking = find_blocking_jobs(db, project_id)
    return {
        "project_id": p.id,
        "project_name": p.name,
        "blocking_jobs": blocking,
        "deletable": blocking == 0,
        "counts": count_project_data(db, project_id),
    }


@router.delete("/{project_id}/purge")
def purge_project(project_id: str, db: Session = Depends(get_db)) -> dict:
    """硬删除项目：彻底清理全部关联数据与媒体文件，不可恢复。

    与 DELETE /{project_id}（软删除，仅写 deleted_at）的本质区别：
    本接口真正删除数据库行与磁盘文件。
    存在 queued / running / submission_unknown / chained_wait 任务时返回 409。
    数据库事务提交成功后才删除媒体文件，避免回滚后文件已丢失。
    """
    from app.services.project_purge import count_project_data, find_blocking_jobs, remove_media_files
    from app.services.project_purge import purge_project as _purge

    p = _get_project(db, project_id)
    blocking = find_blocking_jobs(db, project_id)
    if blocking > 0:
        raise errors.invalid_state_transition(
            f"存在进行中的任务（{blocking} 个），禁止删除项目"
        )

    preview = count_project_data(db, project_id)
    try:
        result = _purge(db, project_id)
        db.commit()
    except Exception:
        db.rollback()
        raise

    media_root = Path(get_settings().media_dir)
    if not media_root.is_absolute():
        media_root = (Path.cwd() / media_root).resolve()
    media_stats = remove_media_files(result["media_paths"], media_root)

    return {
        "project_id": result["project_id"],
        "project_name": result["project_name"],
        "deleted": result["deleted"],
        "preview": preview,
        "media": media_stats,
    }


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

@router.get("/{project_id}/style-config")
def get_style_config(project_id: str, db: Session = Depends(get_db)) -> dict:
    # 读取项目风格配置：返回规范化配置、原始取值与校验问题
    p = _get_project(db, project_id)
    raw = dict(p.settings or {})
    cfg, issues = style_config.validate_style_config(raw)
    return {
        "project_id": p.id,
        "version": style_config.STYLE_CONFIG_VERSION,
        "config": cfg,
        "raw": {k: raw.get(k) for k in style_config.STYLE_FIELDS if k in raw},
        "issues": issues,
        "visual_style_label": style_config.VISUAL_STYLES[cfg["visual_style"]]["label"],
        "visual_style_prompt": style_config.VISUAL_STYLES[cfg["visual_style"]]["prompt"],
    }


@router.put("/{project_id}/style-config")
def update_style_config(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    # 保存项目风格配置：校验后落库；变更只影响后续任务，不自动重生成已审核资产
    p = _get_project(db, project_id)
    body = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    # 基于现有 settings 合并后再校验：未提交的风格字段沿用已保存值，
    # 避免 validate_style_config 从默认值起算导致未提交字段被默认值覆盖。
    merged = dict(p.settings or {})
    merged.update(body)
    cfg, issues = style_config.validate_style_config(merged)
    merged.update(cfg)
    merged["style_config_version"] = style_config.STYLE_CONFIG_VERSION
    p.settings = merged
    db.commit()
    db.refresh(p)
    return {
        "project_id": p.id,
        "version": style_config.STYLE_CONFIG_VERSION,
        "config": cfg,
        "issues": issues,
        "saved": True,
        "note": "配置仅影响后续任务，不会自动重生成已审核资产与已生成结果",
    }



# --- 项目级默认工作流配置（G2，v2 计划 §11.3 / §19） -------------------------
# 存放在 project.settings["default_workflows"]，不建新表（见审计附录 C.2 第 3 条）。
# 项目默认不能绕过能力校验：保存时校验类型，解析时由 matcher 校验能力。

@router.get("/{project_id}/workflow-config")
def get_workflow_config(project_id: str, db: Session = Depends(get_db)) -> dict:
    """读项目工作流配置：已配置的默认工作流 + 各槽位候选（含能力摘要与可用性）。

    候选列表按能力标记计算 `usable`，前端据此禁用不可用项并展示原因，
    避免用户选中一个缺依赖的工作流后到生成时才失败。
    """
    from app.models import Workflow
    from app.services.workflow import capabilities as caps_svc
    from app.services.workflow import matcher

    p = _get_project(db, project_id)
    defaults = matcher.project_defaults(db, project_id)

    rows = db.query(Workflow).order_by(Workflow.workflow_type, Workflow.name).all()
    candidates: dict[str, list[dict]] = {slot: [] for slot in matcher.SLOT_WF_TYPE}
    for wf in rows:
        caps = (
            caps_svc.normalize(wf.workflow_type, wf.definition,
                               capabilities=wf.capabilities or {}, trigger=wf.trigger or {})
            if wf.definition else dict(wf.capabilities or {})
        )
        entry = {
            "id": wf.id,
            "name": wf.name,
            "alias": wf.alias,
            "workflow_type": wf.workflow_type,
            "version": wf.version,
            "is_default": wf.is_default,
            "runtime_status": wf.runtime_status,
            "generation_modes": caps.get("generation_modes") or [],
            "modalities": caps.get("modalities") or {},
            "tier": (caps.get("source") or {}).get("tier"),
        }
        for slot, expected_type in matcher.SLOT_WF_TYPE.items():
            if wf.workflow_type != expected_type:
                continue
            ok, missing = caps_svc.capability_matches(
                caps, matcher.SLOT_REQUIRED.get(slot, {})
            )
            entry_slot = {**entry, "usable": ok, "missing": missing}
            # 资产图槽位额外约束（与 generate.py::_match_pure_asset_workflow 一致）：
            # 带参考图槽位的工作流在无参考图时会由模型自行补全主体，导致角色图混入
            # 无关人物、场景图混入人物道具。这类工作流对资产图永远不可用，前端不该
            # 让用户选中后到生成时才 422，因此在这里就标为不可用并说明原因。
            if slot == "image" and caps.get("reference_slots"):
                entry_slot["usable"] = False
                entry_slot["missing"] = [
                    *entry_slot["missing"],
                    "资产图只允许无参考图的纯文生图工作流",
                ]
                entry_slot["asset_purity_blocked"] = True
            candidates[slot].append(entry_slot)

    return {
        "project_id": p.id,
        "defaults": defaults,
        "slots": sorted(matcher.SLOT_WF_TYPE),
        "slot_required": matcher.SLOT_REQUIRED,
        "candidates": candidates,
    }


@router.put("/{project_id}/workflow-config")
def update_workflow_config(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """保存项目默认工作流。空字符串表示「恢复系统默认」，从映射中移除该槽位。"""
    from app.services.workflow import matcher

    body = payload.get("defaults") if isinstance(payload.get("defaults"), dict) else payload
    saved = matcher.save_project_defaults(db, project_id, body or {})
    return {
        "project_id": project_id,
        "defaults": saved,
        "saved": True,
        "note": "项目默认工作流不绕过能力校验；不兼容时生成接口会明确报错而非静默切换",
    }
