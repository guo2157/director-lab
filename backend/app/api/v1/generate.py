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


def _workflow_binds_param(wf: Workflow, param_name: str) -> bool:
    """检查工作流定义是否绑定了指定参数标记（$param.*）。
    用于判断负面约束等参数能否实际注入，避免写入无人消费的参数。
    """
    from app.services.workflow_inject import parse_workflow
    try:
        parsed = parse_workflow(wf.definition)
    except Exception:  # noqa: BLE001
        return False
    return param_name in parsed.params and not parsed.errors


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


def _find_first_frame_workflow(db: Session) -> Workflow | None:
    """查找支持 first_frame 的视频工作流（flf2v）。"""
    from app.services.workflow_inject import parse_workflow
    for wf in db.query(Workflow).filter(Workflow.workflow_type == "video").all():
        parsed = parse_workflow(wf.definition)
        if not parsed.errors and "first_frame" in parsed.params:
            return wf
    return None


def _is_first_shot_of_episode(db: Session, shot: Shot) -> bool:
    """判断镜头是否为某集的第一个镜头（按 episode, scene.seq, shot.seq 排序）。"""
    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        return False
    first = (
        db.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == scene.project_id, Shot.episode == shot.episode)
        .order_by(Scene.seq, Shot.seq)
        .first()
    )
    return first is not None and first.id == shot.id


def _inject_continuity_first_frame(db: Session, shot: Shot, target: str, wf: Workflow, params: dict) -> Workflow:
    """跨集连续性首帧注入（对应改造第五节）。

    当 target == clip 且镜头为某集首镜时：
    1. 检查上一集末帧是否存在且有效
    2. 若有效，切换到 flf2v 工作流（严格首帧锁定）
    3. 将末帧路径注入 __media__.first_frame
    4. 若连续性应满足但失败，抛出错误阻止生成
    """
    if target != "clip":
        return wf
    if not _is_first_shot_of_episode(db, shot):
        return wf
    if shot.episode <= 1:
        return wf

    from app.services.continuity import check_continuity_for_episode
    ok, err, rec = check_continuity_for_episode(db, _project_id_from_shot(db, shot), shot.episode)
    if not ok:
        raise errors.AppError("continuity_check_failed", err or "连续性校验失败", 422)
    if rec is None or not rec.source_frame_path:
        return wf

    flf2v_wf = _find_first_frame_workflow(db)
    if flf2v_wf is None:
        # flf2v 工作流不可用时，降级为 ref_prev_frame 参考图约束
        media = params.get("__media__", {})
        media["ref_prev_frame"] = rec.source_frame_path
        params["__media__"] = media
        return wf

    media = params.get("__media__", {})
    media["first_frame"] = rec.source_frame_path
    params["__media__"] = media
    return flf2v_wf


def _project_id_from_shot(db: Session, shot: Shot) -> str:
    """从镜头获取项目 ID。"""
    scene = db.get(Scene, shot.scene_id)
    return scene.project_id if scene else ""


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


def _check_inflight_duplicate(db: Session, shot_id: str, target: str) -> None:
    """在途任务去重：同镜头同目标已有 queued/running 任务时返回 409，
    防止重复提交/重试产生重复任务（幂等性保障）。"""
    job_type = f"generate_{target}" if target != "keyframe" else "generate_keyframe"
    dup = (
        db.query(Job)
        .filter(
            Job.shot_id == shot_id,
            Job.job_type == job_type,
            Job.status.in_(("queued", "running")),
        )
        .order_by(Job.created_at.desc())
        .first()
    )
    if dup is not None:
        raise errors.AppError(
            "job_in_flight",
            f"该镜头已有同目标在途任务（{dup.id}，状态 {dup.status}），请等待完成或先取消",
            409,
        )


