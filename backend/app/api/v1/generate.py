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
from app.services.execution_verifier import find_active_job_by_request_key
from app.services.workflow.capabilities import declared_reference_slots, workflow_tier

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


def _match_workflow_exact(db: Session, target: str, workflow_id: str | None,
                          project_id: str | None = None) -> Workflow:
    """工作流匹配：显式 workflow_id 优先；否则 项目默认 → 默认 → 内置 → 任意。

    项目级默认来自 `project.settings.default_workflows`（v2 §11.3/§19，G2）。
    项目默认同样受能力校验约束——由调用方通过 matcher 校验，此处只负责定位。
    """
    if workflow_id:
        wf = db.get(Workflow, workflow_id)
        if wf is None:
            raise errors.AppError("workflow_not_found", "工作流不存在", 404)
        return wf
    wf_type = _TARGET_WF_TYPE.get(target)
    if wf_type is None:
        raise errors.AppError("invalid_request", f"不支持的 target: {target}", 400)
    # 0. 项目级默认工作流优先（项目设置里显式配置过才算）
    if project_id:
        from app.services.workflow import matcher as _matcher
        _slot = _matcher.TARGET_SLOT.get(target)
        _defaults = _matcher.project_defaults(db, project_id)
        _pid = _defaults.get(_slot) if _slot else None
        if _pid:
            _wf = db.get(Workflow, _pid)
            if _wf is None:
                raise errors.AppError(
                    "workflow_not_found",
                    f"项目默认工作流不存在（{target}）：{_pid}", 404,
                )
            if _wf.workflow_type != wf_type:
                raise errors.AppError(
                    "workflow_capability_mismatch",
                    f"项目默认工作流 {_wf.name} 类型为 {_wf.workflow_type}，"
                    f"与 {target} 要求的 {wf_type} 不符", 422,
                )
            return _wf
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
        slots = declared_reference_slots(wf.capabilities)
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
        slots = declared_reference_slots(wf.capabilities)
        names = [item.get("param") if isinstance(item, dict) else item for item in slots]
        if "ref_image" in names:
            tier = workflow_tier(wf.capabilities)
            candidates.append((_VIDEO_TIER_ORDER.get(tier, 99), wf))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def _match_workflow(db: Session, target: str, workflow_id: str | None,
                    project_id: str | None = None) -> Workflow:
    """工作流匹配：显式 workflow_id 优先；分镜图默认自动改用带参考槽位的工作流。

    默认图片工作流是无参考的纯文生图（0 槽位），而分镜图提示词通常带 @角色，
    直接用会在 ref_inject 里报"当前工作流仅支持 0 个角色参考槽位"。

    project_id 非空时，项目级默认（`settings.default_workflows`）优先于系统默认（G2）。
    """
    wf = _match_workflow_exact(db, target, workflow_id, project_id)
    # 项目默认命中时不再做「无槽位就换」的兜底——用户显式配置的工作流必须被尊重，
    # 能力不足由 matcher 报错，不静默替换（v2 §24 第 16 条）。
    _project_default_hit = bool(
        project_id and workflow_id is None
        and _is_project_default(db, project_id, target, wf.id)
    )
    if not _project_default_hit and workflow_id is None and target == "keyframe":
        if not declared_reference_slots(wf.capabilities):
            alt = _find_ref_capable_image_workflow(db)
            if alt is not None:
                return alt
    if not _project_default_hit and workflow_id is None and target == "clip" and not wf.is_default:
        slots = declared_reference_slots(wf.capabilities)
        names = [item.get("param") if isinstance(item, dict) else item for item in slots]
        if "ref_image" not in names:
            alt = _find_ref_capable_video_workflow(db)
            if alt is not None:
                return alt
    return wf


def _is_project_default(db: Session, project_id: str, target: str, workflow_id: str) -> bool:
    """该工作流是否就是项目为该 target 配置的默认工作流。"""
    from app.services.workflow import matcher as _matcher
    slot = _matcher.TARGET_SLOT.get(target)
    if not slot:
        return False
    return _matcher.project_defaults(db, project_id).get(slot) == workflow_id


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


