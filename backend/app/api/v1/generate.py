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
from app.models import (Asset, AssetRef, Character, Job, JobEvent, Project, Prop, Scene,
                       Shot, Workflow, ids)
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

# 重新生成分镜视频的调度优先级：高于普通任务（0）与重试加权（+100），
# 用户手动发起的重新生成在有空闲 ComfyUI 实例时优先插队执行，不必排在批量任务末尾
_REGENERATE_CLIP_PRIORITY = 1000


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _match_workflow_exact(db: Session, target: str, workflow_id: str | None) -> Workflow:
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


_REF_CHAR_SLOTS = ("ref_identity", "ref_character_2", "ref_image")


def _find_ref_capable_image_workflow(db: Session) -> Workflow | None:
    """找带角色参考槽位的图片工作流（分镜图默认用它，避免 @角色 撞上 0 槽位）。"""
    rows = (
        db.query(Workflow)
        .filter(Workflow.workflow_type == "image")
        .order_by(Workflow.is_default.desc())
        .all()
    )
    for wf in rows:
        slots = (wf.capabilities or {}).get("reference_slots") or []
        names = [item.get("param") if isinstance(item, dict) else item for item in slots]
        if any(name in _REF_CHAR_SLOTS for name in names):
            return wf
    return None


# 视频工作流默认档位优先级：标准 5s 优先，其次极速 5s / 质量 10s
_VIDEO_TIER_ORDER = {"std_5s": 0, "fast_5s": 1, "quality_10s": 2}


def _find_ref_capable_video_workflow(db: Session) -> Workflow | None:
    """找能接收分镜图参考槽位（ref_image）的视频工作流。

    无显式默认工作流时，避免按插入顺序落到只接受 first_frame 的 FLF2V 工作流：
    批量出视频注入的是分镜图（ref_image），首个镜头也没有上一镜尾帧，
    会因 first_frame 必填而整批失败。
    """
    rows = (
        db.query(Workflow)
        .filter(Workflow.workflow_type == "video")
        .order_by(Workflow.is_default.desc())
        .all()
    )
    candidates: list[tuple[int, Workflow]] = []
    for wf in rows:
        slots = (wf.capabilities or {}).get("reference_slots") or []
        names = [item.get("param") if isinstance(item, dict) else item for item in slots]
        if "ref_image" in names:
            tier = (wf.capabilities or {}).get("tier")
            candidates.append((_VIDEO_TIER_ORDER.get(tier, 99), wf))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def _match_workflow(db: Session, target: str, workflow_id: str | None) -> Workflow:
    """工作流匹配：显式 workflow_id 优先；分镜图默认自动改用带参考槽位的工作流。

    默认图片工作流是无参考的纯文生图（0 槽位），而分镜图提示词通常带 @角色，
    直接用会在 ref_inject 里报"当前工作流仅支持 0 个角色参考槽位"。
    """
    wf = _match_workflow_exact(db, target, workflow_id)
    if workflow_id is None and target == "keyframe":
        if not (wf.capabilities or {}).get("reference_slots"):
            alt = _find_ref_capable_image_workflow(db)
            if alt is not None:
                return alt
    if workflow_id is None and target == "clip" and not wf.is_default:
        slots = (wf.capabilities or {}).get("reference_slots") or []
        names = [item.get("param") if isinstance(item, dict) else item for item in slots]
        if "ref_image" not in names:
            alt = _find_ref_capable_video_workflow(db)
            if alt is not None:
                return alt
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


def _attach_compilation(job: Job, compilation: dict | None) -> None:
    """把阶段 B 编译结果写入 input_snapshot，便于复现与排障。"""
    if not compilation:
        return
    snap = dict(job.input_snapshot or {})
    snap["prompt_compilation"] = compilation
    job.input_snapshot = snap


# 结构性错误：槽位超出能力、槽位重复、资产类型不匹配、参考文件缺失等，必须硬阻断，
# 不允许静默丢图或把错误槽位塞给工作流。
_HARD_BLOCKING = {
    "reference_capacity_exceeded",
    "reference_slot_duplicated",
    "reference_asset_type_mismatch",
    "reference_asset_foreign",
    "reference_asset_file_missing",
    "reference_asset_not_current",
    "asset_identity_missing",
}


def _compilation_blocked(compiled, http: int = 422) -> list:
    """编译结果未就绪时的处理：结构性错误硬阻断，其余交由既有资产门禁处理并留痕。"""
    if compiled.status == "ready":
        return []
    hard = [i for i in compiled.blocking_issues if i.get("code") in _HARD_BLOCKING]
    if not hard:
        return list(compiled.blocking_issues)
    messages = [issue.get("message", issue.get("code", "")) for issue in hard]
    raise errors.AppError(
        "prompt_validation_failed",
        f"提示词校验未通过（{compiled.template_id}）：" + "；".join(m for m in messages if m),
        http,
        blocking_issues=compiled.blocking_issues,
        template_id=compiled.template_id,
    )


def _apply_compiled_media(compiled, params: dict) -> dict:
    """只合并编译出的参考图槽位，不改写用户已确认的提示词。"""
    out = dict(params)
    if compiled.reference_slots:
        media = dict(out.get("__media__") or {})
        for slot in compiled.reference_slots:
            media[slot["slot_key"]] = slot["file_path"]
        out["__media__"] = media
    return out


def _finalize_negative(compiled, wf: Workflow, params: dict) -> dict:
    """负向提示词仅在当前工作流真实绑定 negative 参数时注入，否则不假装生效。"""
    out = dict(params)
    if not compiled.negative_prompt:
        return out
    if _workflow_binds_param(wf, "negative"):
        out.setdefault("negative", compiled.negative_prompt)
    else:
        out.pop("negative", None)
    return out