def _filter_inflight(db: Session, shots: list, target: str) -> tuple[list, list]:
    """批量生成时过滤在途任务：跳过已有同目标 queued/running 任务的镜头，
    返回（可生成镜头, 跳过明细），避免批量提交产生重复任务。"""
    job_type = f"generate_{target}" if target != "keyframe" else "generate_keyframe"
    shot_ids = [s.id for s in shots]
    if not shot_ids:
        return [], []
    inflight = {
        j.shot_id: j.id
        for j in db.query(Job)
        .filter(
            Job.shot_id.in_(shot_ids),
            Job.job_type == job_type,
            Job.status.in_(("queued", "running")),
        )
        .all()
    }
    ok, skipped = [], []
    for s in shots:
        if s.id in inflight:
            skipped.append({"shot_id": s.id, "job_id": inflight[s.id], "reason": "job_in_flight"})
        else:
            ok.append(s)
    return ok, skipped


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
    # 在途任务去重：同镜头同目标已有 queued/running 任务时拒绝重复提交
    _check_inflight_duplicate(db, shot.id, target)
    wf = _match_workflow(db, target, payload.get("workflow_id"))
    params = dict(payload.get("params") or {})
    # 跨集连续性首帧注入（第五节）：集首镜 clip 生成时注入上一集末帧
    if target == "clip":
        wf = _inject_continuity_first_frame(db, shot, target, wf, params)
    # 按 target 组装默认 prompt 基础
    if target == "keyframe":
        params.setdefault("prompt", shot.image_prompt or "")
    elif target == "clip":
        params.setdefault("prompt", shot.video_prompt or shot.image_prompt or "")
    elif target == "audio":
        params.setdefault("tts_text", shot.dialogue or "")
    # P2：镜头资产清单注入——出场角色/所属场景未在提示词 @引用 时自动补入头部
    if target == "keyframe" and params.get("prompt"):
        from app.services.shot_refs import inject_asset_refs
        params["prompt"] = inject_asset_refs(db, shot, params["prompt"])
    # @引用注入（D-16）：keyframe/clip 生成时解析提示词中的 @角色/@场景，自动携带参考图
    # 连续性首帧注入可能已设置 __media__.first_frame，此处合并而非覆盖
    if target in ("keyframe", "clip"):
        from app.services.ref_inject import build_media_params
        prompt_text = params.get("prompt") or ""
        if prompt_text:
            slots = (wf.capabilities or {}).get("reference_slots") if wf.capabilities else None
            media = build_media_params(db, scene.project_id, prompt_text, slots)
            if media:
                _existing = params.get("__media__", {})
                _existing.update(media)
                params["__media__"] = _existing
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
    # 在途任务去重：同镜头同目标已有 queued/running 任务时拒绝重复提交
    _check_inflight_duplicate(db, shot.id, target)
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
    # 跨集连续性首帧注入（重做与单镜生成行为一致）
    if target == "clip":
        wf = _inject_continuity_first_frame(db, shot, target, wf, params)
    if target == "keyframe":
        params.setdefault("prompt", shot.image_prompt or "")
    elif target == "clip":
        params.setdefault("prompt", shot.video_prompt or shot.image_prompt or "")
    elif target == "audio":
        params.setdefault("tts_text", shot.dialogue or "")
    # P2：镜头资产清单注入（重做与单镜生成行为一致）
    if target == "keyframe" and params.get("prompt"):
        from app.services.shot_refs import inject_asset_refs
        params["prompt"] = inject_asset_refs(db, shot, params["prompt"])
    # @引用注入（D-16）：重做与单镜生成行为一致，重做时同样携带参考图
    # 连续性首帧注入可能已设置 __media__.first_frame，此处合并而非覆盖
    if target in ("keyframe", "clip"):
        from app.services.ref_inject import build_media_params
        prompt_text = params.get("prompt") or ""
        if prompt_text:
            slots = (wf.capabilities or {}).get("reference_slots") if wf.capabilities else None
            media = build_media_params(db, scene.project_id, prompt_text, slots)
            if media:
                _existing = params.get("__media__", {})
                _existing.update(media)
                params["__media__"] = _existing
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
    # P1：角色定妆图使用纯净化结构化模板（单一主体/干净背景/负面约束分段）
    # P4 整改：必填变量缺失时明确报错（422），不静默生成占位提示词
    from app.services.asset_prompts import ASSET_PROMPTS_VERSION, build_character_prompt, negative_for_asset_type
    try:
        params.setdefault(
            "prompt",
            build_character_prompt(ch.name, ch.appearance or "", description=ch.description or ""),
        )
    except ValueError as e:
        raise errors.AppError("invalid_request", str(e), 422)
    params["__asset_prompt_version__"] = ASSET_PROMPTS_VERSION
    # P4 整改：负面约束按工作流实际参数绑定注入（标记 $negative.*），
    # 不再依赖 capabilities.negative_prompt 声明（现有工作流均未声明该字段，导致约束从未生效）
    if "negative" not in params and _workflow_binds_param(wf, "negative"):
        params["negative"] = negative_for_asset_type("character")
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
    # P1：道具锚定图使用纯净化结构化模板
    # P4 整改：必填变量缺失时明确报错（422）
    from app.services.asset_prompts import ASSET_PROMPTS_VERSION, build_prop_prompt, negative_for_asset_type
    try:
        params.setdefault(
            "prompt",
            build_prop_prompt(p.name, p.appearance or "", description=p.description or ""),
        )
    except ValueError as e:
        raise errors.AppError("invalid_request", str(e), 422)
    params["__asset_prompt_version__"] = ASSET_PROMPTS_VERSION
    if "negative" not in params and _workflow_binds_param(wf, "negative"):
        params["negative"] = negative_for_asset_type("prop")
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
    # P1：场景基准图使用纯净化结构化模板（无角色/无独立道具/空间布局完整）
    # P4 整改：必填变量缺失时明确报错（422）
    from app.services.asset_prompts import ASSET_PROMPTS_VERSION, build_scene_prompt, negative_for_asset_type
    try:
        params.setdefault(
            "prompt",
            build_scene_prompt(sc.title or "", sc.description or ""),
        )
    except ValueError as e:
        raise errors.AppError("invalid_request", str(e), 422)
    params["__asset_prompt_version__"] = ASSET_PROMPTS_VERSION
    if "negative" not in params and _workflow_binds_param(wf, "negative"):
        params["negative"] = negative_for_asset_type("scene")
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
    # 在途任务过滤：跳过已有同目标在途任务的镜头，避免重复提交
    shots, skipped = _filter_inflight(db, shots, target)
    if not shots:
        raise errors.AppError(
            "job_in_flight",
            f"所有选中镜头均已有同目标在途任务（{len(skipped)} 个），请等待完成或先取消",
            409,
        )
    if target == "clip":
        _check_keyframe_gate(db, [s.id for s in shots])
    wf = _match_workflow(db, target, payload.get("workflow_id"))
    # @引用注入（D-16，P0 修复）：与单镜生成/项目批量生成行为一致，
    # 解析每个镜头提示词中的 @角色/@场景/@道具，自动携带参考图
    from app.services.ref_inject import build_media_params
    slots = (wf.capabilities or {}).get("reference_slots") if wf.capabilities else None
    jobs = []
    for shot in shots:
        params = dict(payload.get("params") or {})
        if target == "keyframe":
            params.setdefault("prompt", shot.image_prompt or "")
        elif target == "clip":
            params.setdefault("prompt", shot.video_prompt or shot.image_prompt or "")
        # 跨集连续性首帧注入（批量生成与单镜生成行为一致）
        shot_wf = wf
        if target == "clip":
            shot_wf = _inject_continuity_first_frame(db, shot, target, wf, params)
        # P2：镜头资产清单注入（场景批量与单镜生成行为一致）
        if target == "keyframe" and params.get("prompt"):
            from app.services.shot_refs import inject_asset_refs
            params["prompt"] = inject_asset_refs(db, shot, params["prompt"])
        # @引用注入：合并已有 __media__（首帧注入可能已设置 first_frame）
        if target in ("keyframe", "clip"):
            prompt_text = params.get("prompt") or ""
            if prompt_text:
                media = build_media_params(db, sc.project_id, prompt_text, slots)
                if media:
                    _existing = params.get("__media__", {})
                    _existing.update(media)
                    params["__media__"] = _existing
        jobs.append(_create_job(db, sc.project_id, shot.id, target, shot_wf, params))
    db.commit()
    return {"jobs": [_job_out(j) for j in jobs], "count": len(jobs), "skipped": skipped}


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
    # 在途任务过滤：跳过已有同目标在途任务的镜头，避免重复提交
    shots, skipped = _filter_inflight(db, shots, target)
    if not shots:
        raise errors.AppError(
            "job_in_flight",
            f"所有选中镜头均已有同目标在途任务（{len(skipped)} 个），请等待完成或先取消",
            409,
        )
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
        # 跨集连续性首帧注入（项目批量与单镜生成行为一致）
        shot_wf = wf
        if target == "clip":
            shot_wf = _inject_continuity_first_frame(db, shot, target, wf, params)
        # P2：镜头资产清单注入（项目批量与单镜生成行为一致）
        if target == "keyframe" and params.get("prompt"):
            from app.services.shot_refs import inject_asset_refs
            params["prompt"] = inject_asset_refs(db, shot, params["prompt"])
        # @引用注入：合并已有 __media__（首帧注入可能已设置 first_frame）
        if target in ("keyframe", "clip"):
            from app.services.ref_inject import build_media_params
            prompt_text = params.get("prompt") or ""
            if prompt_text:
                media = build_media_params(db, project_id, prompt_text, slots)
                if media:
                    _existing = params.get("__media__", {})
                    _existing.update(media)
                    params["__media__"] = _existing
        jobs.append(_create_job(db, project_id, shot.id, target, shot_wf, params))
    db.commit()
    return {"jobs": [_job_out(j) for j in jobs], "count": len(jobs), "skipped": skipped}


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
