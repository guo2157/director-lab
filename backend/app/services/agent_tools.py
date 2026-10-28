"""Agent 受控工具注册表（Schema 化版本，单 Agent 核心流程框架 P1-1）。

设计约束（AGENTS.md P0 / 任务书 §8.4）：
- 工具只能由确定性业务代码执行，模型只负责选择与解释
- 首批仅注册只读查询工具，无任何写操作/生成触发（失效传播只标记不触发）
- 每个工具：名称/用途/权限等级/风险等级/副作用/幂等性/审批要求/超时/重试/
  Pydantic 入参 Schema/结构化返回/错误分类
- 工具返回真实数据库查询结果，禁止模拟数据
- 可信参数（project_id 等会话上下文）由服务端注入，不由模型决定

权限分级：
- permission：read（只读）/ write（写操作）/ generate（触发生成任务）
- risk_level：low / medium / high（高风险工具须人工审批）
- side_effect：工具是否改变系统状态
- idempotent：重复执行是否安全
- requires_approval：不可逆/高影响操作须人工审批后才执行
- execute_tool 对 requires_approval=True 且未获批（approved=False）的工具返回
  approval_required，不产生任何副作用；审批事件由会话层以
  AgentMessage(role="approval") 记录，获批后以 approved=True 重新调用
"""
import time
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Asset, Character, Job, Project, Prop, Scene, Shot, Workflow, ids
from app.models.asset_plan import AssetPlan, AssetPlanDependency
from app.services.progress import derive_progress


# 工具权限等级白名单（注册表校验用）
TOOL_PERMISSION_LEVELS = {"read", "write", "generate"}
# 风险等级白名单
TOOL_RISK_LEVELS = {"low", "medium", "high"}


# --- 入参 Schema（Pydantic，模型可见 JSON Schema，服务端执行校验） ---


class ProjectOverviewInput(BaseModel):
    """project_overview 入参。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")


class ListJobsInput(BaseModel):
    """list_jobs 入参。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")
    status: str | None = Field(
        default=None,
        description="可选：任务状态过滤（queued/running/succeeded/failed/cancelled）",
    )
    limit: int = Field(default=20, ge=1, le=50, description="可选：返回条数上限，默认 20，最大 50")


class AssetStatsInput(BaseModel):
    """asset_stats 入参。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")


class ListAssetsInput(BaseModel):
    """list_assets 入参。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")
    asset_type: str | None = Field(
        default=None,
        description="可选：资产类型过滤（keyframe/clip/character/scene/prop/reference 等）",
    )
    validity: str | None = Field(
        default=None,
        description="可选：有效性过滤（current/stale/superseded）",
    )
    limit: int = Field(default=8, ge=1, le=30, description="可选：返回条数上限，默认 8，最大 30")
    offset: int = Field(default=0, ge=0, description="可选：偏移量（翻页用），默认 0")


