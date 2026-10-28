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

from fastapi import APIRouter, Depends
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