def _prompt_fingerprint(params: dict) -> str:
    """最终下发提示词的内容指纹：重生成/手工编辑后能区分不同版本。"""
    import hashlib

    text = str(params.get("prompt") or params.get("tts_text") or "")
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _attach_compilation(job: Job, compilation: dict | None) -> None:
    """把阶段 B 编译结果写入 input_snapshot，便于复现与排障。"""
    if not compilation:
        return
    snap = dict(job.input_snapshot or {})
    snap["prompt_compilation"] = compilation
    version = snap.get("prompt_version")
    if not isinstance(version, dict):
        version = {"fingerprint": _prompt_fingerprint(job.params or {})}
    version.update({
        "template_id": compilation.get("template_id"),
        "template_version": compilation.get("template_version"),
    })
    snap["prompt_version"] = version
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
    """只合并编译出的参考图槽位，不改写用户已确认的提示词。

    但**空提示词必须用编译结果填充**：调用方用
    ``params.setdefault("prompt", shot.image_prompt or "")`` 建初始值，
    当 ``image_prompt`` 为空时 prompt 键已存在且为空串，于是
    ``_compile_shot_prompt`` 里 ``if "prompt" not in out`` 为 False，
    走进本函数——若这里也只合并槽位，最终下发的 prompt 就是空串，
    工作流收到空提示词。

    实测（sht_045190812cb5）：image_prompt=None 时 params['prompt'] 长度 0，
    编译出的 STORYBOARD_TEMPLATE 全文（含「硬性要求」分段）被整段丢弃。
    这在「LLM 只产出结构化事实、提示词由模板渲染」的两段式改造后是必现路径，
    不是边缘情况，因此在这里补上填充逻辑。
    """
    out = dict(params)
    if not str(out.get("prompt") or "").strip():
        # 仅在提示词为空时填充；已有内容（用户手改 / 重新生成时的自定义提示词）不覆盖
        compiled_text = (getattr(compiled, "positive_prompt", "") or "").strip()
        if compiled_text:
            out["prompt"] = compiled_text
    if compiled.reference_slots:
        media = dict(out.get("__media__") or {})
        for slot in compiled.reference_slots:
            media[slot["slot_key"]] = slot["file_path"]
        out["__media__"] = media
    return out


def _finalize_negative(compiled, wf: Workflow, params: dict) -> dict:
    """负向提示词仅在当前工作流真实绑定且实际生效时注入，并显式记录降级原因。

    两种不生效场景都不再静默：
    - 工作流没有 `$negative` 绑定（H3 的 BasicGuider 无负向分支）：直接剔除；
    - 绑定了但 CFGGuider.cfg<=1（蒸馏模型）：负向被数学抵消，剔除并告警。
    原因是此前静默 pop 会让用户以为负向约束已生效，实际模型从未收到。
    """
    out = dict(params)
    if not compiled.negative_prompt:
        return out

    def _warn_once(code: str, message: str) -> None:
        if not any(str(w).startswith(code + ":") for w in compiled.warnings):
            compiled.warnings.append(message)

    if not _workflow_binds_param(wf, "negative"):
        out.pop("negative", None)
        _warn_once(
            "negative_prompt_unsupported",
            "negative_prompt_unsupported: 当前工作流未绑定 negative 参数，负向提示词仅存档不生效",
        )
        return out

    from app.services.prompt_compiler import effective_cfg

    _cfg = effective_cfg(getattr(wf, "definition", None))
    if _cfg is not None and _cfg <= 1.05:
        out.pop("negative", None)
        _warn_once(
            "negative_prompt_ineffective",
            f"negative_prompt_ineffective: 当前工作流 CFGGuider.cfg={_cfg}，"
            "负向分支在 cfg=1 时被数学抵消，负向提示词不会生效；"
            "约束请改写为正向肯定句，或改用非蒸馏（cfg 4-7）工作流",
        )
        return out

    out.setdefault("negative", compiled.negative_prompt)
    return out