class ListAssetPlansInput(BaseModel):
    """list_asset_plans 入参。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")
    plan_type: str | None = Field(
        default=None,
        description="可选：定义类型过滤（character/scene/prop/keyframe/clip）",
    )
    status: str | None = Field(
        default=None,
        description="可选：状态过滤（pending/generated/reviewed/rejected）",
    )
    limit: int = Field(default=20, ge=1, le=50, description="可选：返回条数上限，默认 20，最大 50")
    offset: int = Field(default=0, ge=0, description="可选：偏移量（翻页用），默认 0")


@dataclass
class ToolResult:
    """工具执行结果：ok=False 时携带错误码与用户可读信息。"""

    ok: bool
    data: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int = 0


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- 工具实现（全部只读） ---


def tool_project_overview(db: Session, project_id: str) -> dict:
    """项目概览：基础信息 + 派生进度（镜头总数/完成数/当前阶段）。"""
    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    total, done, stage = derive_progress(db, p)
    return {
        "id": p.id,
        "name": p.name,
        "status": p.status,
        "aspect_ratio": p.aspect_ratio,
        "current_stage": stage,
        "shot_total": total,
        "shot_done": done,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def tool_list_jobs(db: Session, project_id: str, status: str | None = None, limit: int = 20) -> dict:
    """项目任务列表（最近优先）：id/类型/目标/状态/错误码。"""
    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    q = select(Job).where(Job.project_id == project_id)
    if status:
        q = q.where(Job.status == status)
    jobs = (
        db.scalars(q.order_by(Job.created_at.desc()).limit(min(max(1, limit), 50))).unique().all()
    )
    return {
        "items": [
            {
                "id": j.id,
                "job_type": j.job_type,
                "shot_id": j.shot_id,
                "status": j.status,
                "error_code": j.error_code,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
            for j in jobs
        ],
        "count": len(jobs),
    }


def tool_asset_stats(db: Session, project_id: str) -> dict:
    """项目资产统计：按 asset_type 分组计数与最新资产时间。"""
    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    rows = db.execute(
        select(Asset.asset_type, func.count(Asset.id))
        .where(Asset.project_id == project_id)
        .group_by(Asset.asset_type)
    ).all()
    return {
        "project_id": project_id,
        "by_type": {row[0]: row[1] for row in rows},
        "total": sum(row[1] for row in rows),
    }


def tool_list_assets(
    db: Session,
    project_id: str,
    asset_type: str | None = None,
    validity: str | None = None,
    limit: int = 8,
    offset: int = 0,
) -> dict:
    """项目资产清单（最近生成优先）：类型/名称/版本/有效性/质量状态/可渲染标识。

    排序：按产出该资产的 Job.created_at 倒序（Asset 自身无时间戳字段）；
    无关联任务（source_job_id 为空或任务已缺失）的资产排在最后。
    只读查询，返回 asset_id 供前端拼媒体 URL（/api/assets/{id}/file?thumb=true），
    不返回文件系统路径，避免泄露服务器目录结构。
    """
    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    q = (
        select(Asset, Job.created_at)
        .outerjoin(Job, Asset.source_job_id == Job.id)
        .where(Asset.project_id == project_id)
    )
    if asset_type:
        q = q.where(Asset.asset_type == asset_type)
    if validity:
        q = q.where(Asset.validity == validity)
    # 无关联任务的资产（Job.created_at 为 NULL）排在最后：布尔升序 False(0) 先于 True(1)
    rows = db.execute(
        q.order_by(Job.created_at.is_(None).asc(), Job.created_at.desc(), Asset.id.asc())
        .offset(max(0, offset))
        .limit(min(max(1, limit), 30))
    ).all()
    items = []
    for a, job_created_at in rows:
        items.append(
            {
                "asset_id": a.id,
                "asset_type": a.asset_type,
                "name": a.name,
                "version": a.version,
                "validity": a.validity,
                "quality_status": a.quality_status,
                "has_file": bool(a.file_path),
                "has_thumb": bool(a.thumb_path),
                "source_job_id": a.source_job_id,
                "locked": a.locked,
                "ref_count": a.ref_count,
                "generated_at": job_created_at.isoformat() if job_created_at else None,
            }
        )
    return {
        "items": items,
        "count": len(items),
        "offset": max(0, offset),
    }


def tool_list_asset_plans(
    db: Session,
    project_id: str,
    plan_type: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """项目资产定义清单：类型/名称/状态/人工确认标记/依赖摘要。

    供对话内回答『哪些定义还没生成/待确认』；
    不返回文件系统路径（media_path/thumb_path），避免泄露服务器目录结构。
    """
    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    q = select(AssetPlan).where(AssetPlan.project_id == project_id)
    if plan_type:
        q = q.where(AssetPlan.plan_type == plan_type)
    if status:
        q = q.where(AssetPlan.status == status)
    plans = (
        db.scalars(
            q.order_by(AssetPlan.episode, AssetPlan.seq, AssetPlan.id)
            .offset(max(0, offset))
            .limit(min(max(1, limit), 50))
        )
        .unique()
        .all()
    )
    items = []
    for plan in plans:
        deps = (
            db.scalars(
                select(AssetPlanDependency).where(AssetPlanDependency.plan_id == plan.id)
            )
            .unique()
            .all()
        )
        items.append(
            {
                "plan_id": plan.id,
                "plan_type": plan.plan_type,
                "asset_key": plan.asset_key,
                "status": plan.status,
                "confirmed": bool(plan.confirmed),
                "owner_type": plan.owner_type,
                "owner_id": plan.owner_id,
                "episode": plan.episode,
                "seq": plan.seq,
                "asset_id": plan.asset_id,
                "version": plan.version,
                "dependencies": [{"slot": d.slot, "ref_key": d.ref_key} for d in deps],
            }
        )
    return {
        "items": items,
        "count": len(items),
        "offset": max(0, offset),
    }


# --- 生成能力（写操作：申请与执行分离，执行须先获批） ---

# target → workflow_type 映射（与生成接口保持一致）
_GEN_TARGET_WF_TYPE = {
    "keyframe": "image",
    "clip": "video",
    "audio": "tts",
    "bgm": "bgm",
    "sfx": "sfx",
}

# 实体目标（公共资产）：生成走 keyframe 图像工作流，任务参数用实体归属键
_ENTITY_TARGETS = {"character", "scene", "prop"}
_ENTITY_OWNER_KEY = {
    "character": "__character_id__",
    "scene": "__scene_id__",
    "prop": "__prop_id__",
}
_ENTITY_MODEL = {"character": Character, "scene": Scene, "prop": Prop}


def _assert_entity_in_project(
    db: Session, entity_id: str, target: str, project_id: str
) -> tuple[object, str]:
    """校验实体存在且属于该项目，防止跨项目触发生成。返回 (实体, 归属参数键)。"""
    model = _ENTITY_MODEL.get(target)
    if model is None:
        raise ToolError("tool_invalid_args", f"不支持的生成目标: {target}")
    entity = db.get(model, entity_id)
    if entity is None:
        raise ToolError("entity_not_found", "实体不存在")
    if entity.project_id != project_id:
        raise ToolError("entity_not_in_project", "该实体不属于指定项目")
    return entity, _ENTITY_OWNER_KEY[target]


class RequestGenerateInput(BaseModel):
    """request_generate 入参。"""

    session_id: str = Field(
        min_length=1, description="会话 ID（审批归属会话，由服务端注入，模型不应自行填写）"
    )
    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")
    shot_id: str | None = Field(
        default=None, description="镜头 ID（sht_ 前缀）；镜头目标必填"
    )
    entity_id: str | None = Field(
        default=None,
        description="实体 ID（chr_/scn_/prp_ 前缀）；角色/场景/道具目标必填",
    )
    target: str = Field(
        default="keyframe",
        description="生成目标：keyframe/clip/audio/bgm/sfx/character/scene/prop",
    )
    prompt: str | None = Field(
        default=None, max_length=4000, description="可选：追加提示词（留空则用分镜原有提示词）"
    )


class RunGenerateInput(BaseModel):
    """run_generate 入参。"""

    confirmation_id: str = Field(min_length=1, description="审批记录 ID（agc_ 前缀）")


def _match_generate_workflow(db: Session, target: str) -> Workflow:
    """按 target 匹配工作流：默认 → 内置 → 任意，无匹配时报错。

    与生成接口的匹配顺序一致，用于在申请阶段就暴露「无可用工作流」，
    避免审批通过后才发现无法执行。
    """
    wf_type = _GEN_TARGET_WF_TYPE.get(target)
    if wf_type is None:
        raise ToolError("tool_invalid_args", f"不支持的生成目标: {target}")
    for cond in (
        Workflow.workflow_type == wf_type,
        Workflow.is_default == True,  # noqa: E712
    ), (
        Workflow.workflow_type == wf_type,
        Workflow.is_builtin == True,  # noqa: E712
    ), (Workflow.workflow_type == wf_type,):
        wf = db.scalars(select(Workflow).where(*cond).limit(1)).first()
        if wf is not None:
            return wf
    raise ToolError("no_matching_workflow", f"无 {wf_type} 类型可用工作流")


def _assert_shot_in_project(db: Session, shot_id: str, project_id: str) -> Shot:
    """校验镜头存在且属于该项目，防止跨项目触发生成。"""
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise ToolError("shot_not_found", "镜头不存在")
    scene = db.get(Scene, shot.scene_id)
    if scene is None or scene.project_id != project_id:
        raise ToolError("shot_not_in_project", "该镜头不属于指定项目")
    return shot


def tool_request_generate(
    db: Session,
    session_id: str,
    project_id: str,
    shot_id: str | None = None,
    target: str = "keyframe",
    prompt: str | None = None,
    entity_id: str | None = None,
) -> dict:
    """提交生成审批申请（不建任务、不触发生成、不消耗算力）。

    镜头目标（keyframe/clip/audio/bgm/sfx）须给出 shot_id；
    实体目标（character/scene/prop 公共资产）须给出 entity_id。
    同会话同入参重复申请复用同一条待审记录（幂等）。
    审批通过后由 run_generate 执行。
    """
    from app.services import approvals

    if target in _ENTITY_TARGETS:
        entity, owner_key = _assert_entity_in_project(db, entity_id or "", target, project_id)
        wf = _match_generate_workflow(db, "keyframe")
        # 在途去重：同实体已有 queued/running 的公共资产生成任务时拒绝重复申请
        candidates = (
            db.scalars(
                select(Job)
                .where(
                    Job.project_id == project_id,
                    Job.job_type == "generate_keyframe",
                    Job.shot_id.is_(None),
                    Job.status.in_(("queued", "running")),
                )
                .order_by(Job.created_at.desc())
                .limit(50)
            )
            .unique()
            .all()
        )
        if any((j.params or {}).get(owner_key) == entity.id for j in candidates):
            raise ToolError("job_in_flight", "该实体已有在途生成任务，请等待完成后再申请")
        args = {
            "project_id": project_id,
            "entity_id": entity.id,
            "target": target,
            "prompt": prompt,
            "workflow_id": wf.id,
        }
        confirmation = approvals.create_confirmation(
            db,
            session_id=session_id,
            tool_name="request_generate",
            args=args,
            goal=f"生成 {target} 公共资产（工作流 {wf.name}）",
        )
        return {
            "confirmation_id": confirmation.id,
            "status": confirmation.status,
            "entity_id": entity.id,
            "target": target,
            "workflow_id": wf.id,
            "workflow_name": wf.name,
            "next_step": "需人工审批通过后才会创建生成任务",
        }

    if not shot_id:
        raise ToolError("tool_invalid_args", "镜头目标必须提供 shot_id")
    _assert_shot_in_project(db, shot_id, project_id)
    wf = _match_generate_workflow(db, target)
    job_type = f"generate_{target}"
    inflight = db.scalars(
        select(Job)
        .where(
            Job.shot_id == shot_id,
            Job.job_type == job_type,
            Job.status.in_(("queued", "running")),
        )
        .limit(1)
    ).first()
    if inflight is not None:
        raise ToolError(
            "job_in_flight", f"该镜头已有在途任务（{inflight.id}，状态 {inflight.status}）"
        )

    args = {
        "project_id": project_id,
        "shot_id": shot_id,
        "target": target,
        "prompt": prompt,
        "workflow_id": wf.id,
    }
    confirmation = approvals.create_confirmation(
        db,
        session_id=session_id,
        tool_name="request_generate",
        args=args,
        goal=f"为镜头生成 {target}（工作流 {wf.name}）",
    )
    return {
        "confirmation_id": confirmation.id,
        "status": confirmation.status,
        "shot_id": shot_id,
        "target": target,
        "workflow_id": wf.id,
        "workflow_name": wf.name,
        "next_step": "需人工审批通过后才会创建生成任务",
    }


def tool_run_generate(db: Session, confirmation_id: str) -> dict:
    """执行已批准的生成请求：建任务入队，幂等键防重复建任务。

    双重门禁：既要求审批状态为 approved，也要求该审批确实存在，
    避免仅凭调用方传入的放行标志绕过审批。
    """
    from app.models import AgentConfirmation

    confirmation = db.get(AgentConfirmation, confirmation_id)
    if confirmation is None:
        raise ToolError("confirmation_not_found", "审批记录不存在")
    if confirmation.status != "approved":
        raise ToolError(
            "approval_required", f"审批未通过（当前 {confirmation.status}），不能执行生成"
        )
    args = dict(confirmation.args or {})
    project_id = args.get("project_id")
    shot_id = args.get("shot_id")
    entity_id = args.get("entity_id")
    target = args.get("target", "keyframe")
    if not project_id or not (shot_id or entity_id):
        raise ToolError("tool_invalid_args", "审批记录缺少必要参数")

    owner_key = None
    entity = None
    if shot_id:
        shot = _assert_shot_in_project(db, shot_id, project_id)
        # 静图先行闸口（D-13）：生成 clip 前校验分镜图已人工确认，
        # 与 API 通道（generate.py _check_keyframe_gate）同等校验，不可绕过
        if target == "clip" and not shot.keyframe_confirmed:
            raise ToolError(
                "keyframe_not_confirmed",
                f"镜头 {shot_id} 的分镜图尚未人工确认，请先在分镜审核页确认后再生成视频",
            )
        # 统一资产就绪门禁（D-23 强化）：Agent 通道与 API 通道同等校验，
        # keyframe 生成前必须满足必需资产齐全/有效/审核通过，不可绕过
        if target == "keyframe":
            from app.services.assets_gate import evaluate_assets_gate

            gate = evaluate_assets_gate(db, project_id)
            if not gate.ready:
                raise ToolError(
                    gate.reason_code,
                    f"{gate.message}（阻断明细：{[b.to_dict() for b in gate.blockers] if gate.blockers else '无'}）",
                )
    else:
        entity, owner_key = _assert_entity_in_project(db, entity_id or "", target, project_id)
    # 实体目标（公共资产）走 keyframe 图像工作流
    wf_target = "keyframe" if target in _ENTITY_TARGETS else target
    wf = _match_generate_workflow(db, wf_target)
    job_type = f"generate_{wf_target}"

    # 幂等：同一审批只产生一个任务；已有同 request_key 的任务直接复用
    request_key = f"agc:{confirmation.id}"
    existing = db.scalars(select(Job).where(Job.request_key == request_key).limit(1)).first()
    if existing is not None:
        return {
            "job_id": existing.id,
            "status": existing.status,
            "request_key": request_key,
            "reused": True,
        }

    params: dict = {}
    prev_frame_info: dict | None = None
    if owner_key is not None:
        # 实体目标（公共资产）：与 API 通道（generate.py 实体端点）行为一致
        params["prompt"] = args.get("prompt") or ""
        params[owner_key] = entity.id
        # 空 prompt 兜底：按实体属性用纯净化模板构造提示词，
        # 避免空提示词提交 ComfyUI 生成无意义图片浪费算力
        from app.services.asset_prompts import (
            ASSET_PROMPTS_VERSION,
            build_character_prompt,
            build_prop_prompt,
            build_scene_prompt,
            negative_for_asset_type,
        )

        if not params["prompt"]:
            if target == "character":
                params["prompt"] = build_character_prompt(
                    entity.name,
                    entity.appearance or "",
                    description=entity.description or "",
                )
            elif target == "scene":
                params["prompt"] = build_scene_prompt(
                    entity.title or "",
                    entity.description or "",
                )
            elif target == "prop":
                params["prompt"] = build_prop_prompt(
                    entity.name,
                    entity.appearance or "",
                    description=entity.description or "",
                )
        params["__asset_prompt_version__"] = ASSET_PROMPTS_VERSION
        # 负面约束：仅当工作流实际绑定 negative 参数标记时注入（与 API 通道一致），
        # 避免写入无人消费的参数
        from app.api.v1.generate import _workflow_binds_param

        if "negative" not in params and _workflow_binds_param(wf, "negative"):
            params["negative"] = negative_for_asset_type(target)
    else:
        # 镜头目标：与 API 通道（generate.py 单镜生成端点）行为对齐
        from fastapi import HTTPException

        from app.api.v1.generate import (
            _inject_continuity_first_frame,
            _inject_prev_shot_frame,
        )

        try:
            if target == "clip":
                # 跨集连续性首帧注入：集首镜 clip 切换 flf2v 工作流并注入上一集末帧
                wf = _inject_continuity_first_frame(db, shot, target, wf, params)
            # 按 target 组装默认 prompt 基础（用户未提供时兜底镜头原有提示词，
            # 避免空提示词提交 ComfyUI 浪费算力）
            if target == "keyframe":
                params.setdefault("prompt", args.get("prompt") or shot.image_prompt or "")
            elif target == "clip":
                params.setdefault(
                    "prompt", args.get("prompt") or shot.video_prompt or shot.image_prompt or ""
                )
            elif target == "audio":
                # audio 走 tts_text（台词），不是图像提示词
                params["tts_text"] = args.get("prompt") or shot.dialogue or ""
            elif args.get("prompt"):
                params["prompt"] = args["prompt"]
            # 镜头资产清单注入：出场角色/所属场景未在提示词 @引用 时自动补入头部
            if target == "keyframe" and params.get("prompt"):
                from app.services.shot_refs import inject_asset_refs

                params["prompt"] = inject_asset_refs(db, shot, params["prompt"])
            # @引用注入（D-16）：解析提示词中的 @角色/@场景，自动携带参考图
            if target in ("keyframe", "clip"):
                from app.services.ref_inject import build_media_params

                prompt_text = params.get("prompt") or ""
                if prompt_text:
                    scene = db.get(Scene, shot.scene_id)
                    slots = (wf.capabilities or {}).get("reference_slots") if wf.capabilities else None
                    media = build_media_params(db, scene.project_id, prompt_text, slots)
                    if media:
                        _existing = params.get("__media__", {})
                        _existing.update(media)
                        params["__media__"] = _existing
            # 镜间尾帧依赖（P0）：非首镜 clip 注入前一镜实际尾帧，缺失时按声明拦截
            prev_frame_info = _inject_prev_shot_frame(db, shot, target, params)
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, dict) else {}
            raise ToolError(
                detail.get("code") or "generate_inject_failed",
                detail.get("message") or str(e.detail),
            )
    cost_basis = {
        "target": target,
        "workflow_id": wf.id,
        "workflow_version": wf.version,
        "shot_count": 1,
        # 项目未配置任何单价/计费字段，此处只记录可核算的用量维度，不臆造金额
        "unit_price": None,
        "note": "未配置单价，仅记录用量维度供事后核对",
    }
    job = Job(
        id=ids.job_id(),
        project_id=project_id,
        shot_id=shot_id,
        job_type=job_type,
        workflow_id=wf.id,
        workflow_version=wf.version,
        params=params,
        status="queued",
        request_key=request_key,
        input_snapshot={
            "workflow_version": wf.version,
            "params": params,
            "confirmation_id": confirmation.id,
            "cost_basis": cost_basis,
            "idempotency_key": request_key,
            **({"prev_frame": prev_frame_info} if prev_frame_info is not None else {}),
        },
        created_at=datetime.now(),
    )
    db.add(job)
    if owner_key is not None:
        # 人工审核闸口（D-23）：公共资产重新生成后原审核结论失效，需重新提交审核
        # 与 API 通道（generate.py 实体生成端点）行为一致
        from app.services.assets_review import reset_assets_review

        reset_assets_review(db, project_id)
    db.commit()
    db.refresh(job)

    from app.services import approvals

    approvals.mark_task_finished(db, confirmation_id, f"已创建生成任务 {job.id}")
    return {
        "job_id": job.id,
        "status": job.status,
        "job_type": job.job_type,
        "request_key": request_key,
        "cost_basis": cost_basis,
        "reused": False,
    }


# --- 注册表（Schema 化：每工具含 input_model 与风险/超时/重试元数据） ---

TOOL_REGISTRY: dict = {
    "project_overview": {
        "description": "查询项目概览：基础信息、当前制作阶段、镜头进度",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ProjectOverviewInput,
        "params": {"project_id": "项目 ID（prj_ 前缀）"},
        "fn": tool_project_overview,
    },
    "list_jobs": {
        "description": "查询项目任务列表：生成任务的状态与错误信息（可按状态过滤）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListJobsInput,
        "params": {
            "project_id": "项目 ID",
            "status": "可选：任务状态过滤（queued/running/succeeded/failed/cancelled）",
            "limit": "可选：返回条数上限，默认 20，最大 50",
        },
        "fn": tool_list_jobs,
    },
    "asset_stats": {
        "description": "查询项目资产统计：按类型（角色图/场景图/道具图/分镜图/视频等）计数",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": AssetStatsInput,
        "params": {"project_id": "项目 ID"},
        "fn": tool_asset_stats,
    },
    "list_assets": {
        "description": "查询项目生成的资产清单（图片/视频等）：类型、名称、版本、有效性、质量状态，可返回给前端展示缩略图",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListAssetsInput,
        "params": {
            "project_id": "项目 ID",
            "asset_type": "可选：资产类型过滤（keyframe/clip/character/scene/prop/reference）",
            "validity": "可选：有效性过滤（current/stale/superseded）",
            "limit": "可选：返回条数上限，默认 8，最大 30",
            "offset": "可选：偏移量（翻页用），默认 0",
        },
        "fn": tool_list_assets,
    },
    "list_asset_plans": {
        "description": "查询项目资产定义清单：类型/名称/状态/人工确认标记/依赖摘要（哪些定义还没生成、待确认）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListAssetPlansInput,
        "params": {
            "project_id": "项目 ID",
            "plan_type": "可选：定义类型过滤（character/scene/prop/keyframe/clip）",
            "status": "可选：状态过滤（pending/generated/reviewed/rejected）",
            "limit": "可选：返回条数上限，默认 20，最大 50",
            "offset": "可选：偏移量（翻页用），默认 0",
        },
        "fn": tool_list_asset_plans,
    },
    "request_generate": {
        "description": "提交生成申请（需人工审批）：只创建审批记录，不建任务、不消耗算力；镜头目标给 shot_id，公共资产目标（角色/场景/道具）给 entity_id",
        "permission": "generate",
        "risk_level": "medium",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": RequestGenerateInput,
        "params": {
            "project_id": "项目 ID",
            "shot_id": "镜头 ID（镜头目标必填）",
            "entity_id": "实体 ID（角色/场景/道具目标必填）",
            "target": "keyframe/clip/audio/bgm/sfx/character/scene/prop",
            "prompt": "可选：追加提示词",
        },
        "fn": tool_request_generate,
    },
    "run_generate": {
        "description": "执行已批准的生成申请：创建生成任务并入队（须先通过人工审批）",
        "permission": "generate",
        "risk_level": "high",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": True,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": RunGenerateInput,
        "params": {"confirmation_id": "审批记录 ID"},
        "fn": tool_run_generate,
    },
}

# 意图 → 候选工具映射（只向模型暴露当前意图需要的工具，降低选错/越权/Token 消耗）
INTENT_TOOL_MAPPING: dict[str, list[str]] = {
    "query": [
        "project_overview",
        "list_jobs",
        "asset_stats",
        "list_assets",
        "list_asset_plans",
    ],
    "analysis": [
        "project_overview",
        "asset_stats",
        "list_jobs",
        "list_assets",
        "list_asset_plans",
    ],
}


def get_allowed_tools(intent: str) -> list[str]:
    """根据意图返回候选工具名列表（未映射意图返回空列表，由调用方决定降级策略）。"""
    return list(INTENT_TOOL_MAPPING.get(intent, []))


def tool_specs() -> list[dict]:
    """工具元数据清单（供意图路由与文档展示，不含可调用函数）。"""
    return [
        {
            "name": name,
            "description": spec["description"],
            "permission": spec["permission"],
            "risk_level": spec.get("risk_level", "low"),
            "side_effect": spec["side_effect"],
            "idempotent": spec["idempotent"],
            "requires_approval": spec["requires_approval"],
            "timeout_seconds": spec.get("timeout_seconds", 30),
            "max_retries": spec.get("max_retries", 0),
            "params": spec["params"],
            "input_schema": spec["input_model"].model_json_schema() if spec.get("input_model") else None,
        }
        for name, spec in TOOL_REGISTRY.items()
    ]


def execute_tool(db: Session, name: str, *, approved: bool = False, **kwargs) -> ToolResult:
    """执行注册工具：审批门禁 → Schema 参数校验 → 执行 → 结构化结果。

    - requires_approval=True 且 approved=False：返回 approval_required，不执行、无副作用
    - 入参经 input_model.model_validate 校验，ValidationError → tool_invalid_args
    - 未知工具/执行错误分类返回，不外泄内部细节
    """
    start = time.monotonic()
    spec = TOOL_REGISTRY.get(name)
    if not spec:
        return ToolResult(ok=False, error_code="tool_not_found", error_message=f"未注册的工具: {name}")
    if spec.get("requires_approval") and not approved:
        return ToolResult(
            ok=False,
            error_code="approval_required",
            error_message=f"工具 {name} 需要人工审批后执行",
            data={"tool": name, "params": kwargs, "permission": spec.get("permission")},
        )
    input_model = spec.get("input_model")
    try:
        if input_model is not None:
            validated = input_model.model_validate(kwargs)
            data = spec["fn"](db, **validated.model_dump())
        else:
            data = spec["fn"](db, **kwargs)
        return ToolResult(ok=True, data=data, duration_ms=int((time.monotonic() - start) * 1000))
    except ToolError as e:
        return ToolResult(ok=False, error_code=e.code, error_message=e.message, duration_ms=int((time.monotonic() - start) * 1000))
    except ValidationError as e:
        return ToolResult(ok=False, error_code="tool_invalid_args", error_message=f"工具参数错误: {e}", duration_ms=int((time.monotonic() - start) * 1000))
    except TypeError as e:
        return ToolResult(ok=False, error_code="tool_invalid_args", error_message=f"工具参数错误: {e}", duration_ms=int((time.monotonic() - start) * 1000))
    except Exception:  # 工具内部错误不外泄细节，仅分类码
        return ToolResult(ok=False, error_code="tool_internal_error", error_message="工具执行失败", duration_ms=int((time.monotonic() - start) * 1000))
