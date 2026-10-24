"""生成接口：API.md §8 字段级契约。
- POST /api/shots/{id}/generate：单镜生成（target 枚举 keyframe/clip/audio/bgm/sfx）
- POST /api/shots/{id}/regenerate：重做单镜（旧资产转历史版本）
- POST /api/characters/{id}/generate：角色定妆图（target 固定 keyframe）
- POST /api/props/{id}/generate：道具锚定图（规则同角色）
- POST /api/scenes/{id}/generate-baseline：场景基准图
- POST /api/scenes/{id}/generate：场景内镜头批量生成（串行入队）
- POST /api/projects/{id}/generate：跨场景批量生成
- POST /api/shots/{id}/assets：手动绑定资产（4 种 role）
工作流匹配：target → workflow_type（keyframe→image、clip→video、audio→tts、bgm→bgm），
传 workflow_id 时跳过匹配直接使用；无匹配返回 422 no_matching_workflow。
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.models import Asset, AssetRef, Character, Job, Project, Prop, Scene, Shot, Workflow, ids
from app.services import errors

router = APIRouter(prefix="/api", tags=["generate"])

# target → workflow_type 映射（API.md §8）
_TARGET_WF_TYPE = {
    "keyframe": "image",
    "clip": "video",
    "audio": "tts",
    "bgm": "bgm",
    "sfx": "sfx",
}

# 手动绑定允许的 role（API.md §6）
_MANUAL_ROLES = {"reference", "first_frame", "end_frame", "audio"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _match_workflow(db: Session, target: str, workflow_id: str | None) -> Workflow:
    """工作流匹配：显式 workflow_id 优先；否则按 workflow_type 依次尝试 默认 → 内置 → 任意。"""
    if workflow_id:
        wf = db.get(Workflow, workflow_id)
        if wf is None:
            raise errors.AppError("workflow_not_found", "工作流不存在", 404)
        return wf
    wf_type = _TARGET_WF_TYPE.get(target)
    if wf_type is None:
        raise errors.AppError("invalid_request", f"不支持的 target: {target}", 400)
    # 1. 默认工作流优先
    wf = (
        db.query(Workflow)
        .filter(Workflow.workflow_type == wf_type, Workflow.is_default == True)  # noqa: E712
        .first()
    )
    # 2. 内置工作流次优先（系统预置，默认优先执行）
    if wf is None:
        wf = (
            db.query(Workflow)
            .filter(Workflow.workflow_type == wf_type, Workflow.is_builtin == True)  # noqa: E712
            .first()
        )
    # 3. 兜底：该类型任意工作流
    if wf is None:
        wf = (
            db.query(Workflow)
            .filter(Workflow.workflow_type == wf_type)
            .first()
        )
    if wf is None:
        raise errors.AppError("no_matching_workflow", f"无 {wf_type} 类型可用工作流", 422)
    return wf


def _create_job(db: Session, project_id: str, shot_id: str | None, target: str,
                wf: Workflow, params: dict, generation_mode: str | None = None) -> Job:
    """创建生成任务并持久化 effective_inputs 快照。"""
    job_type = f"generate_{target}" if target != "keyframe" else "generate_keyframe"
    job = Job(
        id=ids.job_id(),
        project_id=project_id,
        shot_id=shot_id,
        job_type=job_type,
        workflow_id=wf.id,
        workflow_version=wf.version,
        params=params,
        status="queued",
        created_at=_now(),
        input_snapshot={
            "workflow_version": wf.version,
            "params": params,
            "generation_mode": generation_mode,
        },
    )
    db.add(job)
    return job


def _job_out(job: Job) -> dict:
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
    }


def _get_shot(db: Session, shot_id: str) -> Shot:
    s = db.get(Shot, shot_id)
    if s is None:
        raise errors.AppError("shot_not_found", "镜头不存在", 404)
    return s


def _check_keyframe_gate(db: Session, shot_ids: list[str]) -> None:
    """静图先行闸口（D-13）：生成 clip 前校验分镜图已人工确认，未确认返回 409。"""
    unconfirmed = (
        db.query(Shot)
        .filter(Shot.id.in_(shot_ids), Shot.keyframe_confirmed.is_(False))
        .all()
    )
    if unconfirmed:
        ids = ", ".join(s.id for s in unconfirmed[:10])
        raise errors.AppError(
            "keyframe_not_confirmed",
            f"存在未确认分镜图的镜头（{ids}{' 等' if len(unconfirmed) > 10 else ''}），请先在分镜审核页确认后再生成视频",
            409,
        )


@router.post("/shots/{shot_id}/generate", status_code=202)
def generate_shot(shot_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """触发单镜生成。target 必填（keyframe/clip/audio/bgm/sfx）。"""
    shot = _get_shot(db, shot_id)
    scene = db.get(Scene, shot.scene_id)
    target = payload.get("target")
    if target not in _TARGET_WF_TYPE:
        raise errors.AppError("invalid_request", "target 必须为 keyframe/clip/audio/bgm/sfx", 400)
    if target == "clip":
        _check_keyframe_gate(db, [shot.id])
    wf = _match_workflow(db, target, payload.get("workflow_id"))
    params = dict(payload.get("params") or {})
    # 按 target 组装默认 prompt 基础
    if target == "keyframe":
        params.setdefault("prompt", shot.image_prompt or "")
    elif target == "clip":
        params.setdefault("prompt", shot.video_prompt or shot.image_prompt or "")
    elif target == "audio":
        params.setdefault("tts_text", shot.dialogue or "")
    # @引用注入（D-16）：keyframe/clip 生成时解析提示词中的 @角色/@场景，自动携带参考图
    if target in ("keyframe", "clip") and "__media__" not in params:
        from app.services.ref_inject import build_media_params
        prompt_text = params.get("prompt") or ""
        if prompt_text:
            slots = (wf.capabilities or {}).get("reference_slots") if wf.capabilities else None
            media = build_media_params(db, scene.project_id, prompt_text, slots)
            if media:
                params["__media__"] = media
    job = _create_job(db, scene.project_id, shot.id, target, wf, params,
                      generation_mode=payload.get("generation_mode"))
    db.commit()
    return _job_out(job)


@router.post("/shots/{shot_id}/regenerate", status_code=202)
def regenerate_shot(shot_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """重做单镜：旧资产转历史版本（validity=superseded），不物理删除。"""
    shot = _get_shot(db, shot_id)
    scene = db.get(Scene, shot.scene_id)
    target = payload.get("target", "keyframe")
    if target not in _TARGET_WF_TYPE:
        raise errors.AppError("invalid_request", "target 无效", 400)
    if target == "clip":
        _check_keyframe_gate(db, [shot.id])
    # 旧资产转历史版本
    if target == "keyframe":
        # 重做分镜图后确认自动失效（D-13）：需重新生成并重新确认
        shot.keyframe_confirmed = False
    old_type = {"keyframe": "keyframe", "clip": "clip", "audio": "audio", "bgm": "bgm"}.get(target)
    if old_type:
        olds = (
            db.query(Asset)
            .join(AssetRef, AssetRef.asset_id == Asset.id)
            .filter(AssetRef.shot_id == shot.id, Asset.asset_type == old_type,
                    Asset.validity == "current")
            .all()
        )
        for a in olds:
            a.validity = "superseded"
    wf = _match_workflow(db, target, payload.get("workflow_id"))
    params = dict(payload.get("params") or {})
    if target == "keyframe":
        params.setdefault("prompt", shot.image_prompt or "")
    elif target == "clip":
        params.setdefault("prompt", shot.video_prompt or shot.image_prompt or "")
    elif target == "audio":
        params.setdefault("tts_text", shot.dialogue or "")
    # @引用注入（D-16）：重做与单镜生成行为一致，重做时同样携带参考图
    if target in ("keyframe", "clip") and "__media__" not in params:
        from app.services.ref_inject import build_media_params
        prompt_text = params.get("prompt") or ""
        if prompt_text:
            slots = (wf.capabilities or {}).get("reference_slots") if wf.capabilities else None
            media = build_media_params(db, scene.project_id, prompt_text, slots)
            if media:
                params["__media__"] = media
    job = _create_job(db, scene.project_id, shot.id, target, wf, params)
    db.commit()
    return _job_out(job)


@router.post("/characters/{character_id}/generate", status_code=202)
def generate_character(character_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """生成角色定妆图：target 固定 keyframe，appearance 为 prompt 基础，无需参考图。"""
    ch = db.get(Character, character_id)
    if ch is None:
        raise errors.AppError("not_found", "角色不存在", 404)
    wf = _match_workflow(db, "keyframe", payload.get("workflow_id"))
    params = dict(payload.get("params") or {})
    params.setdefault("prompt", ch.appearance or ch.description or ch.name)
    job = _create_job(db, ch.project_id, None, "keyframe", wf, params)
    job.params = {**job.params, "__character_id__": ch.id}
    db.commit()
    return _job_out(job)


@router.post("/props/{prop_id}/generate", status_code=202)
def generate_prop(prop_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """生成道具锚定图：规则同角色定妆图。"""
    p = db.get(Prop, prop_id)
    if p is None:
        raise errors.AppError("not_found", "道具不存在", 404)
    wf = _match_workflow(db, "keyframe", payload.get("workflow_id"))
    params = dict(payload.get("params") or {})
    params.setdefault("prompt", p.appearance or p.description or p.name)
    job = _create_job(db, p.project_id, None, "keyframe", wf, params)
    job.params = {**job.params, "__prop_id__": p.id}
    db.commit()
    return _job_out(job)


@router.post("/scenes/{scene_id}/generate-baseline", status_code=202)
def generate_scene_baseline(scene_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """生成场景基准图：target 固定 keyframe，title+description 为 prompt 基础。"""
    sc = db.get(Scene, scene_id)
    if sc is None:
        raise errors.AppError("scene_not_found", "场景不存在", 404)
    wf = _match_workflow(db, "keyframe", payload.get("workflow_id"))
    params = dict(payload.get("params") or {})
    base = sc.title if not sc.description else f"{sc.title}。{sc.description}"
    params.setdefault("prompt", base)
    job = _create_job(db, sc.project_id, None, "keyframe", wf, params)
    job.params = {**job.params, "__scene_id__": sc.id}
    db.commit()
    return _job_out(job)


@router.post("/scenes/{scene_id}/generate", status_code=202)
def generate_scene_shots(scene_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """批量生成场景内镜头：按 seq 升序串行入队；少于 3 个镜头返回 400。"""
    sc = db.get(Scene, scene_id)
    if sc is None:
        raise errors.AppError("scene_not_found", "场景不存在", 404)
    target = payload.get("target", "keyframe")
    if target not in _TARGET_WF_TYPE:
        raise errors.AppError("invalid_request", "target 无效", 400)
    q = db.query(Shot).filter(Shot.scene_id == sc.id)
    shot_ids = payload.get("shot_ids")
    if shot_ids:
        q = q.filter(Shot.id.in_(shot_ids))
    shots = q.order_by(Shot.seq).all()
    if len(shots) < 3:
        raise errors.AppError("invalid_request", "选中镜头少于 3 个，请使用单镜生成接口", 400)
    if target == "clip":
        _check_keyframe_gate(db, [s.id for s in shots])
    wf = _match_workflow(db, target, payload.get("workflow_id"))
    jobs = []
    for shot in shots:
        params = dict(payload.get("params") or {})
        if target == "keyframe":
            params.setdefault("prompt", shot.image_prompt or "")
        elif target == "clip":
            params.setdefault("prompt", shot.video_prompt or shot.image_prompt or "")
        jobs.append(_create_job(db, sc.project_id, shot.id, target, wf, params))
    db.commit()
    return {"jobs": [_job_out(j) for j in jobs], "count": len(jobs)}


@router.post("/projects/{project_id}/generate", status_code=202)
def generate_project(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """跨场景批量生成：集数升序 → 场景出场顺序 → 镜头 seq 升序，全部串行。"""
    p = db.get(Project, project_id)
    if p is None:
        raise errors.AppError("project_not_found", "项目不存在", 404)
    target = payload.get("target")
    if target not in ("keyframe", "clip"):
        raise errors.AppError("invalid_request", "target 必须为 keyframe 或 clip", 400)
    q = (
        db.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
    )
    if payload.get("shot_ids"):
        q = q.filter(Shot.id.in_(payload["shot_ids"]))
    if payload.get("scene_ids"):
        q = q.filter(Shot.scene_id.in_(payload["scene_ids"]))
    if payload.get("episode_from") is not None:
        q = q.filter(Shot.episode >= payload["episode_from"])
    if payload.get("episode_to") is not None:
        q = q.filter(Shot.episode <= payload["episode_to"])
    shots = q.order_by(Shot.episode, Scene.seq, Shot.seq).all()
    if len(shots) < 3:
        raise errors.AppError("invalid_request", "选中镜头少于 3 个，请使用单镜生成接口", 400)
    if target == "clip":
        _check_keyframe_gate(db, [s.id for s in shots])
    wf = _match_workflow(db, target, payload.get("workflow_id"))
    jobs = []
    slots = (wf.capabilities or {}).get("reference_slots") if wf.capabilities else None
    for shot in shots:
        params = dict(payload.get("params") or {})
        if target == "keyframe":
            params.setdefault("prompt", shot.image_prompt or "")
        else:
            params.setdefault("prompt", shot.video_prompt or shot.image_prompt or "")
        # @引用注入（D-16）：批量生成与单镜生成行为一致，分镜图/视频均携带参考图
        if "__media__" not in params:
            from app.services.ref_inject import build_media_params
            prompt_text = params.get("prompt") or ""
            if prompt_text:
                media = build_media_params(db, project_id, prompt_text, slots)
                if media:
                    params["__media__"] = media
        jobs.append(_create_job(db, project_id, shot.id, target, wf, params))
    db.commit()
    return {"jobs": [_job_out(j) for j in jobs], "count": len(jobs)}


@router.post("/shots/{shot_id}/assets", status_code=201)
def bind_shot_asset(shot_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """手动绑定资产：仅允许 reference/first_frame/end_frame/audio 四种 role。
    单镜头关联总数 ≤6，重复绑定返回 409。"""
    shot = _get_shot(db, shot_id)
    asset_id = payload.get("asset_id")
    role = payload.get("role")
    if not asset_id or not role:
        raise errors.AppError("invalid_request", "asset_id 与 role 必填", 400)
    if role not in _MANUAL_ROLES:
        raise errors.AppError("invalid_request", f"role 仅允许 {sorted(_MANUAL_ROLES)}", 400)
    a = db.get(Asset, asset_id)
    if a is None:
        raise errors.AppError("asset_not_found", "资产不存在", 404)
    existing = db.query(AssetRef).filter(AssetRef.shot_id == shot.id).count()
    if existing >= 6:
        raise errors.AppError("invalid_request", "单镜头关联资产总数不得超过 6 个", 400)
    dup = (
        db.query(AssetRef)
        .filter(AssetRef.shot_id == shot.id, AssetRef.asset_id == asset_id)
        .first()
    )
    if dup is not None:
        raise errors.AppError("asset_in_use", "同一镜头重复绑定同一资产", 409)
    ref = AssetRef(
        id=ids.asset_ref_id(), shot_id=shot.id, asset_id=asset_id,
        role=role, source_version=a.version, created_at=_now(),
    )
    a.ref_count = (a.ref_count or 0) + 1
    db.add(ref)
    db.commit()
    return {"id": ref.id, "shot_id": shot.id, "asset_id": asset_id, "role": role}