def _match_pure_asset_workflow(db: Session, workflow_id: str | None,
                               project_id: str | None = None) -> Workflow:
    """资产图专用工作流匹配：优先无参考图槽位的纯文生图工作流。

    带参考图槽位的工作流在未提供参考图时，模型会自行补全主体，
    导致角色图、道具图中混入无关人物、场景与杂物，破坏资产纯净度。

    选择优先级（G6，与 matcher 一致）：显式 workflow_id → 项目默认(image 槽位)
    → 系统默认 → 内置 → 任意无参考槽位。项目默认同样受「无 reference_slots」
    约束——配置了带参考槽位的工作流时明确报错，不静默换用别的（v2 §24 第 16 条）。
    """
    if workflow_id:
        wf = db.get(Workflow, workflow_id)
        if wf is None:
            raise errors.AppError("workflow_not_found", "工作流不存在", 404)
        if declared_reference_slots(wf.capabilities):
            raise errors.AppError(
                "invalid_request",
                "资产图只允许无参考图的纯文生图工作流，当前工作流声明了 reference_slots",
                422,
            )
        return wf
    # 项目级默认（资产图共用 image 槽位：角色定妆图/场景基准图/道具锚定图）
    if project_id:
        from app.services.workflow import matcher as _matcher
        _pid = _matcher.project_defaults(db, project_id).get("image")
        if _pid:
            _wf = db.get(Workflow, _pid)
            if _wf is None:
                raise errors.AppError(
                    "workflow_not_found", f"项目默认资产图工作流不存在：{_pid}", 404,
                )
            if _wf.workflow_type != "image":
                raise errors.AppError(
                    "workflow_capability_mismatch",
                    f"项目默认资产图工作流 {_wf.name} 类型为 {_wf.workflow_type}，应为 image", 422,
                )
            if declared_reference_slots(_wf.capabilities):
                raise errors.AppError(
                    "workflow_capability_mismatch",
                    f"项目默认资产图工作流 {_wf.name} 声明了参考图槽位；"
                    "资产图必须使用无参考图的纯文生图工作流，请到项目工作流配置中改选", 422,
                )
            return _wf
    candidates = (
        db.query(Workflow)
        .filter(Workflow.workflow_type == "image")
        .order_by(Workflow.is_default.desc(), Workflow.is_builtin.desc())
        .all()
    )
    for wf in candidates:
        if not declared_reference_slots(wf.capabilities):
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
        to_h3_clip_prompt,
        validate_compilation,
    )

    raw = (compile_storyboard_image(db, shot.id, workflow=wf) if task == "keyframe"
           else compile_shot_video(db, shot.id, workflow=wf))
    compiled = validate_compilation(raw.to_dict())
    soft = _compilation_blocked(compiled, 409)
    out = dict(params)
    if soft and "prompt" not in out:
        out["prompt"] = fallback_prompt
        # 历史 video_prompt 里的『台词/对白/第N段』标签会被 H3 一起念出来，统一改成可播报格式
        if task == "clip":
            out["prompt"] = to_h3_clip_prompt(shot, out["prompt"])
        snapshot = compiled.to_dict()
        snapshot["deferred_blocking_issues"] = soft
        return out, snapshot
    # keyframe 提示词统一走编译结果（v2 模板含 ref_duty_block/action_moment/aspect_desc），
    # 不再因「params 里已有 prompt」（来自解析期 image_prompt）而跳过编译。
    # clip 暂保持「有则保留」策略（视频提示词可能被人工精修）。
    if task == "keyframe" or "prompt" not in out:
        if compiled.status != "ready":
            # 编译未就绪（典型：绑定的参考图质检不合格）时 apply_to_params 会抛裸
            # ValueError，接口层表现为 500 且响应体为空，页面只能显示「批量生成失败」，
            # 用户无从知道是哪张参考图、该怎么处理。这里统一转成可读的 409 并带回阻断项。
            messages = [i.get("message") or i.get("code", "") for i in compiled.blocking_issues]
            raise errors.AppError(
                "prompt_compilation_blocked",
                "分镜图提示词未就绪：" + "；".join(m for m in messages if m),
                409,
                blocking_issues=compiled.blocking_issues,
                template_id=compiled.template_id,
            )
        out = apply_to_params(compiled, out)
    else:
        out["prompt"] = to_h3_clip_prompt(shot, out.get("prompt") or "")
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
    if not workflow_tier(wf.capabilities):
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
                priority: int = 0, request_key: str | None = None) -> Job:
    """创建生成任务并持久化 effective_inputs 快照。

    priority 越大越先被 worker 取到（同优先级按入队顺序），用于人工发起的
    重新生成插队；普通任务保持 0。
    request_key 非空时按提交级幂等：同键已存在未终态任务则直接复用，
    避免前端重复点击产生重复任务（D13）。
    """
    # 幂等查重（D13）：同 request_key 已有任务直接复用
    if request_key:
        existing = find_active_job_by_request_key(db, request_key)
        if existing:
            return existing
    job_type = f"generate_{target}" if target != "keyframe" else "generate_keyframe"
    if shot_id and target in ("keyframe", "clip"):
        # 出分镜前资产预检：缺失资产自动补生成并阻断本次提交，避免必然失败的任务
        _shot_for_preflight = db.get(Shot, shot_id)
        if _shot_for_preflight is not None:
            _preflight_shot_assets(db, _shot_for_preflight, target)
    if target == "clip":
        # 清除上一镜头遗留的质检快照，避免快照串镜头
        db.info.pop("prompt_qc_last", None)
        # 极速/标准/质量三档工作流各自带画幅，创建任务时就落到 params，
        # 避免 worker 再按项目宽高比覆盖
        params = _apply_tier_frame_size(wf, params)
        # 出视频前质检：视频提示词在解析阶段生成，此处按分镜图/资产做一致性校验，
        # 发现疑点交 LLM 整段重写（带缓存），结果写入 input_snapshot 可追溯
        if params.get("prompt") and shot_id:
            shot = db.get(Shot, shot_id)
            if shot is not None:
                from app.services.prompt_qc import apply_clip_qc
                params = dict(params)
                params["prompt"] = apply_clip_qc(db, shot, params["prompt"])[0]
                # 质检可整段重写，重写后再按结构化对白兜一次，确保 <d> 外不出现“台词/对白”标签
                from app.services.prompt_compiler import to_h3_clip_prompt
                params["prompt"] = to_h3_clip_prompt(shot, params["prompt"])
    # 参考图槽位职责硬校验：身份槽只允许本镜主体角色、时序槽只允许尾帧。
    # 串位会在出图/出视频阶段直接换脸或构图串镜，必须在提交前阻断。
    if target in ("keyframe", "clip") and shot_id:
        from app.services.prompt_compiler import audit_reference_slots

        _shot = db.get(Shot, shot_id)
        if _shot is not None:
            _ref_issues = audit_reference_slots(db, _shot, params.get("__media__") or {})
            if _ref_issues:
                raise errors.AppError(
                    "reference_slot_mismatch",
                    "；".join(i.get("message", "") for i in _ref_issues if i.get("message"))
                    + "。已阻止生成，请检查参考图槽位绑定",
                    409,
                    reference_issues=_ref_issues,
                )
    qc_meta = db.info.pop("prompt_qc_last", None)
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
        request_key=request_key,
        created_at=_now(),
        input_snapshot={
            "workflow_version": wf.version,
            "prompt_version": {"fingerprint": _prompt_fingerprint(params)},
            "params": params,
            "generation_mode": generation_mode,
            "prompt_qc": qc_meta,
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


def _check_keyframe_ready(db: Session, shot_ids: list[str]) -> None:
    """分镜图就绪校验：生成视频前校验每个镜头都有当前有效且文件存在的分镜图。

    人工逐镜确认（keyframe_confirmed）已取消：阶段推进不再依赖确认标记，
    只依赖「这一镜的分镜图是否真的生成出来了」。资产替换/重绘会让旧版本转
    superseded，因此这里查的是 validity=current + 来源任务 succeeded + 文件存在。
    """
    from app.services.assets_gate import media_file_exists

    if not shot_ids:
        return
    rows = (
        db.query(Asset, Job.shot_id)
        .join(Job, Job.id == Asset.source_job_id)
        .filter(
            Job.shot_id.in_(shot_ids),
            Job.status == "succeeded",
            Asset.asset_type == "keyframe",
            Asset.validity == "current",
        )
        .all()
    )
    ready: dict[str, Asset] = {}
    for asset, shot_id in rows:
        if shot_id:
            ready[shot_id] = asset
    missing: list[str] = []
    for shot_id in shot_ids:
        asset = ready.get(shot_id)
        if asset is None or not media_file_exists(asset.file_path):
            missing.append(shot_id)
    if missing:
        ids = ", ".join(missing[:10])
        raise errors.AppError(
            "keyframe_not_ready",
            f"存在缺少有效分镜图的镜头（{ids}{' 等' if len(missing) > 10 else ''}），请先生成分镜图后再生成视频",
            409,
        )


def _check_assets_ready(db: Session, project_id: str) -> None:
    """公共资产就绪闸口：生成分镜图前校验必需资产图片已真实生成完成。

    统一委托 assets_gate 门禁服务：校验必需资产指针/版本有效性/文件存在/
    在途生成任务，未就绪时返回结构化阻断明细（blockers：资产 ID/名称/类型/原因）
    与分项统计（summary：角色/场景/道具各自的 total/done）。

    人工审核状态不再参与判定：阶段审核机制已取消。
    """
    from app.services.assets_gate import assert_assets_gate

    assert_assets_gate(db, project_id)


# 资产补生成任务优先级：高于镜头任务（0），保证缺失的定妆图/基准图/锚定图先出
_ASSET_PREFLIGHT_PRIORITY = 500


def _inflight_asset_keys(db: Session, project_id: str) -> set[tuple[str, str]]:
    """项目内在途（queued/running）的资产补生成任务：{(类型, 实体 id)}，用于去重。"""
    rows = (
        db.query(Job)
        .filter(
            Job.project_id == project_id,
            Job.job_type == "generate_keyframe",
            Job.status.in_(("queued", "running")),
        )
        .all()
    )
    keys: set[tuple[str, str]] = set()
    for job in rows:
        params = job.params or {}
        for key, kind in (("__character_id__", "character"),
                          ("__prop_id__", "prop"),
                          ("__scene_id__", "scene")):
            if params.get(key):
                keys.add((kind, params[key]))
    return keys


def _enqueue_asset_job(db: Session, asset_type: str, entity, wf: Workflow) -> Job:
    """按资产类型提交补生成任务（与 /characters|props|scenes/{id}/generate 同规则）。"""
    from app.services.asset_prompts import ASSET_PROMPTS_VERSION

    params, compilation = _compile_asset_prompt(db, asset_type, entity.id, wf, {})
    params["__asset_prompt_version__"] = ASSET_PROMPTS_VERSION
    job = _create_job(db, entity.project_id, None, "keyframe", wf, params,
                      priority=_ASSET_PREFLIGHT_PRIORITY)
    _attach_compilation(job, compilation)
    id_key = {"character": "__character_id__", "prop": "__prop_id__",
              "scene": "__scene_id__"}[asset_type]
    job.params = {**(job.params or {}), id_key: entity.id}
    return job


def _preflight_shot_assets(db: Session, shot: Shot, target: str) -> None:
    """出分镜前资产预检：本镜引用的定妆图/基准图/锚定图缺失时自动补生成。

    缺失资产若不补，提交后必然在参考图门禁处失败（reference_asset_missing /
    asset_identity_missing），既浪费一次失败任务也污染任务历史。这里改为：
    自动提交缺失资产的生成任务（高优先级、按实体去重），并阻断本次分镜任务，
    资产出图后重新发起即可。
    """
    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        return
    project_id = scene.project_id
    text = shot.image_prompt if target == "keyframe" else (shot.video_prompt or shot.image_prompt)
    text = text or ""

    missing: list[tuple[str, str]] = []  # (类型, 名称)
    chars = [
        ch for ch in db.query(Character).filter(Character.project_id == project_id).all()
        if ch.name and (f"@{ch.name}" in text or ch.name in text)
    ]
    for ch in chars:
        if not ch.portrait_asset_id:
            missing.append(("character", ch.name))
    if target == "keyframe" and not scene.baseline_asset_id:
        # 视频以已生成的分镜图为参考，场景基准图缺失只影响分镜图
        missing.append(("scene", scene.title or "场景"))
    props = [
        p for p in db.query(Prop).filter(Prop.project_id == project_id).all()
        if p.name and (f"@{p.name}" in text or p.name in text)
    ]
    for p in props:
        if not p.anchor_asset_id:
            missing.append(("prop", p.name))
    if not missing:
        return

    inflight = _inflight_asset_keys(db, project_id)
    wf = None
    created: list[str] = []
    for kind, name in missing:
        entity = None
        if kind == "character":
            entity = next((c for c in chars if c.name == name), None)
        elif kind == "prop":
            entity = next((p for p in props if p.name == name), None)
        else:
            entity = scene
        if entity is None or (kind, entity.id) in inflight:
            continue
        if wf is None:
            wf = _match_pure_asset_workflow(db, None)
        job = _enqueue_asset_job(db, kind, entity, wf)
        created.append(job.id)
        inflight.add((kind, entity.id))
    # 资产任务必须先落库：随后抛出的 409 会回滚当前事务
    db.commit()

    names = "、".join(f"{name}（{_ASSET_LABEL[kind]}）" for kind, name in missing)
    raise errors.AppError(
        "asset_preflight_pending",
        f"本镜所需资产缺失，已自动提交补生成：{names}。资产完成后请重新发起生成",
        409,
        missing=[{"type": k, "name": n} for k, n in missing],
        created_job_ids=created,
    )


_ASSET_LABEL = {"character": "角色定妆图", "scene": "场景基准图", "prop": "道具锚定图"}


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


def _already_generated_message(target: str, count: int) -> str:
    """已生成跳过时的用户提示：分镜图与分镜视频的重做入口不同，文案必须分开。

    旧实现两处都写死「生成过视频…请在分镜视频详情页重新生成」，
    但 keyframe 也走这个分支后，批量出图时会提示用户去「分镜视频详情页」重做，
    指向错误的页面。
    """
    if target == "clip":
        return (f"选中镜头均已由该工作流生成过分镜视频（{count} 个），"
                f"如需重做请在分镜视频详情页重新生成")
    return (f"选中镜头均已由该工作流生成过分镜图（{count} 个），"
            f"如需重画请在镜头卡片上点「重新生成」")


def _filter_already_generated(db: Session, shots: list, target: str,
                             wf_id: str | None, wf_version: int | None) -> tuple[list, list]:
    """批量生成幂等过滤：跳过已由同一工作流生成过当前有效产物的镜头。

    判定口径：镜头存在 validity=current 的对应资产（keyframe/clip），其来源任务
    succeeded，且该任务的 workflow_id 与本次批量使用的工作流一致。
    目的：重复点击批量出图/出视频不会对已产出的镜头再次创建任务、重复消耗 GPU。
    重新生成走详情页的 regenerate 接口，不受此处限制。

    ## 为什么 keyframe 也要过滤（2026-11-21 修复）

    旧实现开头是 `if target != "clip": return shots, []` —— 只对分镜视频幂等，
    分镜图完全不判重。实测用户操作：分镜图页 26 镜已全部生成 → 点「全选」
    → 点「批量生成」，26 张图被**全部重新渲染**，既浪费 GPU 又覆盖掉已确认的图。
    资产库的角色定妆图 / 场景基准图 / 道具锚定图同理（见 _filter_entity_generated）。

    注意：`target` 与 Asset.asset_type 的对应关系是 1:1（keyframe/clip），
    分镜视频仍额外要求工作流一致——换工作流重做是合法的主动行为。
    """
    if target not in ("keyframe", "clip") or not wf_id or not shots:
        return shots, []
    shot_ids = [s.id for s in shots]
    job_type = "generate_clip" if target == "clip" else "generate_keyframe"
    rows = (
        db.query(Job.shot_id)
        .join(Asset, Asset.source_job_id == Job.id)
        .filter(
            Job.shot_id.in_(shot_ids),
            Job.job_type == job_type,
            Job.status == "succeeded",
            Job.workflow_id == wf_id,
            Asset.asset_type == target,
            Asset.validity == "current",
        )
        .distinct()
        .all()
    )
    done = {r[0] for r in rows if r[0]}
    # 在途任务同样要跳过（2026-11-21 修复）：旧实现只看「已成功的产物」，
    # 任务还在 queued/running 时判重查不到任何产物 → 重复点击「批量生成」
    # 会为同一镜头反复建任务。实测：第 2 集 6 个镜头在途，再点一次批量生成
    # 又建了 6 个重复任务（17 个任务 / 11 个镜头），白白排队消耗算力。
    inflight_rows = (
        db.query(Job.shot_id)
        .filter(
            Job.shot_id.in_(shot_ids),
            Job.job_type == job_type,
            Job.status.in_(("queued", "running", "chained_wait", "submission_unknown")),
        )
        .distinct()
        .all()
    )
    inflight = {r[0] for r in inflight_rows if r[0]}
    if not done and not inflight:
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
        elif s.id in inflight:
            skipped.append({
                "shot_id": s.id,
                "reason": "already_inflight",
                "workflow_id": wf_id,
                "workflow_version": wf_version,
            })
        else:
            ok.append(s)
    return ok, skipped


def _filter_entity_generated(db: Session, entities: list, plan_type: str,
                             wf_id: str | None, wf_version: int | None) -> tuple[list, list]:
    """实体资产批量生成幂等过滤：角色定妆图 / 场景基准图 / 道具锚定图。

    实体资产没有 shot_id，归属通过 job.params 里的 owner key 关联，
    产物指针写在实体表上（portrait_asset_id / baseline_asset_id / anchor_asset_id）。

    判定口径：实体已有指向 current 有效资产的指针，且该资产来源任务 succeeded、
    工作流与本次一致 → 跳过。重新生成走卡片上的「重生成」，不受此限制。

    实测问题：资产库点「全选」→「批量生成角色定妆图」，已生成好的定妆图被
    全部重做，覆盖掉人工挑过的结果。
    """
    if not entities or not wf_id:
        return entities, []
    pointer_attr = {
        "character": "portrait_asset_id",
        "scene": "baseline_asset_id",
        "prop": "anchor_asset_id",
    }.get(plan_type)
    if pointer_attr is None:
        return entities, []

    done: set[str] = set()
    for e in entities:
        asset_id = getattr(e, pointer_attr, None)
        if not asset_id:
            continue
        asset = db.get(Asset, asset_id)
        if asset is None or asset.validity != "current":
            continue
        src = db.get(Job, asset.source_job_id) if asset.source_job_id else None
        if src is not None and src.status == "succeeded" and src.workflow_id == wf_id:
            done.add(e.id)

    # 在途任务同样跳过：实体资产无 shot_id，归属靠 job.params 里的 owner key。
    # 与 _filter_already_generated 同理，避免重复点击批量生成反复建任务。
    _owner_key = {"character": "__character_id__", "scene": "__scene_id__",
                  "prop": "__prop_id__"}[plan_type]
    entity_ids = [e.id for e in entities]
    inflight: set[str] = set()
    for job in (
        db.query(Job)
        .filter(
            Job.project_id == (entities[0].project_id if entities else None),
            Job.job_type == f"generate_{plan_type}",
            Job.status.in_(("queued", "running", "chained_wait", "submission_unknown")),
        )
        .all()
    ):
        owner = (job.params or {}).get(_owner_key)
        if owner and owner in entity_ids:
            inflight.add(owner)

    if not done and not inflight:
        return entities, []
    ok, skipped = [], []
    for e in entities:
        if e.id in done:
            skipped.append({
                "entity_id": e.id,
                "reason": "already_generated",
                "workflow_id": wf_id,
                "workflow_version": wf_version,
            })
        elif e.id in inflight:
            skipped.append({
                "entity_id": e.id,
                "reason": "already_inflight",
                "workflow_id": wf_id,
                "workflow_version": wf_version,
            })
        else:
            ok.append(e)
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
        # 资产就绪闸口：必需资产图片未全部生成完成时禁止生成分镜图
        _check_assets_ready(db, scene.project_id)
    if target == "clip":
        _check_keyframe_ready(db, [shot.id])
    # 在途任务去重：同镜头同目标已有 queued/running 任务时拒绝重复提交
    _check_inflight_duplicate(db, shot.id, target)
    wf = _match_workflow(db, target, payload.get("workflow_id"), scene.project_id)
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
        params["prompt"] = inject_asset_refs(db, shot, params["prompt"], with_qc=True)
    # @引用注入（D-16）：keyframe/clip 生成时解析提示词中的 @角色/@场景，自动携带参考图
    # 连续性首帧注入可能已设置 __media__.first_frame，此处合并而非覆盖
    if target in ("keyframe", "clip"):
        from app.services.ref_inject import build_media_params
        prompt_text = params.get("prompt") or ""
        if prompt_text:
            slots = declared_reference_slots(wf.capabilities)
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
    # clip 图序最终重建（2026-11-21）：到这一步 __media__ 才是最终形态
    # （build_media_params 的 @引用 + 上面注入的 ref_prev_frame 都在了）。
    # 此前用的是 rewrite_picture_refs，它按工作流静态编号换算、且不会补角色定妆图
    # 的职责描述，结果就是「提示词写 Picture 1=上一镜尾帧 / Picture 2=分镜图，
    # 而两个角色定妆图占了第 3/4 张却一个字都没提」。
    # 这里改用与详情页、执行器同源的 reference_layout 口径重算一遍。
    if target == "clip" and params.get("prompt"):
        from app.services import reference_layout as _rl
        from app.services.ref_inject import workflow_ref_syntax as _wfs

        _slots = declared_reference_slots(wf.capabilities)
        _media = params.get("__media__") or {}
        _nums = _rl.compressed_numbers(list(_media.keys()), _slots)
        if _nums:
            from app.services.submission_persist import _slot_names

            _names = _slot_names(db, scene.project_id, _media)
            # rebuild_prompt 以单个换行结尾；任务参数历史上不带尾换行。
            # 这里统一剥掉，保证 shot.video_prompt 与 job.params["prompt"]
            # 在**字节层面**完全一致（否则详情页与下发文本差一个 \n，
            # 提示词指纹也会不同，排查时容易误判成「不是同一份」）。
            params["prompt"] = _rl.rebuild_prompt(
                params["prompt"], _nums, _names, _slots, _wfs(_slots)
            ).rstrip("\n")
    job = _create_job(db, scene.project_id, shot.id, target, wf, params,
                      generation_mode=payload.get("generation_mode"),
                      request_key=payload.get("idempotency_key"))
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
        # 详情页数据源回写（2026-11-21）：首次生成（待生成 → 生成中）此前**完全
        # 没有**回写 shot.video_prompt 与 StoryboardRegenConfig，导致详情页显示的是
        # 解析阶段的原始文本与实时推导的槽位，与本次实际下发的提示词/参考图不一致。
        # 用本次冻结快照的同一份数据回写，页面看到的 == 实际生成的。
        from app.services.submission_persist import persist_submission

        _persist = persist_submission(
            db,
            shot=shot,
            project_id=scene.project_id,
            target=target,
            prompt=params.get("prompt") or "",
            media=params.get("__media__") or {},
            declared_slots=declared_reference_slots(wf.capabilities),
            workflow_id=wf.id,
            workflow_version=wf.version,
            negative_prompt=params.get("negative"),
            parameters={
                k: v for k, v in params.items()
                if not str(k).startswith("__") and k not in ("prompt", "negative")
            },
        )
        _snap = dict(job.input_snapshot or {})
        _snap["submission_persist"] = _persist
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
        # 资产就绪闸口：重做分镜图属于出图通道，同样要求必需资产已生成完成
        _check_assets_ready(db, scene.project_id)
    if target == "clip":
        _check_keyframe_ready(db, [shot.id])
    # 在途任务去重：同镜头同目标已有 queued/running 任务时拒绝重复提交
    _check_inflight_duplicate(db, shot.id, target)
    # 旧资产转历史版本 + 下游失效传播
    # - keyframe 重做：分镜图会被重绘，本镜确认状态回退（D-13）
    # - clip 重做：分镜图不变，不回退确认状态；本镜尾帧不置失效，
    #   重新生成成功后原地覆盖同一张，失败时下游仍可沿用旧尾帧
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
    wf = _match_workflow(db, target, payload.get("workflow_id"), scene.project_id)
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
        params["prompt"] = inject_asset_refs(db, shot, params["prompt"], with_qc=True)
    # @引用注入（D-16）：重做与单镜生成行为一致，重做时同样携带参考图
    # 连续性首帧注入可能已设置 __media__.first_frame，此处合并而非覆盖
    if target in ("keyframe", "clip"):
        from app.services.ref_inject import build_media_params
        prompt_text = params.get("prompt") or ""
        if prompt_text:
            slots = declared_reference_slots(wf.capabilities)
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
    job = _create_job(db, scene.project_id, shot.id, target, wf, params,
                      request_key=payload.get("idempotency_key"))
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
    # 分镜图就绪校验：没有有效分镜图不允许生成视频
    _check_keyframe_ready(db, [shot.id])
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
    wf = _match_workflow(db, target, payload.get("workflow_id"), scene.project_id)
    params = dict(payload.get("params") or {})
    wf = _inject_continuity_first_frame(db, shot, target, wf, params)
    params.setdefault("prompt", shot.video_prompt or shot.image_prompt or "")
    # @引用注入（D-16）：解析提示词中的 @角色/@场景，自动携带参考图
    from app.services.ref_inject import build_media_params
    prompt_text = params.get("prompt") or ""
    if prompt_text:
        slots = declared_reference_slots(wf.capabilities)
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
                      priority=_REGENERATE_CLIP_PRIORITY,
                      request_key=payload.get("idempotency_key"))
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
    job = _create_job(db, ch.project_id, None, "keyframe", wf, params,
                      request_key=(payload or {}).get("idempotency_key"))
    _attach_compilation(job, compilation)
    job.params = {**job.params, "__character_id__": ch.id}
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
    job = _create_job(db, p.project_id, None, "keyframe", wf, params,
                      request_key=(payload or {}).get("idempotency_key"))
    _attach_compilation(job, compilation)
    job.params = {**job.params, "__prop_id__": p.id}
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
        if declared_reference_slots(wf.capabilities):
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
    job = _create_job(db, sc.project_id, None, "keyframe", wf, params,
                      request_key=(payload or {}).get("idempotency_key"))
    _attach_compilation(job, compilation)
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
    if target == "keyframe":
        # 资产就绪闸口：必需资产图片未全部生成完成时禁止批量生成分镜图
        _check_assets_ready(db, sc.project_id)
    if target == "clip":
        _check_keyframe_ready(db, [s.id for s in shots])
        # 尾帧依赖预检（P0）：避免循环中途失败导致部分任务已入队
        _assert_prev_frames_available(db, shots, target)
    wf = _match_workflow(db, target, payload.get("workflow_id"), sc.project_id)
    # 幂等过滤（P0）：同一工作流已生成过视频的镜头直接跳过，避免重复渲染；
    # 需要重做时走分镜视频详情页的重新生成，不受此处限制
    shots, already_done = _filter_already_generated(db, shots, target, wf.id, wf.version)
    if already_done:
        skipped.extend(already_done)
    if not shots:
        raise errors.AppError(
            "already_generated",
            _already_generated_message(target, len(already_done)),
            409,
        )
    # @引用注入（D-16，P0 修复）：与单镜生成/项目批量生成行为一致，
    # 解析每个镜头提示词中的 @角色/@场景/@道具，自动携带参考图
    from app.services.ref_inject import build_media_params
    slots = declared_reference_slots(wf.capabilities)
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
            params["prompt"] = inject_asset_refs(db, shot, params["prompt"], with_qc=True)
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
        _job = _create_job(db, sc.project_id, shot.id, target, shot_wf, params,
                           request_key=(
                               f"{payload.get('idempotency_key')}/{shot.id}"
                               if payload.get('idempotency_key') else None))
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
        # 资产就绪闸口：必需资产图片未全部生成完成时禁止批量生成分镜图
        # （含角色定妆图 / 场景基准图 / anchor_required 道具锚定图的真实状态校验）
        _check_assets_ready(db, project_id)
    # 批量生成方式（仅 clip）：independent 独立并行（不依赖上一镜尾帧）/ chained 链式串行（逐镜衔接）
    chain_mode = payload.get("chain_mode") if target == "clip" else None
    if chain_mode is not None and chain_mode not in ("independent", "chained"):
        raise errors.AppError("invalid_request", "chain_mode 仅允许 independent/chained", 400)
    if target == "clip":
        _check_keyframe_ready(db, [s.id for s in shots])
        # 尾帧依赖预检（P0）：仅传统模式（未指定 chain_mode）执行；
        # independent 不依赖尾帧；chained 由链式任务在执行时解析上一镜尾帧
        if chain_mode is None:
            _assert_prev_frames_available(db, shots, target)
    wf = _match_workflow(db, target, payload.get("workflow_id"), project_id)
    # 幂等过滤（P0）：同一工作流已生成过视频的镜头直接跳过，避免重复渲染；
    # 需要重做时走分镜视频详情页的重新生成，不受此处限制
    shots, already_done = _filter_already_generated(db, shots, target, wf.id, wf.version)
    if already_done:
        skipped.extend(already_done)
    if not shots:
        raise errors.AppError(
            "already_generated",
            _already_generated_message(target, len(already_done)),
            409,
        )
    jobs = []
    slots = declared_reference_slots(wf.capabilities)
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
            params["prompt"] = inject_asset_refs(db, shot, params["prompt"], with_qc=True)
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
        _job = _create_job(db, project_id, shot.id, target, shot_wf, params,
                           request_key=(
                               f"{payload.get('idempotency_key')}/{shot.id}"
                               if payload.get('idempotency_key') else None))
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