def _match_pure_asset_workflow(db: Session, workflow_id: str | None) -> Workflow:
    """资产图专用工作流匹配：优先无参考图槽位的纯文生图工作流。

    带参考图槽位的工作流在未提供参考图时，模型会自行补全主体，
    导致角色图、道具图中混入无关人物、场景与杂物，破坏资产纯净度。
    """
    if workflow_id:
        wf = db.get(Workflow, workflow_id)
        if wf is None:
            raise errors.AppError("workflow_not_found", "工作流不存在", 404)
        if (wf.capabilities or {}).get("reference_slots"):
            raise errors.AppError(
                "invalid_request",
                "资产图只允许无参考图的纯文生图工作流，当前工作流声明了 reference_slots",
                422,
            )
        return wf
    candidates = (
        db.query(Workflow)
        .filter(Workflow.workflow_type == "image")
        .order_by(Workflow.is_default.desc(), Workflow.is_builtin.desc())
        .all()
    )
    for wf in candidates:
        if not (wf.capabilities or {}).get("reference_slots"):
            return wf
    raise errors.AppError(
        "workflow_not_found", "未找到可用的纯文生图工作流，资产图生成已阻断", 404
    )


# 资产图纯净约束：调用方自带提示词时也必须附带，避免净化模板被整体跳过
_ASSET_PURITY_CLAUSE = {
    "character": (
        "画面纯净约束：只出现这一个角色，不出现第二个人物、人物剪影、镜中人物或背景人物；"
        "不出现剧情场景背景，背景为干净统一的纯色或简单摄影棚背景；"
        "不出现文字、标签、水印、Logo、边框、拼贴或多视图。"
    ),
    "scene": (
        "画面纯净约束：只表现环境本身，不出现任何人物、动物、角色剪影或人体局部；"
        "不出现剧情中的可移动道具、手持物品或临时摆放物；"
        "不出现文字、标签、水印、Logo、边框、拼贴或多场景拼接。"
    ),
    "prop": (
        "画面纯净约束：只出现这一件道具，不出现人物、手部或无关物品；"
        "背景为干净统一的纯色或简单背景；"
        "不出现文字、标签、水印、Logo、边框或拼贴。"
    ),
}


def _purify_custom_prompt(compiled, params: dict) -> dict:
    """调用方自带提示词时补齐类型隔离约束，保证净化规则不因自定义提示词失效。"""
    out = dict(params)
    prompt = str(out.get("prompt") or "").strip()
    clause = _ASSET_PURITY_CLAUSE.get(compiled.asset_type or "")
    if prompt and clause and clause not in prompt:
        out["prompt"] = f"{prompt}\n{clause}"
    return out


def _compile_asset_prompt(db: Session, asset_type: str, entity_id: str, wf: Workflow,
                          params: dict) -> tuple[dict, dict]:
    """阶段 B：编译独立资产图提示词（单主体、干净背景、类型隔离）。"""
    from app.services.prompt_compiler import apply_to_params, compile_asset_image, validate_compilation

    try:
        raw = compile_asset_image(db, asset_type, entity_id, workflow=wf).to_dict()
    except ValueError as exc:
        raise errors.AppError("invalid_request", str(exc), 422)
    compiled = validate_compilation(raw)
    _compilation_blocked(compiled, 422)
    out = dict(params)
    if "prompt" not in out:
        out = apply_to_params(compiled, out)
    else:
        # 自带提示词时净化模板不再整体失效：补齐类型隔离禁令后继续走负向词落地
        out = _purify_custom_prompt(compiled, out)
    out = _finalize_negative(compiled, wf, out)
    return out, compiled.to_dict()


def _compile_shot_prompt(db: Session, shot: Shot, wf: Workflow, params: dict,
                         task: str, fallback_prompt: str = "") -> tuple[dict, dict]:
    """阶段 B：编译分镜图/分镜视频提示词，只组合镜头绑定且审核通过的资产。

    结构性错误硬阻断；资产未审核等软阻断沿用既有资产门禁语义，仅在无提示词时回退，
    并把解码结果完整写入快照，避免重复拦截破坏既有入口。
    """
    from app.services.prompt_compiler import (
        apply_to_params,
        compile_shot_video,
        compile_storyboard_image,
        validate_compilation,
    )

    raw = (compile_storyboard_image(db, shot.id, workflow=wf) if task == "keyframe"
           else compile_shot_video(db, shot.id, workflow=wf))
    compiled = validate_compilation(raw.to_dict())
    soft = _compilation_blocked(compiled, 409)
    out = dict(params)
    if soft and "prompt" not in out:
        out["prompt"] = fallback_prompt
        snapshot = compiled.to_dict()
        snapshot["deferred_blocking_issues"] = soft
        return out, snapshot
    if "prompt" not in out:
        out = apply_to_params(compiled, out)
    else:
        out = _apply_compiled_media(compiled, out)
    out = _finalize_negative(compiled, wf, out)
    snapshot = compiled.to_dict()
    if soft:
        snapshot["deferred_blocking_issues"] = soft
    return out, snapshot


def _workflow_frame_size(definition: dict | None) -> tuple[int, int] | None:
    """取工作流里 H3 视频节点的 width/height。"""
    if not isinstance(definition, dict):
        return None
    for node in definition.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") != "MiniMaxH3ReferenceToVideo":
            continue
        inputs = node.get("inputs") or {}
        width, height = inputs.get("width"), inputs.get("height")
        if isinstance(width, int) and isinstance(height, int):
            return width, height
    return None


def _apply_tier_frame_size(wf: Workflow, params: dict) -> dict:
    """档位工作流（capabilities.tier）自带画幅时，写入 width/height。

    只在调用方没显式指定时填；非档位工作流保持原行为（由 worker 按项目宽高比注入）。
    """
    if params.get("width") is not None and params.get("height") is not None:
        return params
    if not (wf.capabilities or {}).get("tier"):
        return params
    size = _workflow_frame_size(wf.definition)
    if size is None:
        return params
    out = dict(params)
    out.setdefault("width", size[0])
    out.setdefault("height", size[1])
    return out


