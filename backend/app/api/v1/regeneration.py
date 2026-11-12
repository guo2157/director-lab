"""多图参考重新生成工作台 V1 接口。

- GET  /api/shots/{shot_id}/regenerate/config：从指定基础版本读取原始生成配置
- POST /api/shots/{shot_id}/regenerate/workbench：按三种模式提交重新生成任务（带幂等键）
  使用独立路径，避免与既有 POST /shots/{id}/regenerate 重做接口冲突
- GET  /api/jobs/{job_id}/snapshot：查看冻结的任务输入快照
- POST /api/jobs/{job_id}/retry：基于原快照重试
- GET  /api/assets/{asset_id}/candidates：候选版本列表
- POST /api/assets/{asset_id}/adopt：采用候选版本
- GET  /api/shots/{shot_id}/impact：更换尾帧/参考图后受影响的后续镜头

约束：重新生成结果一律先落候选版本（validity=candidate），采用后才切换为 current，
不覆盖原始文件、已审核版本或正在被其他任务使用的参考版本。
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Asset, AssetRef, Character, Job, JobInputRef, Project, Prop, Scene, Shot, Workflow, ids
from app.services import errors
from app.services import regeneration as regen

router = APIRouter(prefix="/api", tags=["regeneration"])

# target → 产物资产类型
_TARGET_ASSET_TYPE = {"keyframe": "keyframe", "clip": "clip"}
# target → 工作流类型
_TARGET_WF_TYPE = {"keyframe": "image", "clip": "video"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _get_shot(db: Session, shot_id: str) -> Shot:
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise errors.AppError("shot_not_found", "镜头不存在", 404)
    return shot


def _project_id_of(db: Session, shot: Shot) -> str:
    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        raise errors.AppError("scene_not_found", "场景不存在", 404)
    return scene.project_id


def _shot_ids_of_asset(db: Session, asset: Asset) -> list[str]:
    """取资产所属镜头：优先看手动绑定引用，其次按来源任务反查。

    自动生成流程不建立 AssetRef 记录，因此必须同时支持按来源任务反查，
    否则查询会漏掉全部自动生成的产物。
    """
    shot_ids = [r.shot_id for r in db.query(AssetRef).filter(AssetRef.asset_id == asset.id).all()]
    if not shot_ids and asset.source_job_id:
        src = db.get(Job, asset.source_job_id)
        if src is not None and src.shot_id:
            shot_ids = [src.shot_id]
    return shot_ids


def _sync_entity_pointer(db: Session, old: Asset, new: Asset) -> None:
    """采用候选后同步实体主图指针：角色定妆图 / 场景基准图 / 道具锚定图。

    仅当实体当前指针正好指向被替换的旧版本时才切换，
    避免误改用户已另行指定的其他版本。
    """
    if old.asset_type == "character":
        for ch in db.query(Character).filter(Character.portrait_asset_id == old.id).all():
            ch.portrait_asset_id = new.id
    elif old.asset_type == "scene":
        for sc in db.query(Scene).filter(Scene.baseline_asset_id == old.id).all():
            sc.baseline_asset_id = new.id
    elif old.asset_type == "prop":
        for pr in db.query(Prop).filter(Prop.anchor_asset_id == old.id).all():
            pr.anchor_asset_id = new.id


def _current_asset(db: Session, shot_id: str, asset_type: str) -> Asset | None:
    """取镜头当前生效的指定类型产物。

    关联口径与既有镜头详情保持一致：按来源任务的 shot_id 反查，
    不依赖自动生成流程不会建立的 AssetRef 记录。
    """
    # 判定口径与既有镜头详情严格一致：必须同时满足来源任务成功，
    # 否则存在失败任务时可能取到未真正产出的版本
    return (
        db.query(Asset)
        .join(Job, Job.id == Asset.source_job_id)
        .filter(
            Job.shot_id == shot_id,
            Job.status == "succeeded",
            Asset.asset_type == asset_type,
            Asset.validity == "current",
        )
        .order_by(Asset.version.desc())
        .first()
    )


def _resolve_base_asset(db: Session, shot: Shot, target: str, base_asset_id: str | None) -> Asset:
    if base_asset_id:
        asset = db.get(Asset, base_asset_id)
        if asset is None:
            raise errors.AppError("asset_not_found", f"基础版本资产 {base_asset_id} 不存在", 404)
        return asset
    asset_type = _TARGET_ASSET_TYPE.get(target)
    if asset_type is None:
        raise errors.AppError("invalid_request", f"不支持的目标：{target}", 400)
    asset = _current_asset(db, shot.id, asset_type)
    if asset is None:
        raise errors.AppError(
            "asset_not_found",
            "该镜头尚无当前生效产物，请先完成首次生成后再重新生成",
            404,
        )
    return asset


def _load_workflow(db: Session, workflow_id: str | None, target: str) -> Workflow:
    if workflow_id:
        wf = db.get(Workflow, workflow_id)
        if wf is None:
            raise errors.AppError("workflow_not_found", "工作流不存在", 404)
        return wf
    wf_type = _TARGET_WF_TYPE.get(target)
    if wf_type is None:
        raise errors.AppError("invalid_request", f"不支持的目标：{target}", 400)
    wf = (
        db.query(Workflow)
        .filter(Workflow.workflow_type == wf_type, Workflow.is_default == True)  # noqa: E712
        .first()
    )
    if wf is None:
        wf = (
            db.query(Workflow)
            .filter(Workflow.workflow_type == wf_type, Workflow.is_builtin == True)  # noqa: E712
            .first()
        )
    if wf is None:
        raise errors.AppError("no_matching_workflow", f"没有可用的 {wf_type} 工作流", 422)
    return wf


def _job_out(job: Job, snapshot: dict | None = None) -> dict:
    return {
        "id": job.id,
        "project_id": job.project_id,
        "shot_id": job.shot_id,
        "job_type": job.job_type,
        "status": job.status,
        "workflow_id": job.workflow_id,
        "workflow_version": job.workflow_version,
        "created_at": job.created_at.isoformat(),
        "effective_inputs": job.input_snapshot,
        "snapshot": snapshot,
    }


def _system_refs_for_clip(db: Session, shot: Shot) -> list[dict]:
    """clip 目标的系统参考：本镜分镜图（参考图语义）与上一镜尾帧（帧约束语义）。

    两类语义必须区分：ref_image 是本镜构图参考，ref_prev_frame 是上一镜尾帧约束。
    """
    from app.services.shot_chain import resolve_prev_frame

    refs: list[dict] = []
    keyframe = _current_asset(db, shot.id, "keyframe")
    if keyframe is not None and keyframe.file_path:
        refs.append({
            "slot_param": "ref_image",
            "input_key": "ref_images.ref_image_1",
            "asset_id": keyframe.id,
            "asset_version": keyframe.version,
            "asset_type": keyframe.asset_type,
            "role": "keyframe",
            "required": False,
            "file_path": keyframe.file_path,
            "semantic": "reference",
            "enabled": True,
        })
    frame = resolve_prev_frame(db, shot)
    if frame is not None:
        refs.append({
            "slot_param": "ref_prev_frame",
            "input_key": "ref_images.ref_image_0",
            "asset_id": frame["asset_id"],
            "asset_version": frame["version"],
            "asset_type": "frame",
            "role": "prev_frame",
            "required": bool(shot.requires_prev_frame),
            "file_path": frame["file_path"],
            "semantic": "frame_constraint",
            "enabled": True,
        })
    return refs


@router.get("/shots/{shot_id}/regenerate/config")
def get_regenerate_config(
    shot_id: str,
    target: str = "keyframe",
    base_asset_id: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """读取指定基础版本的原始生成配置，默认模式为『沿用原配置，只修改提示词』。"""
    shot = _get_shot(db, shot_id)
    if target not in _TARGET_ASSET_TYPE:
        raise errors.AppError("invalid_request", f"不支持的目标：{target}", 400)
    base_asset = _resolve_base_asset(db, shot, target, base_asset_id)
    config = regen.build_base_config(db, base_asset)

    if target == "clip":
        existing = {r["slot_param"] for r in config["refs"]}
        for ref in _system_refs_for_clip(db, shot):
            if ref["slot_param"] not in existing:
                config["refs"].append(ref)

    # 默认模式仅允许改提示词，其余选项在界面上置灰并给出原因
    config["editable"] = {
        "prompt": True,
        "negative_prompt": bool(config["negative_prompt"]["supported"]),
        "refs": False,
        "params": False,
        "workflow": False,
    }
    config["modes"] = list(regen.VALID_MODES)
    return config


@router.post("/shots/{shot_id}/regenerate/workbench", status_code=202)
def post_regenerate(shot_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """按指定模式提交重新生成任务，结果先落候选版本。"""
    shot = _get_shot(db, shot_id)
    target = payload.get("target", "keyframe")
    if target not in _TARGET_ASSET_TYPE:
        raise errors.AppError("invalid_request", f"不支持的目标：{target}", 400)
    mode = payload.get("mode") or regen.MODE_INHERIT_PROMPT_ONLY
    if mode not in regen.VALID_MODES:
        raise errors.AppError("invalid_request", f"不支持的重新生成模式：{mode}", 400)

    idempotency_key = payload.get("idempotency_key")
    if idempotency_key:
        exist = db.query(Job).filter(Job.request_key == idempotency_key).first()
        if exist is not None:
            return _job_out(exist, (exist.input_snapshot or {}).get("snapshot"))

    project_id = _project_id_of(db, shot)
    base_asset = _resolve_base_asset(db, shot, target, payload.get("base_asset_id"))
    base_config = regen.build_base_config(db, base_asset)

    workflow = None
    if payload.get("workflow_id"):
        if mode != regen.MODE_FULL_RECONFIGURE:
            raise errors.AppError(
                "invalid_request",
                "仅『完整重新配置』模式允许更换工作流",
                422,
            )
        workflow = _load_workflow(db, payload.get("workflow_id"), target)
    else:
        wf_id = (base_config.get("workflow") or {}).get("id")
        workflow = db.get(Workflow, wf_id) if wf_id else None
        if workflow is None:
            workflow = _load_workflow(db, None, target)

    prompt = payload.get("prompt")
    if prompt is None:
        prompt = (base_config.get("prompt") or {}).get("rendered") or ""
    negative = payload.get("negative_prompt")
    if negative is None:
        negative = (base_config.get("negative_prompt") or {}).get("rendered") or ""

    refs = payload.get("refs")
    if target == "clip":
        system_refs = _system_refs_for_clip(db, shot)
        if mode == regen.MODE_INHERIT_PROMPT_ONLY:
            refs = list(base_config.get("refs") or []) + [
                r for r in system_refs
                if r["slot_param"] not in {x["slot_param"] for x in (base_config.get("refs") or [])}
            ]
        elif refs is not None:
            provided = {r.get("slot_param") for r in refs}
            refs = list(refs) + [r for r in system_refs if r["slot_param"] not in provided]

    snapshot = regen.assemble_snapshot(
        db,
        mode=mode,
        base_config=base_config,
        prompt=prompt,
        negative_prompt=negative,
        refs=refs,
        params=payload.get("params") if mode == regen.MODE_FULL_RECONFIGURE else None,
        workflow=workflow if mode == regen.MODE_FULL_RECONFIGURE else None,
    )

    # 一致性校验门禁：提示词引用的图片编号没有对应启用参考图时阻断生成（422），
    # 警告（已注入未引用）不阻断，随快照返回供前端提示用户确认
    consistency = snapshot.get("consistency") or {}
    if consistency.get("errors"):
        raise errors.AppError(
            "prompt_ref_mismatch",
            "提示词与参考图不一致，已阻止生成：" + "；".join(
                e.get("message") or str(e) for e in consistency["errors"]
            ),
            422,
            consistency=consistency,
        )

    # 组装配送参数：媒体参数严格来自快照，确保提交内容与快照一致
    params = dict(snapshot.get("model_params") or {})
    params["prompt"] = snapshot["prompt"]["rendered"]
    if snapshot["negative_prompt"]["rendered"]:
        params["negative"] = snapshot["negative_prompt"]["rendered"]
    media = regen.snapshot_to_media(snapshot)
    if media:
        params["__media__"] = media

    job_type = "generate_keyframe" if target == "keyframe" else f"generate_{target}"
    job = Job(
        id=ids.job_id(),
        project_id=project_id,
        shot_id=shot.id,
        job_type=job_type,
        workflow_id=workflow.id,
        workflow_version=workflow.version,
        params=params,
        status="queued",
        request_key=idempotency_key,
        created_at=_now(),
        input_snapshot={
            "snapshot_version": 1,
            "mode": mode,
            "snapshot": snapshot,
            "workflow_version": workflow.version,
            "params": params,
            "generation_mode": mode,
            "base_asset_id": base_asset.id,
            "base_asset_version": base_asset.version,
            "regeneration": True,
        },
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        # 并发提交竞态：两个请求同时通过前置查重，第二个会在唯一索引上冲突。
        # 不能直接抛出让用户看到 500，应回滚后按幂等键复用已落库任务；
        # 确认不是同一幂等键时返回可读的 409 冲突提示。
        db.rollback()
        exist = (
            db.query(Job).filter(Job.request_key == idempotency_key).first()
            if idempotency_key
            else None
        )
        if exist is not None:
            return _job_out(exist, (exist.input_snapshot or {}).get("snapshot"))
        raise errors.AppError(
            "job_conflict",
            "该提交与另一个并发请求冲突，请刷新页面后重试",
            409,
        )
    regen.write_input_refs(db, job, snapshot)

    # clip 目标补齐系统注入参数（尾帧/首帧/尺寸/帧数），与既有生成链路保持一致
    if target == "clip":
        from app.api.v1.generate import _inject_prev_shot_frame
        info = _inject_prev_shot_frame(db, shot, target, params)
        _snap = dict(job.input_snapshot or {})
        _snap["prev_frame"] = info
        _snap["injected_media"] = dict(params.get("__media__") or {})
        job.input_snapshot = _snap
        job.params = params

    db.commit()
    return _job_out(job, snapshot)


@router.post("/shots/{shot_id}/regenerate/validate")
def post_validate(shot_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """提交前预检：按当前提示词与参考图配置执行完整校验，不创建任务。

    返回 refs 校验（槽位/资产/类型）与提示词一致性校验（图片编号引用）的
    合并结果；errors 非空时前端应阻止提交，warnings 需用户确认后允许提交。
    """
    shot = _get_shot(db, shot_id)
    target = payload.get("target", "keyframe")
    if target not in _TARGET_ASSET_TYPE:
        raise errors.AppError("invalid_request", f"不支持的目标：{target}", 400)
    mode = payload.get("mode") or regen.MODE_INHERIT_PROMPT_ONLY
    if mode not in regen.VALID_MODES:
        raise errors.AppError("invalid_request", f"不支持的重新生成模式：{mode}", 400)
    project_id = _project_id_of(db, shot)
    base_asset = _resolve_base_asset(db, shot, target, payload.get("base_asset_id"))
    base_config = regen.build_base_config(db, base_asset)
    workflow = None
    if payload.get("workflow_id"):
        workflow = _load_workflow(db, payload.get("workflow_id"), target)
    else:
        wf_id = (base_config.get("workflow") or {}).get("id")
        workflow = db.get(Workflow, wf_id) if wf_id else None
        if workflow is None:
            workflow = _load_workflow(db, None, target)
    prompt = payload.get("prompt")
    if prompt is None:
        prompt = (base_config.get("prompt") or {}).get("rendered") or ""
    refs = payload.get("refs")
    if target == "clip":
        system_refs = _system_refs_for_clip(db, shot)
        if refs is not None:
            provided = {r.get("slot_param") for r in refs}
            refs = list(refs) + [r for r in system_refs if r["slot_param"] not in provided]
    # refs 校验失败（槽位/资产/类型）直接返回错误，不进入一致性校验
    try:
        slots = (
            regen.normalize_reference_slots(workflow.capabilities)
            if workflow is not None
            else list(base_config.get("available_slots") or [])
        )
        final_refs = (
            (base_config.get("refs") or []) if refs is None else refs
        )
        validated = regen.validate_refs(db, slots, final_refs)
    except HTTPException as e:
        detail = getattr(e, "detail", None) or {}
        return {
            "valid": False,
            "errors": [{"code": detail.get("code") or "invalid_request",
                        "message": detail.get("message") or str(e)}],
            "warnings": [],
            "checks": [],
        }
    consistency = regen.validate_prompt_consistency(prompt, validated, slots)
    return {
        "valid": consistency["valid"],
        "errors": consistency["errors"],
        "warnings": consistency["warnings"],
        "checks": consistency["checks"],
    }


@router.get("/jobs/{job_id}/snapshot")
def get_job_snapshot(job_id: str, db: Session = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise errors.AppError("job_not_found", "任务不存在", 404)
    snap = job.input_snapshot or {}
    refs = [
        {
            "slot_param": r.slot_param,
            "input_key": r.input_key,
            "asset_id": r.asset_id,
            "asset_version": r.asset_version,
            "asset_type": r.asset_type,
            "role": r.role,
            "required": bool(r.required),
            "semantic": r.semantic,
            "enabled": bool(r.enabled),
            "file_path": r.file_path,
        }
        for r in db.query(JobInputRef)
        .filter(JobInputRef.job_id == job.id)
        .order_by(JobInputRef.seq)
        .all()
    ]
    return {
        "job_id": job.id,
        "status": job.status,
        "snapshot": snap.get("snapshot"),
        "refs": refs,
        "frozen": True,
    }


@router.get("/assets/{asset_id}/candidates")
def list_candidates(asset_id: str, db: Session = Depends(get_db)) -> dict:
    """列出与该资产同镜头同类型的候选版本，不包含已生效与已废弃版本。"""
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise errors.AppError("asset_not_found", "资产不存在", 404)
    shot_ids = _shot_ids_of_asset(db, asset)
    q = db.query(Asset).filter(Asset.asset_type == asset.asset_type, Asset.validity == "candidate")
    if shot_ids:
        q = q.join(Job, Job.id == Asset.source_job_id).filter(Job.shot_id.in_(shot_ids))
    else:
        q = q.filter(Asset.project_id == asset.project_id, Asset.name == asset.name)
    rows = q.order_by(Asset.version.desc()).all()
    return {
        "asset_id": asset.id,
        "current": "current",
        "candidates": [
            {
                "asset_id": a.id,
                "version": a.version,
                "file_path": a.file_path,
                "thumb_path": a.thumb_path,
                "source_job_id": a.source_job_id,
                "created_at": a.created_at.isoformat() if getattr(a, "created_at", None) else None,
            }
            for a in rows
        ],
    }


@router.post("/assets/{asset_id}/adopt")
def adopt_candidate(asset_id: str, db: Session = Depends(get_db)) -> dict:
    """采用候选版本：原生效版本转历史，目标候选转生效，并同步实体引用指针。"""
    target = db.get(Asset, asset_id)
    if target is None:
        raise errors.AppError("asset_not_found", "资产不存在", 404)
    if target.validity != "candidate":
        raise errors.AppError(
            "invalid_state_transition",
            f"只有候选版本可以采，当前状态：{target.validity}",
            409,
        )
    shot_ids = _shot_ids_of_asset(db, target)
    superseded: list[str] = []
    if shot_ids:
        # 自动生成流程不建立 AssetRef，必须按来源任务反查同镜头同类型的当前版本
        olds = (
            db.query(Asset)
            .join(Job, Job.id == Asset.source_job_id)
            .filter(
                Job.shot_id.in_(shot_ids),
                Asset.asset_type == target.asset_type,
                Asset.validity == "current",
                Asset.id != target.id,
            )
            .all()
        )
        # 兼容手动绑定建立的引用记录
        for _r in db.query(AssetRef).filter(AssetRef.shot_id.in_(shot_ids)).all():
            _a = db.get(Asset, _r.asset_id)
            if (
                _a is not None
                and _a.id != target.id
                and _a.asset_type == target.asset_type
                and _a.validity == "current"
                and all(_a.id != x.id for x in olds)
            ):
                olds.append(_a)
    else:
        # 实体类资产（角色定妆图 / 场景基准图 / 道具锚定图）没有镜头归属，
        # 必须按项目 + 类型 + 名称定位旧版本，否则会出现两个 current 同时生效
        olds = (
            db.query(Asset)
            .filter(
                Asset.project_id == target.project_id,
                Asset.asset_type == target.asset_type,
                Asset.name == target.name,
                Asset.validity == "current",
                Asset.id != target.id,
            )
            .all()
        )

    for old in olds:
        old.validity = "superseded"
        superseded.append(old.id)
        # 实体主图指针同步：定妆图/基准图/锚定图采用后不应仍指向旧版本
        _sync_entity_pointer(db, old, target)

    target.validity = "current"
    target.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

    # 人工审核闸口（D-23）：实体类公共资产换版后原审核结论失效，需重新提交审核
    if target.asset_type in ("character", "scene", "prop"):
        from app.services.assets_review import reset_assets_review
        reset_assets_review(db, target.project_id)

    # 同步实体主引用指针，避免详情页仍指向旧版本
    for shot_id in shot_ids:
        shot = db.get(Shot, shot_id)
        if shot is None:
            continue
        if target.asset_type == "keyframe":
            shot.keyframe_confirmed = False
        if target.asset_type == "frame":
            shot.last_frame_asset_id = target.id

    db.commit()
    return {
        "adopted_asset_id": target.id,
        "version": target.version,
        "superseded": superseded,
        "validity": target.validity,
    }


@router.get("/shots/{shot_id}/impact")
def get_impact(shot_id: str, db: Session = Depends(get_db)) -> dict:
    """更换上一镜尾帧或本镜参考图后，返回受影响的下游镜头清单（只标记，不自动重生成）。"""
    shot = _get_shot(db, shot_id)
    from app.services.shot_chain import ordered_episode_shots, shot_project_id

    project_id = shot_project_id(db, shot)
    affected: list[dict] = []
    hit = False
    if project_id:
        for item in ordered_episode_shots(db, project_id, shot.episode):
            if item.id == shot.id:
                hit = True
                continue
            if hit:
                affected.append({
                    "shot_id": item.id,
                    "seq": item.seq,
                    "episode": item.episode,
                    "status": item.status,
                    "requires_prev_frame": item.requires_prev_frame,
                })
    return {
        "shot_id": shot.id,
        "affected_shot_ids": [a["shot_id"] for a in affected],
        "affected": affected,
        "auto_regenerate": False,
    }


# ============================================================
# 重新生成工作台：多参考图 / 身份绑定 / 空间布局 / 动作时间轴
# 分层提示词 / 连续性校验 / 参数快照
# ============================================================

def _ws_refs_to_slots(references: list[dict]) -> list[dict]:
    """把工作台结构化参考图转换成快照 refs（槽位级）。

    工作台的 slot_id 即工作流槽位参数名；没有 asset_id 的槽位不进入快照，
    未启用的槽位保留但会被 validate_refs 过滤。
    """
    out: list[dict] = []
    for r in references or []:
        if not isinstance(r, dict):
            continue
        slot = r.get("slot_id")
        if not slot:
            continue
        ref: dict = {"slot_param": slot, "enabled": bool(r.get("enabled", True))}
        if r.get("asset_id"):
            ref["asset_id"] = r["asset_id"]
        out.append(ref)
    return out


def _ws_load_workflow(db: Session, shot: Shot, target: str, payload: dict, base_config: dict):
    """确定工作台使用的工作流：显式指定优先，否则沿用基础快照，最后回退默认。"""
    if payload.get("workflow_id"):
        return _load_workflow(db, payload.get("workflow_id"), target)
    wf_id = (base_config.get("workflow") or {}).get("id")
    workflow = db.get(Workflow, wf_id) if wf_id else None
    if workflow is None:
        workflow = _load_workflow(db, None, target)
    return workflow


def _ws_upsert_config(
    db: Session,
    *,
    shot: Shot,
    project_id: str,
    target: str,
    workflow: Workflow | None,
    payload: dict,
) -> "StoryboardRegenConfig":
    """保存或更新工作台草稿；只写可编辑副本，不触碰历史视频与既有资产。"""
    from app.models.regen_config import StoryboardRegenConfig

    row = (
        db.query(StoryboardRegenConfig)
        .filter(StoryboardRegenConfig.shot_id == shot.id)
        .filter(StoryboardRegenConfig.target == target)
        .first()
    )
    now = _now()
    if row is None:
        row = StoryboardRegenConfig(
            id=ids.new_id("rgc"),
            project_id=project_id,
            shot_id=shot.id,
            target=target,
            version=1,
            created_at=now,
        )
        db.add(row)
    else:
        row.version = int(row.version or 1) + 1
    row.workflow_id = workflow.id if workflow is not None else None
    row.workflow_version = workflow.version if workflow is not None else None
    row.references = payload.get("references")
    row.character_bindings = payload.get("character_bindings")
    row.spatial_layout = payload.get("spatial_layout")
    row.action_timeline = payload.get("action_timeline")
    row.prop_bindings = payload.get("prop_bindings")
    row.prompt_sections = payload.get("prompt_sections")
    row.final_prompt = payload.get("final_prompt")
    row.parameters = payload.get("parameters")
    row.extra = payload.get("extra")
    row.updated_at = now
    return row


@router.get("/shots/{shot_id}/regenerate/workspace")
def get_regenerate_workspace(
    shot_id: str,
    target: str = "clip",
    db: Session = Depends(get_db),
) -> dict:
    """读取重新生成工作台完整数据。

    有已保存配置时以保存内容为准，否则由基础快照推导并标注待确认项；
    同时返回历史数据兼容标记，供前端提示『历史配置不完整』。
    """
    from app.services import regen_workspace as ws

    shot = _get_shot(db, shot_id)
    if target not in _TARGET_ASSET_TYPE:
        raise errors.AppError("invalid_request", f"不支持的目标：{target}", 400)
    base_asset = _resolve_base_asset(db, shot, target, None)
    base_config = regen.build_base_config(db, base_asset)
    workflow = _ws_load_workflow(db, shot, target, {}, base_config)
    saved = ws.load_saved_config(db, shot.id, target)
    data = ws.build_workspace(
        db,
        shot=shot,
        target=target,
        workflow=workflow,
        base_config=base_config,
        saved=saved,
    )
    data["history_compat"] = {
        "has_saved_config": saved is not None,
        "saved_version": (saved or {}).get("version"),
        "base_snapshot_complete": bool(base_config.get("snapshot_complete")),
        "incomplete_reasons": base_config.get("incomplete_reasons") or [],
        "base_asset_id": base_asset.id,
    }
    return data


@router.put("/shots/{shot_id}/regenerate/workspace")
def put_regenerate_workspace(shot_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """保存重新生成工作台草稿。

    仅保存可编辑副本：不创建任务、不覆盖历史视频、不改写已生成的资产。
    """
    from app.services import regen_workspace as ws

    shot = _get_shot(db, shot_id)
    target = payload.get("target") or "clip"
    if target not in _TARGET_ASSET_TYPE:
        raise errors.AppError("invalid_request", f"不支持的目标：{target}", 400)
    project_id = _project_id_of(db, shot)
    base_asset = _resolve_base_asset(db, shot, target, payload.get("base_asset_id"))
    base_config = regen.build_base_config(db, base_asset)
    workflow = _ws_load_workflow(db, shot, target, payload, base_config)
    row = _ws_upsert_config(
        db,
        shot=shot,
        project_id=project_id,
        target=target,
        workflow=workflow,
        payload=payload,
    )
    db.commit()
    db.refresh(row)
    saved = ws.load_saved_config(db, shot.id, target)
    data = ws.build_workspace(
        db,
        shot=shot,
        target=target,
        workflow=workflow,
        base_config=base_config,
        saved=saved,
    )
    data["history_compat"] = {
        "has_saved_config": True,
        "saved_version": row.version,
        "base_snapshot_complete": bool(base_config.get("snapshot_complete")),
        "incomplete_reasons": base_config.get("incomplete_reasons") or [],
        "base_asset_id": base_asset.id,
    }
    return data


@router.post("/shots/{shot_id}/regenerate/workspace/validate")
def post_regenerate_workspace_validate(
    shot_id: str,
    payload: dict,
    db: Session = Depends(get_db),
) -> dict:
    """工作台提交前预检：结构化校验 + 槽位/资产校验 + 提示词一致性校验。

    errors 非空必须阻止提交；warnings 需用户确认。不创建任务。
    """
    from app.services import regen_workspace as ws

    shot = _get_shot(db, shot_id)
    target = payload.get("target") or "clip"
    if target not in _TARGET_ASSET_TYPE:
        raise errors.AppError("invalid_request", f"不支持的目标：{target}", 400)
    base_asset = _resolve_base_asset(db, shot, target, payload.get("base_asset_id"))
    base_config = regen.build_base_config(db, base_asset)
    workflow = _ws_load_workflow(db, shot, target, payload, base_config)
    data = ws.build_workspace(
        db,
        shot=shot,
        target=target,
        workflow=workflow,
        base_config=base_config,
        saved=None,
    )
    # 用提交内容覆盖派生结果，只校验用户当前这一份草稿
    for key in (
        "references",
        "character_bindings",
        "spatial_layout",
        "action_timeline",
        "prop_bindings",
        "prompt_sections",
    ):
        if key in payload and payload[key] is not None:
            data[key] = payload[key]
    final_prompt = payload.get("final_prompt")
    if final_prompt is None:
        final_prompt = data.get("final_prompt") or ""
    workspace_check = ws.validate_workspace(
        db,
        shot=shot,
        references=data.get("references") or [],
        bindings=data.get("character_bindings") or [],
        spatial=data.get("spatial_layout") or {},
        timeline=data.get("action_timeline") or {},
        props=data.get("prop_bindings") or [],
        final_prompt=final_prompt,
        nodes=ws.slot_node_map(workflow),
    )
    # 槽位与资产级校验：把工作台引用转成快照 refs 后复用既有校验
    refs = _ws_refs_to_slots(data.get("references") or [])
    slots = regen.normalize_reference_slots(workflow.capabilities if workflow is not None else None)
    slot_result = {"errors": [], "warnings": [], "checks": []}
    try:
        validated = regen.validate_refs(db, slots, refs)
        consistency = regen.validate_prompt_consistency(final_prompt, validated, slots)
        slot_result = consistency
    except HTTPException as e:
        detail = getattr(e, "detail", None) or {}
        slot_result = {
            "errors": [{
                "code": detail.get("code") or "invalid_request",
                "message": detail.get("message") or str(e),
            }],
            "warnings": [],
            "checks": [],
        }
    merged_errors = list(workspace_check.get("errors") or []) + list(slot_result.get("errors") or [])
    merged_warnings = list(workspace_check.get("warnings") or []) + list(slot_result.get("warnings") or [])
    merged_checks = list(workspace_check.get("checks") or []) + list(slot_result.get("checks") or [])
    return {
        "valid": not merged_errors,
        "errors": merged_errors,
        "warnings": merged_warnings,
        "checks": merged_checks,
        "workspace": {
            "summary": workspace_check.get("summary") or {},
            "continuity": data.get("continuity") or {},
            "capabilities": data.get("capabilities") or {},
        },
    }


@router.post("/shots/{shot_id}/regenerate/workspace/regenerate", status_code=202)
def post_regenerate_workspace_generate(
    shot_id: str,
    payload: dict,
    db: Session = Depends(get_db),
) -> dict:
    """按工作台配置提交重新生成任务。

    - 结果先落候选版本，不改写当前生效视频
    - 生成参数严格来自本次冻结快照
    - 提交成功后把本份配置保存为工作台草稿，便于下次继续编辑
    """
    from app.models.regen_config import StoryboardRegenConfig
    from app.services import regen_workspace as ws

    shot = _get_shot(db, shot_id)
    target = payload.get("target") or "clip"
    if target not in _TARGET_ASSET_TYPE:
        raise errors.AppError("invalid_request", f"不支持的目标：{target}", 400)
    idempotency_key = payload.get("idempotency_key")
    if idempotency_key:
        exist = db.query(Job).filter(Job.request_key == idempotency_key).first()
        if exist is not None:
            return _job_out(exist, (exist.input_snapshot or {}).get("snapshot"))
    project_id = _project_id_of(db, shot)
    base_asset = _resolve_base_asset(db, shot, target, payload.get("base_asset_id"))
    base_config = regen.build_base_config(db, base_asset)
    workflow = _ws_load_workflow(db, shot, target, payload, base_config)
    refs = _ws_refs_to_slots(payload.get("references") or [])
    if target == "clip":
        system_refs = _system_refs_for_clip(db, shot)
        provided = {r.get("slot_param") for r in refs}
        refs = list(refs) + [r for r in system_refs if r["slot_param"] not in provided]
    final_prompt = payload.get("final_prompt")
    if final_prompt is None:
        final_prompt = (base_config.get("prompt") or {}).get("rendered") or ""
    negative = payload.get("negative_prompt")
    if negative is None:
        negative = (base_config.get("negative_prompt") or {}).get("rendered") or ""
    parameters = dict(payload.get("parameters") or {})
    snapshot = regen.assemble_snapshot(
        db,
        mode=regen.MODE_FULL_RECONFIGURE,
        base_config=base_config,
        prompt=final_prompt,
        negative_prompt=negative,
        refs=refs,
        params=parameters,
        workflow=workflow,
    )
    consistency = snapshot.get("consistency") or {}
    if consistency.get("errors"):
        raise errors.AppError(
            "prompt_ref_mismatch",
            "提示词与参考图不一致，已阻止生成：" + "；".join(
                e.get("message") or str(e) for e in consistency["errors"]
            ),
            422,
            consistency=consistency,
        )
    params = dict(snapshot.get("model_params") or {})
    params["prompt"] = snapshot["prompt"]["rendered"]
    if snapshot["negative_prompt"]["rendered"]:
        params["negative"] = snapshot["negative_prompt"]["rendered"]
    media = regen.snapshot_to_media(snapshot)
    if media:
        params["__media__"] = media
    job_type = "generate_keyframe" if target == "keyframe" else f"generate_{target}"
    job = Job(
        id=ids.job_id(),
        project_id=project_id,
        shot_id=shot.id,
        job_type=job_type,
        workflow_id=workflow.id,
        workflow_version=workflow.version,
        params=params,
        status="queued",
        request_key=idempotency_key,
        created_at=_now(),
        input_snapshot={
            "snapshot_version": 1,
            "mode": regen.MODE_FULL_RECONFIGURE,
            "snapshot": snapshot,
            "workflow_version": workflow.version,
            "params": params,
            "generation_mode": regen.MODE_FULL_RECONFIGURE,
            "base_asset_id": base_asset.id,
            "base_asset_version": base_asset.version,
            "regeneration": True,
            "workspace": {
                "references": payload.get("references") or [],
                "character_bindings": payload.get("character_bindings") or [],
                "spatial_layout": payload.get("spatial_layout") or {},
                "action_timeline": payload.get("action_timeline") or {},
                "prop_bindings": payload.get("prop_bindings") or [],
                "prompt_sections": payload.get("prompt_sections") or {},
                "capabilities": {
                    "spatial_control": "prompt_level",
                    "timeline_control": "prompt_level",
                },
            },
        },
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        exist = (
            db.query(Job).filter(Job.request_key == idempotency_key).first()
            if idempotency_key
            else None
        )
        if exist is not None:
            return _job_out(exist, (exist.input_snapshot or {}).get("snapshot"))
        raise errors.AppError(
            "job_conflict",
            "该提交与另一个并发请求冲突，请刷新页面后重试",
            409,
        )
    regen.write_input_refs(db, job, snapshot)
    if target == "clip":
        from app.api.v1.generate import _inject_prev_shot_frame
        info = _inject_prev_shot_frame(db, shot, target, params)
        snap_now = dict(job.input_snapshot or {})
        snap_now["prev_frame"] = info
        snap_now["injected_media"] = dict(params.get("__media__") or {})
        job.input_snapshot = snap_now
        job.params = params
    # 保存本份配置为工作台草稿，便于下次继续编辑（不影响本次已冻结的快照）
    saved_row = _ws_upsert_config(
        db,
        shot=shot,
        project_id=project_id,
        target=target,
        workflow=workflow,
        payload=payload,
    )
    db.commit()
    out = _job_out(job, snapshot)
    out["workspace_config_id"] = saved_row.id
    out["workspace_config_version"] = saved_row.version
    out["continuity"] = ws.build_continuity(
        db,
        shot,
        current_spatial=payload.get("spatial_layout") or {},
        current_props=payload.get("prop_bindings") or [],
    )
    return out