def _create_job(db: Session, project_id: str, shot_id: str | None, target: str,
                wf: Workflow, params: dict, generation_mode: str | None = None,
                priority: int = 0) -> Job:
    """创建生成任务并持久化 effective_inputs 快照。

    priority 越大越先被 worker 取到（同优先级按入队顺序），用于人工发起的
    重新生成插队；普通任务保持 0。
    """
    job_type = f"generate_{target}" if target != "keyframe" else "generate_keyframe"
    if target == "clip":
        # 极速/标准/质量三档工作流各自带画幅，创建任务时就落到 params，
        # 避免 worker 再按项目宽高比覆盖
        params = _apply_tier_frame_size(wf, params)
    job = Job(
        id=ids.job_id(),
        project_id=project_id,
        shot_id=shot_id,
        job_type=job_type,
        workflow_id=wf.id,
        workflow_version=wf.version,
        params=params,
        status="queued",
        priority=priority,
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


def _inject_prev_shot_frame(db: Session, shot: Shot, target: str, params: dict) -> dict:
    """镜间尾帧依赖注入与校验（P0 整改）。

    - 同集非首镜生成 clip 时，注入前一镜实际尾帧为 __media__.ref_prev_frame
    - shot.requires_prev_frame 为 True 但拿不到可解码尾帧时明确报错（409），
      不静默绕过，也不用分镜图或其他镜头尾帧顶替
    - 集首镜由跨集连续性流程处理，此处不干预
    返回注入明细，供写入任务 input_snapshot 追溯。
    """
    info: dict = {"injected": False, "reason": None}
    if target != "clip":
        return info
    if _is_first_shot_of_episode(db, shot):
        # 仅集首镜交给跨集连续性流程；第 1 集首镜无上一集，自然不会注入
        info["reason"] = "first_shot_handled_by_continuity"
        return info
    from app.services.shot_chain import previous_shot, resolve_prev_frame
    prev = previous_shot(db, shot)
    if prev is None:
        info["reason"] = "no_previous_shot"
        return info
    frame = resolve_prev_frame(db, shot)
    if frame is None:
        if shot.requires_prev_frame:
            raise errors.AppError(
                "prev_frame_missing",
                f"镜头 {shot.id} 声明需要上一镜尾帧，但镜头 {prev.id} 尚无可用尾帧；"
                f"请先成功生成该镜头视频并完成尾帧提取",
                409,
            )
        info["reason"] = "prev_frame_unavailable"
        info["prev_shot_id"] = prev.id
        return info
    media = params.get("__media__", {})
    media.setdefault("ref_prev_frame", frame["file_path"])
    params["__media__"] = media
    info.update({
        "injected": True,
        "prev_shot_id": prev.id,
        "frame_asset_id": frame["asset_id"],
        "frame_version": frame["version"],
    })
    return info


def _assert_prev_frames_available(db: Session, shots: list, target: str) -> None:
    """批量 clip 提交前的尾帧依赖预检（P0）。

    批量接口在循环中逐个入队，若循环中途因缺少上一镜尾帧抛错，
    会出现部分任务已入队、部分未入队的半成品状态。因此先整体校验，
    存在声明需要尾帧却不可用的镜头时统一返回 409，不静默降级。
    """
    if target != "clip":
        return
    from app.services.shot_chain import previous_shot, resolve_prev_frame
    missing: list = []
    for shot in shots:
        if not shot.requires_prev_frame or _is_first_shot_of_episode(db, shot):
            continue
        if previous_shot(db, shot) is None:
            continue
        if resolve_prev_frame(db, shot) is None:
            missing.append(shot.id)
    if missing:
        ids = ", ".join(missing[:10])
        raise errors.AppError(
            "prev_frame_missing",
            f"以下镜头声明需要上一镜尾帧但当前不可用（{ids}{' 等' if len(missing) > 10 else ''}），"
            f"请先生成前序镜头视频并完成尾帧提取",
            409,
        )


def _apply_chain_mode(jobs: list[Job], chain_mode: str) -> None:
    """批量 clip 生成方式标记（批量出视频弹窗选择）。
    - independent：各镜头独立并行，执行时不注入上一镜尾帧
    - chained：按顺序串行，仅首任务 queued，其余置 chained_wait，
      由 worker 在前一任务成功且尾帧绑定后释放为 queued；
      前一任务失败时链条保持暂停，可重新发起批量继续
    """
    prev = None
    prev_scene_id = None
    for j in jobs:
        snap = dict(j.input_snapshot or {})
        snap["chain_mode"] = chain_mode
        cur_scene_id = (j.input_snapshot or {}).get("scene_id")
        if chain_mode == "chained" and prev is not None and cur_scene_id == prev_scene_id:
            j.status = "chained_wait"
            snap["chain"] = {"prev_job_id": prev.id, "mode": "chained"}
        j.input_snapshot = snap
        prev = j
        prev_scene_id = cur_scene_id


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


def _check_assets_reviewed(db: Session, project_id: str) -> None:
    """公共资产人工审核闸口（D-23）：生成分镜图前校验资产库已人工审核通过。

    统一委托 assets_gate 门禁服务：除审核状态外，同时校验必需资产
    指针/版本有效性/文件存在/在途生成任务，未就绪时返回结构化阻断明细
    （blockers：资产 ID/名称/类型/原因/建议操作）。
    """
    from app.services.assets_gate import assert_assets_gate

    assert_assets_gate(db, project_id)


def _check_keyframe_not_locked(db: Session, shot_ids: list[str]) -> None:
    """分镜图锁定保护（D-23 扩展）：已锁定的分镜图禁止被重新生成覆盖。

    锁定代表人工对某张分镜图的保护性确认，重绘会将该版本转 superseded，
    因此所有出图通道（单镜/重做/场景批量/跨场景批量）都须先校验。
    """
    if not shot_ids:
        return
    rows = (
        db.query(Job.shot_id)
        .join(Asset, Asset.source_job_id == Job.id)
        .filter(
            Job.shot_id.in_(shot_ids),
            Asset.asset_type == "keyframe",
            Asset.validity == "current",
            Asset.locked.is_(True),
        )
        .all()
    )
    if rows:
        locked = ", ".join(sorted({r[0] for r in rows if r[0]})[:10])
        raise errors.AppError(
            "asset_locked",
            f"以下镜头的分镜图已锁定，请先解锁再重新生成（{locked}）",
            409,
        )


def _check_character_portraits(db: Session, project_id: str) -> None:
    """角色定妆图闸口（P2 合规）：生成 keyframe 前校验项目内所有角色均已生成定妆图。"""
    missing = (
        db.query(Character)
        .filter(Character.project_id == project_id, Character.portrait_asset_id.is_(None))
        .all()
    )
    if missing:
        names = ", ".join(c.name for c in missing[:10])
        raise errors.AppError(
            "character_portrait_missing",
            f"以下角色未生成定妆图（{names}{' 等' if len(missing) > 10 else ''}），请先生成角色定妆图",
            409,
        )


def _check_scene_baselines(db: Session, project_id: str) -> None:
    """场景基准图闸口（P2 合规）：生成 keyframe 前校验项目内所有场景均已生成基准图。"""
    missing = (
        db.query(Scene)
        .filter(Scene.project_id == project_id, Scene.baseline_asset_id.is_(None))
        .all()
    )
    if missing:
        titles = ", ".join(s.title for s in missing[:10])
        raise errors.AppError(
            "scene_baseline_missing",
            f"以下场景未生成基准图（{titles}{' 等' if len(missing) > 10 else ''}），请先生成场景基准图",
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

def _check_asset_inflight(db: Session, entity_id: str, param_key: str, target: str) -> None:
    """资产图在途任务去重：同实体+同目标已有 queued/running 任务时返回 409，
    防止重复提交导致同一 ComfyUI prompt 被多次调用（浪费 ComfyUI 资源且可能出图结果异常）。
    
    entity_id 通过 job.params[param_key] 查询（如 __scene_id__ / __character_id__ / __prop_id__）。
    """
    job_type = f"generate_{target}" if target != "keyframe" else "generate_keyframe"
    # SQLite 无原生 JSONB 查询，取所有 queued/running 的资产类 Job 在 Python 中过滤
    candidates = (
        db.query(Job)
        .filter(
            Job.job_type == job_type,
            Job.status.in_(("queued", "running")),
        )
        .order_by(Job.created_at.desc())
        .all()
    )
    for j in candidates:
        if (j.params or {}).get(param_key) == entity_id:
            raise errors.AppError(
                "job_in_flight",
                f"该资产已有同目标在途任务（{j.id}，状态 {j.status}），请等待完成或先取消",
                409,
            )



def _cancel_inflight_jobs(db: Session, shot_id: str, target: str) -> list[str]:
    """重新生成前取消该镜头同目标的在途任务，释放 ComfyUI 显卡资源。

    running 的生成任务调用 ComfyUI /interrupt（多实例按 input_snapshot.comfyui_instance 定向中断），
    queued/chained_wait/submission_unknown 直接本地置 cancelled。
    返回被取消的任务 ID 列表，用于前端提示与追溯。
    """
    job_type = f"generate_{target}" if target != "keyframe" else "generate_keyframe"
    inflight = (
        db.query(Job)
        .filter(
            Job.shot_id == shot_id,
            Job.job_type == job_type,
            Job.status.in_(("queued", "running", "chained_wait", "submission_unknown")),
        )
        .all()
    )
    cancelled: list[str] = []
    for j in inflight:
        if j.status == "running" and j.prompt_id:
            try:
                from app.integrations.comfyui import ComfyUIClient
                used_url = (j.input_snapshot or {}).get("comfyui_instance")
                if used_url:
                    ComfyUIClient(base_url=used_url).interrupt()
                else:
                    ComfyUIClient().interrupt()
            except Exception as e:  # 中断失败不阻断本地取消，记入事件
                db.add(JobEvent(job_id=j.id, event_type="job.interrupt_failed",
                                payload={"reason": type(e).__name__}, created_at=_now()))
        j.status = "cancelled"
        j.finished_at = _now()
        j.prompt_id = None  # 清除 prompt_id，防止 scheduler 续跑分支复用已取消任务
        db.add(JobEvent(job_id=j.id, event_type="job.cancelled",
                        payload={"reason": "shot_clip_regenerate"}, created_at=_now()))
        cancelled.append(j.id)
    if cancelled:
        db.commit()
    return cancelled


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
            Job.status.in_(("queued", "running", "chained_wait")),
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


def _filter_already_generated(db: Session, shots: list, target: str,
                             wf_id: str | None, wf_version: int | None) -> tuple[list, list]:
    """批量生成幂等过滤（仅分镜视频 clip）：跳过已由同一工作流生成过当前有效视频的镜头。

    判定口径：镜头存在 validity=current 的 clip 资产，其来源任务 succeeded，
    且该任务的 workflow_id 与本次批量使用的工作流一致。
    目的：重复点击批量出视频不会对已产出的镜头再次创建任务、重复消耗 ComfyUI。
    重新生成走详情页的 regenerate-clip / regenerate 接口，不受此处限制。
    """
    if target != "clip" or not wf_id or not shots:
        return shots, []
    shot_ids = [s.id for s in shots]
    rows = (
        db.query(Job.shot_id)
        .join(Asset, Asset.source_job_id == Job.id)
        .filter(
            Job.shot_id.in_(shot_ids),
            Job.job_type == "generate_clip",
            Job.status == "succeeded",
            Job.workflow_id == wf_id,
            Asset.asset_type == "clip",
            Asset.validity == "current",
        )
        .distinct()
        .all()
    )
    done = {r[0] for r in rows if r[0]}
    if not done:
        return shots, []
    ok, skipped = [], []
    for s in shots:
        if s.id in done:
            skipped.append({
                "shot_id": s.id,
                "reason": "already_generated",
                "workflow_id": wf_id,
                "workflow_version": wf_version,
            })
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
    if target == "keyframe":
        # 人工审核闸口（D-23）：公共资产未审核通过时，禁止生成分镜图
        _check_assets_reviewed(db, scene.project_id)
        # 锁定保护：已锁定的分镜图不允许被覆盖
        _check_keyframe_not_locked(db, [shot.id])
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
                # P0 错位修复：按实际注入槽位重写提示词图片引用编号并统一为工作流语法
                from app.services.ref_inject import rewrite_picture_refs
                params["prompt"] = rewrite_picture_refs(
                    db, scene.project_id, params.get("prompt") or "",
                    params["__media__"], slots)
    # Reference Resolver（PROP_BINDING_PLAN P0）：keyframe 提交前解析参考图清单，
    # 组合图优先（character_prop_binding）→ 独立图回退 → offscreen/absent 保护；
    # 语义槽位输出与 build_media_params 兼容格式合并（Resolver 结果优先），
    # 完整 Manifest 写入 job.input_snapshot 供追溯（验收第 9 条）。
    _reference_manifest = None
    if target == "keyframe":
        from app.services.reference_resolver import resolve_shot_references
        _reference_manifest = resolve_shot_references(db, shot.id, scene.project_id)
        _resolver_media = {
            s.slot: s.file_path for s in _reference_manifest.slots if s.file_path
        }
        if _resolver_media:
            _existing = params.get("__media__", {})
            _existing.update(_resolver_media)
            params["__media__"] = _existing
    # 阶段 B：分镜图/分镜视频提示词编译——只组合镜头绑定且审核通过的资产，
    # 结构性错误（槽位超限、参考文件缺失、槽位重复）硬阻断，不静默丢图。
    compilation = None
    if target in ("keyframe", "clip"):
        _fallback = params.get("prompt") or shot.image_prompt or ""
        params, compilation = _compile_shot_prompt(db, shot, wf, params, target, _fallback)

    # 镜间尾帧依赖（P0）：非首镜使用前序镜头实际尾帧，缺失时按声明决定拦截或记录
    prev_frame_info = _inject_prev_shot_frame(db, shot, target, params)
    job = _create_job(db, scene.project_id, shot.id, target, wf, params,
                      generation_mode=payload.get("generation_mode"))
    _attach_compilation(job, compilation)
    if _reference_manifest is not None:
        _snap = dict(job.input_snapshot or {})
        _snap["reference_manifest"] = _reference_manifest.to_dict()
        job.input_snapshot = _snap
    if target == "clip":
        # 无论是否注入成功都记录判定依据，便于事后追溯串图与缺帧问题
        _snap = dict(job.input_snapshot or {})
        _snap["prev_frame"] = prev_frame_info
        job.input_snapshot = _snap
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
    if target == "keyframe":
        # 人工审核闸口（D-23）：重做分镜图属于出图通道，同样要求公共资产已审核通过
        _check_assets_reviewed(db, scene.project_id)
        # 锁定保护：已锁定的分镜图不允许被重做覆盖
        _check_keyframe_not_locked(db, [shot.id])
    if target == "clip":
        _check_keyframe_gate(db, [shot.id])
    # 在途任务去重：同镜头同目标已有 queued/running 任务时拒绝重复提交
    _check_inflight_duplicate(db, shot.id, target)
    # 旧资产转历史版本 + 下游失效传播
    # - keyframe 重做：分镜图会被重绘，本镜确认状态回退（D-13）
    # - clip 重做：分镜图不变，不回退确认状态；旧尾帧置 superseded，
    #   使下游镜头解析前序尾帧时拿不到旧版本（P0 失效传播）
    chain_info: dict = {}
    if target in ("keyframe", "clip"):
        from app.services.shot_chain import invalidate_chain_after_regenerate
        chain_info = invalidate_chain_after_regenerate(
            db, shot, reset_keyframe_confirmed=(target == "keyframe")
        )
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
                # P0 错位修复：按实际注入槽位重写提示词图片引用编号并统一为工作流语法
                from app.services.ref_inject import rewrite_picture_refs
                params["prompt"] = rewrite_picture_refs(
                    db, scene.project_id, params.get("prompt") or "",
                    params["__media__"], slots)
    # Reference Resolver（PROP_BINDING_PLAN P0）：重做与单镜生成行为一致，
    # 组合图优先（character_prop_binding）→ 独立图回退 → offscreen/absent 保护；
    # Resolver 结果覆盖同名语义槽位，完整 Manifest 写入 input_snapshot 供追溯。
    _reference_manifest = None
    if target == "keyframe":
        from app.services.reference_resolver import resolve_shot_references
        _reference_manifest = resolve_shot_references(db, shot.id, scene.project_id)
        _resolver_media = {
            s.slot: s.file_path for s in _reference_manifest.slots if s.file_path
        }
        if _resolver_media:
            _existing = params.get("__media__", {})
            _existing.update(_resolver_media)
            params["__media__"] = _existing
    # 镜间尾帧依赖（P0）：重做与单镜生成行为一致
    prev_frame_info = _inject_prev_shot_frame(db, shot, target, params)
    job = _create_job(db, scene.project_id, shot.id, target, wf, params)
    if target == "clip" or chain_info or _reference_manifest is not None:
        _snap = dict(job.input_snapshot or {})
        if target == "clip":
            _snap["prev_frame"] = prev_frame_info
        if chain_info:
            _snap["chain_invalidation"] = chain_info
        if _reference_manifest is not None:
            _snap["reference_manifest"] = _reference_manifest.to_dict()
        job.input_snapshot = _snap
    db.commit()
    return _job_out(job)


@router.post("/shots/{shot_id}/regenerate-clip", status_code=202)
def regenerate_shot_clip(shot_id: str, payload: dict | None = None,
                         db: Session = Depends(get_db)) -> dict:
    """重新生成分镜视频：覆盖原视频，并把该镜头绑定到新任务。

    - 提交前先取消该镜头所有在途 generate_clip 任务（running 任务调用 ComfyUI /interrupt），
      取消后不再占用 ComfyUI 显卡资源；
    - 原有视频资产转历史版本（validity=superseded），不物理删除，便于追溯；
    - 新任务成功后写入新的 current 视频资产，即『镜头绑定到新任务』。
    返回新任务 + 被取消的历史任务 ID 列表。
    """
    payload = payload or {}
    shot = _get_shot(db, shot_id)
    scene = db.get(Scene, shot.scene_id)
    target = "clip"
    # 关键帧门禁：未确认分镜图不允许生成视频
    _check_keyframe_gate(db, [shot.id])
    # 先取消历史在途任务（含 running 的 ComfyUI 中断），释放显卡资源并解除在途去重
    cancelled = _cancel_inflight_jobs(db, shot.id, target)
    # 旧视频转历史版本 + 下游尾帧失效传播（分镜图不变，不回退关键帧确认状态）
    olds = (
        db.query(Asset)
        .join(AssetRef, AssetRef.asset_id == Asset.id)
        .filter(AssetRef.shot_id == shot.id, Asset.asset_type == "clip",
                Asset.validity == "current")
        .all()
    )
    if olds:
        from app.services.shot_chain import invalidate_chain_after_regenerate
        invalidate_chain_after_regenerate(db, shot, reset_keyframe_confirmed=False)
        for a in olds:
            a.validity = "superseded"
    wf = _match_workflow(db, target, payload.get("workflow_id"))
    params = dict(payload.get("params") or {})
    wf = _inject_continuity_first_frame(db, shot, target, wf, params)
    params.setdefault("prompt", shot.video_prompt or shot.image_prompt or "")
    # @引用注入（D-16）：解析提示词中的 @角色/@场景，自动携带参考图
    from app.services.ref_inject import build_media_params
    prompt_text = params.get("prompt") or ""
    if prompt_text:
        slots = (wf.capabilities or {}).get("reference_slots") if wf.capabilities else None
        media = build_media_params(db, scene.project_id, prompt_text, slots)
        if media:
            _existing = params.get("__media__", {})
            _existing.update(media)
            params["__media__"] = _existing
            # P0 错位修复：按实际注入槽位重写提示词图片引用编号并统一为工作流语法
            from app.services.ref_inject import rewrite_picture_refs
            params["prompt"] = rewrite_picture_refs(
                db, scene.project_id, params.get("prompt") or "",
                params["__media__"], slots)
    # 阶段 B：提示词编译（槽位超限/参考图缺失硬阻断）
    _fallback = params.get("prompt") or shot.image_prompt or ""
    params, compilation = _compile_shot_prompt(db, shot, wf, params, target, _fallback)
    # 镜间尾帧依赖（P0）
    prev_frame_info = _inject_prev_shot_frame(db, shot, target, params)
    job = _create_job(db, scene.project_id, shot.id, target, wf, params,
                      generation_mode=payload.get("generation_mode"),
                      priority=_REGENERATE_CLIP_PRIORITY)
    _attach_compilation(job, compilation)
    _snap = dict(job.input_snapshot or {})
    _snap["prev_frame"] = prev_frame_info
    _snap["replaced_job_ids"] = cancelled
    _snap["priority_reason"] = "shot_clip_regenerate"
    job.input_snapshot = _snap
    db.commit()
    out = _job_out(job)
    out["cancelled_job_ids"] = cancelled
    return out


@router.post("/characters/{character_id}/generate", status_code=202)
def generate_character(character_id: str, payload: dict | None = None, db: Session = Depends(get_db)) -> dict:
    """生成角色定妆图：target 固定 keyframe，appearance 为 prompt 基础，无需参考图。"""
    payload = payload or {}
    ch = db.get(Character, character_id)
    if ch is None:
        raise errors.AppError("not_found", "角色不存在", 404)
    wf = _match_pure_asset_workflow(db, payload.get("workflow_id"))
    params = dict(payload.get("params") or {})
    # 阶段 B：角色资产图提示词由编译器从结构化资料 + 集中模板产出（单角色/全身/无场景）
    # 必填外观缺失时硬阻断 422，不静默生成占位提示词
    _check_asset_inflight(db, ch.id, "__character_id__", "keyframe")
    from app.services.asset_prompts import ASSET_PROMPTS_VERSION
    params, compilation = _compile_asset_prompt(db, "character", ch.id, wf, params)
    params["__asset_prompt_version__"] = ASSET_PROMPTS_VERSION
    job = _create_job(db, ch.project_id, None, "keyframe", wf, params)
    _attach_compilation(job, compilation)
    job.params = {**job.params, "__character_id__": ch.id}
    # 人工审核闸口（D-23）：公共资产重新生成后原审核结论失效，需重新提交审核
    from app.services.assets_review import reset_assets_review
    reset_assets_review(db, ch.project_id)
    db.commit()
    return _job_out(job)


@router.post("/props/{prop_id}/generate", status_code=202)
def generate_prop(prop_id: str, payload: dict | None = None, db: Session = Depends(get_db)) -> dict:
    """生成道具锚定图：规则同角色定妆图。"""
    payload = payload or {}
    p = db.get(Prop, prop_id)
    if p is None:
        raise errors.AppError("not_found", "道具不存在", 404)
    wf = _match_pure_asset_workflow(db, payload.get("workflow_id"))
    params = dict(payload.get("params") or {})
    # 阶段 B：道具资产图提示词编译（单件道具/无手无人物/无场景背景）
    _check_asset_inflight(db, p.id, "__prop_id__", "keyframe")
    from app.services.asset_prompts import ASSET_PROMPTS_VERSION
    params, compilation = _compile_asset_prompt(db, "prop", p.id, wf, params)
    params["__asset_prompt_version__"] = ASSET_PROMPTS_VERSION
    job = _create_job(db, p.project_id, None, "keyframe", wf, params)
    _attach_compilation(job, compilation)
    job.params = {**job.params, "__prop_id__": p.id}
    # 人工审核闸口（D-23）：公共资产重新生成后原审核结论失效，需重新提交审核
    from app.services.assets_review import reset_assets_review
    reset_assets_review(db, p.project_id)
    db.commit()
    return _job_out(job)


@router.post("/scenes/{scene_id}/generate-baseline", status_code=202)
def generate_scene_baseline(scene_id: str, payload: dict | None = None, db: Session = Depends(get_db)) -> dict:
    """生成场景基准图：target 固定 keyframe，title+description 为 prompt 基础。"""
    payload = payload or {}
    sc = db.get(Scene, scene_id)
    if sc is None:
        raise errors.AppError("scene_not_found", "场景不存在", 404)
    # 场景基准图必须使用无参考图的纯文生图工作流（D-18 修订 D-03b：
    # 带参考图槽位的工作流会在无参考图时由模型自行补全主体，导致画面中出现人物与道具）。
    # 显式指定 workflow_id 时信任调用方，但校验其 reference_slots 必须为空。
    wf_id = payload.get("workflow_id")
    if wf_id:
        wf = db.get(Workflow, wf_id)
        if wf is None:
            raise errors.AppError("workflow_not_found", "工作流不存在", 404)
        if (wf.capabilities or {}).get("reference_slots"):
            raise errors.AppError(
                "invalid_request",
                "场景基准图只允许无参考图的纯文生图工作流，当前工作流声明了 reference_slots",
                422,
            )
    else:
        wf = db.query(Workflow).filter(Workflow.name == "wf_image_t2i_qwen21_turbo").first()
        if wf is None or wf.workflow_type != "image":
            raise errors.AppError("workflow_not_found", "纯文生图工作流 wf_image_t2i_qwen21_turbo 不存在", 404)
    params = dict(payload.get("params") or {})
    # 阶段 B：场景资产图提示词编译（纯环境/无人无角色/空间结构完整）
    _check_asset_inflight(db, sc.id, "__scene_id__", "keyframe")
    from app.services.asset_prompts import ASSET_PROMPTS_VERSION
    params, compilation = _compile_asset_prompt(db, "scene", sc.id, wf, params)
    params["__asset_prompt_version__"] = ASSET_PROMPTS_VERSION
    job = _create_job(db, sc.project_id, None, "keyframe", wf, params)
    _attach_compilation(job, compilation)
    job.params = {**job.params, "__scene_id__": sc.id}
    # 人工审核闸口（D-23）：公共资产重新生成后原审核结论失效，需重新提交审核
    from app.services.assets_review import reset_assets_review
    reset_assets_review(db, sc.project_id)
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
    if target == "keyframe":
        # 人工审核闸口（D-23）：公共资产未审核通过时，禁止批量生成分镜图
        _check_assets_reviewed(db, sc.project_id)
        # 锁定保护（D-23 扩展）：已锁定的分镜图不允许被场景批量重绘覆盖
        _check_keyframe_not_locked(db, [s.id for s in shots])
    if target == "clip":
        _check_keyframe_gate(db, [s.id for s in shots])
        # 尾帧依赖预检（P0）：避免循环中途失败导致部分任务已入队
        _assert_prev_frames_available(db, shots, target)
    wf = _match_workflow(db, target, payload.get("workflow_id"))
    # 幂等过滤（P0）：同一工作流已生成过视频的镜头直接跳过，避免重复渲染；
    # 需要重做时走分镜视频详情页的重新生成，不受此处限制
    shots, already_done = _filter_already_generated(db, shots, target, wf.id, wf.version)
    if already_done:
        skipped.extend(already_done)
    if not shots:
        raise errors.AppError(
            "already_generated",
            f"选中镜头均已由该工作流生成过视频（{len(already_done)} 个），如需重做请在分镜视频详情页重新生成",
            409,
        )
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
                    # P0 错位修复：按实际注入槽位重写提示词图片引用编号并统一为工作流语法
                    from app.services.ref_inject import rewrite_picture_refs
                    params["prompt"] = rewrite_picture_refs(
                        db, sc.project_id, params.get("prompt") or "",
                        params["__media__"], slots)
        # 镜间尾帧依赖（P0）：批量与单镜生成行为一致
        prev_frame_info = _inject_prev_shot_frame(db, shot, target, params)
        _job = _create_job(db, sc.project_id, shot.id, target, shot_wf, params)
        if target == "clip":
            _snap = dict(_job.input_snapshot or {})
            _snap["prev_frame"] = prev_frame_info
            _job.input_snapshot = _snap
        jobs.append(_job)
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
    # 显式指定 shot_ids 视为调用方已明确目标（如分镜图环节批量补齐），不设数量下限；
    # 未指定时按筛选条件批量操作，要求至少 3 个镜头，避免误用批量接口做单镜生成
    if not payload.get("shot_ids") and len(shots) < 3:
        raise errors.AppError("invalid_request", "选中镜头少于 3 个，请使用单镜生成接口", 400)
    # 在途任务过滤：跳过已有同目标在途任务的镜头，避免重复提交
    shots, skipped = _filter_inflight(db, shots, target)
    if not shots:
        raise errors.AppError(
            "job_in_flight",
            f"所有选中镜头均已有同目标在途任务（{len(skipped)} 个），请等待完成或先取消",
            409,
        )
    if target == "keyframe":
        # 人工审核闸口（D-23）：公共资产未审核通过时，禁止批量生成分镜图
        _check_assets_reviewed(db, project_id)
        # 锁定保护：已锁定的分镜图不允许被批量覆盖
        _check_keyframe_not_locked(db, [s.id for s in shots])
        # 前置闸口（P2 合规）：生成分镜图前校验角色定妆图与场景基准图齐全
        _check_character_portraits(db, project_id)
        _check_scene_baselines(db, project_id)
    # 批量生成方式（仅 clip）：independent 独立并行（不依赖上一镜尾帧）/ chained 链式串行（逐镜衔接）
    chain_mode = payload.get("chain_mode") if target == "clip" else None
    if chain_mode is not None and chain_mode not in ("independent", "chained"):
        raise errors.AppError("invalid_request", "chain_mode 仅允许 independent/chained", 400)
    if target == "clip":
        _check_keyframe_gate(db, [s.id for s in shots])
        # 尾帧依赖预检（P0）：仅传统模式（未指定 chain_mode）执行；
        # independent 不依赖尾帧；chained 由链式任务在执行时解析上一镜尾帧
        if chain_mode is None:
            _assert_prev_frames_available(db, shots, target)
    wf = _match_workflow(db, target, payload.get("workflow_id"))
    # 幂等过滤（P0）：同一工作流已生成过视频的镜头直接跳过，避免重复渲染；
    # 需要重做时走分镜视频详情页的重新生成，不受此处限制
    shots, already_done = _filter_already_generated(db, shots, target, wf.id, wf.version)
    if already_done:
        skipped.extend(already_done)
    if not shots:
        raise errors.AppError(
            "already_generated",
            f"选中镜头均已由该工作流生成过视频（{len(already_done)} 个），如需重做请在分镜视频详情页重新生成",
            409,
        )
    jobs = []
    slots = (wf.capabilities or {}).get("reference_slots") if wf.capabilities else None
    for shot in shots:
        params = dict(payload.get("params") or {})
        if target == "keyframe":
            params.setdefault("prompt", shot.image_prompt or "")
        else:
            params.setdefault("prompt", shot.video_prompt or shot.image_prompt or "")
        shot_wf = wf
        if target == "clip":
            shot_wf = _inject_continuity_first_frame(db, shot, target, wf, params)
        if target == "keyframe" and params.get("prompt"):
            from app.services.shot_refs import inject_asset_refs
            params["prompt"] = inject_asset_refs(db, shot, params["prompt"])
        if target in ("keyframe", "clip"):
            from app.services.ref_inject import build_media_params
            prompt_text = params.get("prompt") or ""
            if prompt_text:
                media = build_media_params(db, project_id, prompt_text, slots)
                if media:
                    _existing = params.get("__media__", {})
                    _existing.update(media)
                    params["__media__"] = _existing
                    # P0 错位修复：按实际注入槽位重写提示词图片引用编号并统一为工作流语法
                    from app.services.ref_inject import rewrite_picture_refs
                    params["prompt"] = rewrite_picture_refs(
                        db, project_id, params.get("prompt") or "",
                        params["__media__"], slots)
        # Reference Resolver（与单镜生成保持一致）：keyframe 批量生成时解析参考图清单，
        # 组合图优先 → 独立图回退 → offscreen 保护；Resolver 结果优先合并到 __media__
        _reference_manifest = None
        if target == "keyframe":
            from app.services.reference_resolver import resolve_shot_references
            _reference_manifest = resolve_shot_references(db, shot.id, project_id)
            _resolver_media = {
                s.slot: s.file_path for s in _reference_manifest.slots if s.file_path
            }
            if _resolver_media:
                _existing = params.get("__media__", {})
                _existing.update(_resolver_media)
                params["__media__"] = _existing
        # 提示词编译（与单镜生成保持一致）：结构性校验（槽位超限/参考文件缺失/槽位重复）硬阻断
        compilation = None
        if target in ("keyframe", "clip"):
            _fallback = params.get("prompt") or shot.image_prompt or ""
            params, compilation = _compile_shot_prompt(db, shot, shot_wf, params, target, _fallback)
        # 镜间尾帧依赖（P0）：仅传统模式在提交时注入；
        # independent 不注入；chained 由执行期 ref_prev_frame 注入逻辑解析上一镜尾帧
        prev_frame_info = None
        if target == "clip" and chain_mode is None:
            prev_frame_info = _inject_prev_shot_frame(db, shot, target, params)
        _job = _create_job(db, project_id, shot.id, target, shot_wf, params)
        _attach_compilation(_job, compilation)
        if _reference_manifest is not None:
            _snap = dict(_job.input_snapshot or {})
            _snap["reference_manifest"] = _reference_manifest.to_dict()
            _job.input_snapshot = _snap
        if target == "clip":
            _snap = dict(_job.input_snapshot or {})
            _snap["scene_id"] = shot.scene_id
            if prev_frame_info is not None:
                _snap["prev_frame"] = prev_frame_info
            _job.input_snapshot = _snap
        jobs.append(_job)
    # 生成方式标记：chained 仅首任务 queued，其余 chained_wait 等待前一任务成功后释放
    if target == "clip" and chain_mode:
        _apply_chain_mode(jobs, chain_mode)
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
