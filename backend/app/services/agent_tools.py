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


class GetStyleConfigInput(BaseModel):
    """get_style_config 入参。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")


# --- 生成进度 / 诊断 / 实体定位类入参（全部只读） ---


class ProjectProgressInput(BaseModel):
    """资产图 / 分镜图 / 分镜视频进度入参（三张进度表口径一致）。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")
    failed_hours: int = Field(default=24, ge=1, le=168, description="可选：最近失败统计窗口（小时），默认 24")


class GetJobInput(BaseModel):
    """get_job 入参。"""

    job_id: str = Field(min_length=1, description="任务 ID（job_ 前缀）")


class ListProjectEntitiesInput(BaseModel):
    """list_project_entities 入参：按名称/ID 定位角色、场景、道具。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")
    query: str | None = Field(
        default=None,
        description="可选：名称或 ID 关键字（模糊匹配）；不传则列出全部实体（最多 limit 条）",
    )
    entity_type: str | None = Field(
        default=None,
        description="可选：实体类型过滤（character/scene/prop）",
    )
    limit: int = Field(default=50, ge=1, le=200, description="可选：返回条数上限，默认 50")


class ListProjectShotsInput(BaseModel):
    """list_project_shots 入参：按镜号/场景定位镜头。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")
    episode: int | None = Field(default=None, ge=1, description="可选：集数过滤")
    scene_id: str | None = Field(default=None, description="可选：场景 ID（scn_ 前缀）")
    shot_seq: int | None = Field(default=None, ge=1, description="可选：场景内镜号（『第 N 镜』）")
    limit: int = Field(default=60, ge=1, le=200, description="可选：返回条数上限，默认 60")


class GetParseStatusInput(BaseModel):
    """get_parse_status 入参。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")


class ListCompositionsInput(BaseModel):
    """list_compositions 入参。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")
    limit: int = Field(default=10, ge=1, le=50, description="可选：返回条数上限，默认 10")


class ListExportsInput(BaseModel):
    """list_exports 入参。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")


class ListAssetVersionsInput(BaseModel):
    """list_asset_versions 入参：版本历史与候选版本。"""

    asset_id: str = Field(min_length=1, description="资产 ID（ast_ 前缀）")
    limit: int = Field(default=20, ge=1, le=100, description="可选：返回条数上限，默认 20")


class GetShotImpactInput(BaseModel):
    """get_shot_impact 入参：改动影响面预判。"""

    shot_id: str = Field(min_length=1, description="镜头 ID（sht_ 前缀）")


class UpdateStyleConfigInput(BaseModel):
    """update_style_config 入参：只提交需要修改的字段。"""

    session_id: str = Field(min_length=1, description="会话 ID（审批记录归属）")
    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")
    genre_tags: list[str] | None = Field(default=None, description="可选：题材标签，第一个为主类型")
    narrative_styles: list[str] | None = Field(default=None, description="可选：叙事风格，第一个为主风格")
    visual_style: str | None = Field(default=None, description="可选：主视觉风格（单选）")
    pacing: str | None = Field(default=None, description="可选：剧情节奏")
    emotional_tone: list[str] | None = Field(default=None, description="可选：情绪基调，第一个为主情绪")
    conflict_intensity: str | None = Field(default=None, description="可选：冲突强度")
    twist_density: str | None = Field(default=None, description="可选：反转密度")
    dialogue_style: str | None = Field(default=None, description="可选：对白风格")
    narration_mode: str | None = Field(default=None, description="可选：叙述方式")
    target_audience: str | None = Field(default=None, description="可选：目标受众")
    custom_style_requirements: str | None = Field(
        default=None, description="可选：自定义风格要求（不覆盖资产绑定等硬约束）"
    )


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
    validity: str | None = None,  # deprecated
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


# --- 生成进度（只读：与资产库/分镜图/分镜看板页面同口径） ---


def _progress_conflict(project_id: str) -> None:
    if not project_id:
        raise ToolError("invalid_args", "缺少项目 ID")


def tool_get_asset_generation_progress(
    db: Session, project_id: str, failed_hours: int = 24
) -> dict:
    """公共资产图进度：角色定妆图/场景基准图/道具锚定图 的分组 done/total、在途明细、最近失败。"""
    from app.api.v1.progress import asset_generation_progress

    _progress_conflict(project_id)
    data = asset_generation_progress(project_id, failed_hours=failed_hours, db=db)
    return {
        "total": data["total"],
        "done": data["done"],
        "percent": data["percent"],
        "all_done": data["all_done"],
        "groups": data["groups"],
        "inflight_count": data["inflight_count"],
        "inflight": data["inflight"],
        "failed_recent_count": data["failed_recent_count"],
        "failed_recent": data["failed_recent"],
        "has_inflight": data["has_inflight"],
        "last_completed_at": data["last_completed_at"],
        "assets_review_status": data.get("assets_review_status"),
    }


def tool_get_keyframe_generation_progress(
    db: Session, project_id: str, failed_hours: int = 24
) -> dict:
    """分镜图进度：全片镜头总数、已出图、已确认、在途明细、最近失败。"""
    from app.api.v1.progress import keyframe_generation_progress

    _progress_conflict(project_id)
    data = keyframe_generation_progress(project_id, failed_hours=failed_hours, db=db)
    return {
        "total": data["total"],
        "with_keyframe": data["with_keyframe"],
        "confirmed": data["confirmed"],
        "percent": data["percent"],
        "all_done": data["all_done"],
        "all_confirmed": data["all_confirmed"],
        "inflight_count": data["inflight_count"],
        "inflight": data["inflight"],
        "failed_recent_count": data["failed_recent_count"],
        "failed_recent": data["failed_recent"],
        "has_inflight": data["has_inflight"],
        "last_completed_at": data["last_completed_at"],
    }


def tool_get_clip_generation_progress(
    db: Session, project_id: str, failed_hours: int = 24
) -> dict:
    """分镜视频进度：已生成/总数、正在渲染的镜头、排队名次、剩余耗时估算、最近失败。"""
    from app.api.v1.progress import clip_generation_progress

    _progress_conflict(project_id)
    data = clip_generation_progress(project_id, failed_hours=failed_hours, db=db)
    return {
        "total": data["total"],
        "with_clip": data["with_clip"],
        "percent": data["percent"],
        "all_done": data["all_done"],
        "inflight_count": data["inflight_count"],
        "inflight_queued": data["inflight_queued"],
        "inflight_running": data["inflight_running"],
        "inflight_chained_wait": data["inflight_chained_wait"],
        "inflight": data["inflight"],
        "avg_seconds": data["avg_seconds"],
        "estimated_wait_seconds": data["estimated_wait_seconds"],
        "failed_recent_count": data["failed_recent_count"],
        "failed_recent": data["failed_recent"],
        "has_inflight": data["has_inflight"],
        "last_completed_at": data["last_completed_at"],
    }


# --- 任务诊断（只读） ---


def tool_get_job(db: Session, job_id: str) -> dict:
    """单个任务详情：状态、错误码/原因、重试次数、可重试性、产物摘要与输入参数摘要。"""
    job = db.get(Job, job_id)
    if job is None:
        raise ToolError("job_not_found", "任务不存在")
    # 只给参数摘要：提示词截断 + 关键标记位，避免把整份工作流入参塞进对话
    params = job.params or {}
    param_summary = {
        k: (str(v)[:200] if isinstance(v, str) else v)
        for k, v in params.items()
        if not k.startswith("__") and k not in ("prompt_compilation",)
    }
    for marker in ("__character_id__", "__scene_id__", "__prop_id__"):
        if params.get(marker):
            param_summary[marker] = params[marker]
    result = job.result or {}
    artifact_summary = {
        k: result.get(k)
        for k in ("asset_id", "asset_ids", "file_path", "duration", "count")
        if k in result
    }
    return {
        "id": job.id,
        "project_id": job.project_id,
        "shot_id": job.shot_id,
        "job_type": job.job_type,
        "status": job.status,
        "error_code": job.error_code,
        "error_msg": job.error_msg,
        "attempts": job.attempts or 0,
        "retryable": job.status in ("failed", "lost"),
        "workflow_id": job.workflow_id,
        "params_summary": param_summary,
        "result_summary": artifact_summary,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


# --- 实体与镜头定位（只读：把名称解析成 ID，避免反问用户要 ID） ---


def tool_list_project_entities(
    db: Session,
    project_id: str,
    query: str | None = None,
    entity_type: str | None = None,
    limit: int = 50,
) -> dict:
    """列项目角色/场景/道具：ID、名称、是否已有定妆图/基准图/锚定图（支持名称模糊匹配）。"""
    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    cap = min(max(1, limit), 200)
    keyword = (query or "").strip().lower()
    allowed = {"character", "scene", "prop"}
    wanted = {entity_type} if entity_type in allowed else allowed

    items: list[dict] = []
    if "character" in wanted:
        for c in db.scalars(select(Character).where(Character.project_id == project_id)).all():
            if keyword and keyword not in c.name.lower() and keyword not in c.id.lower():
                continue
            items.append(
                {
                    "id": c.id,
                    "type": "character",
                    "name": c.name,
                    "has_image": bool(c.portrait_asset_id),
                }
            )
    if "scene" in wanted:
        for s in db.scalars(
            select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq)
        ).all():
            if keyword and keyword not in (s.title or "").lower() and keyword not in s.id.lower():
                continue
            items.append(
                {
                    "id": s.id,
                    "type": "scene",
                    "name": s.title,
                    "has_image": bool(s.baseline_asset_id),
                }
            )
    if "prop" in wanted:
        for pr in db.scalars(select(Prop).where(Prop.project_id == project_id)).all():
            if keyword and keyword not in pr.name.lower() and keyword not in pr.id.lower():
                continue
            items.append(
                {
                    "id": pr.id,
                    "type": "prop",
                    "name": pr.name,
                    "has_image": bool(pr.anchor_asset_id),
                }
            )
    return {"items": items[:cap], "count": min(len(items), cap), "matched_total": len(items)}


def tool_list_project_shots(
    db: Session,
    project_id: str,
    episode: int | None = None,
    scene_id: str | None = None,
    shot_seq: int | None = None,
    limit: int = 60,
) -> dict:
    """列项目镜头：镜号、场景、是否有分镜图/视频、分镜图是否已确认（支持定位『第 N 镜』）。"""
    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    cap = min(max(1, limit), 200)
    q = (
        select(Shot, Scene)
        .join(Scene, Shot.scene_id == Scene.id)
        .where(Scene.project_id == project_id)
    )
    if episode is not None:
        q = q.where(Shot.episode == episode)
    if scene_id:
        q = q.where(Shot.scene_id == scene_id)
    if shot_seq is not None:
        q = q.where(Shot.seq == shot_seq)
    rows = db.execute(q.order_by(Shot.episode, Scene.seq, Shot.seq).limit(cap)).all()

    shot_ids = [s.id for s, _ in rows]
    has_keyframe: set[str] = set()
    has_clip: set[str] = set()
    if shot_ids:
        asset_rows = (
            db.query(Job.shot_id, Asset.asset_type)
            .join(Asset, Asset.source_job_id == Job.id)
            .filter(
                Job.shot_id.in_(shot_ids),
                Job.job_type == "generate_keyframe",
                Job.status == "succeeded",
                Asset.validity == "current",
                Asset.asset_type.in_(("keyframe", "clip")),
            )
            .distinct()
            .all()
        )
        for sid, atype in asset_rows:
            (has_keyframe if atype == "keyframe" else has_clip).add(sid)

    items = [
        {
            "shot_id": s.id,
            "episode": s.episode,
            "scene_id": sc.id,
            "scene_title": sc.title,
            "seq": s.seq,
            "global_hint": f"第{s.episode}集 场景#{sc.seq} 第{s.seq}镜",
            "has_keyframe": s.id in has_keyframe,
            "keyframe_confirmed": bool(s.keyframe_confirmed),
            "has_clip": s.id in has_clip,
        }
        for s, sc in rows
    ]
    return {"items": items, "count": len(items)}


# --- 解析 / 合成 / 导出 / 资产版本（只读） ---


def tool_get_parse_status(db: Session, project_id: str) -> dict:
    """剧本分析状态：是否已保存剧本、当前解析版本、各集解析进度、是否已产出镜头与资产。"""
    from app.models import Composition, EpisodeParseRecord, ParseVersion, Scene as _Scene
    from app.models import ScriptVersion

    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    sv = db.get(ScriptVersion, p.current_script_version_id) if p.current_script_version_id else None
    pv = db.get(ParseVersion, p.current_parse_version_id) if p.current_parse_version_id else None
    shot_count = db.scalar(
        select(func.count(Shot.id)).join(_Scene, Shot.scene_id == _Scene.id).where(
            _Scene.project_id == project_id
        )
    )
    records = db.scalars(
        select(EpisodeParseRecord).where(EpisodeParseRecord.project_id == project_id)
    ).all()
    return {
        "has_script": sv is not None,
        "script_version": sv.version if sv else None,
        "script_char_count": len(sv.content_snapshot) if sv and sv.content_snapshot else 0,
        "parse_version": pv.version if pv else None,
        "parse_version_status": pv.status if pv else None,
        "shot_count": int(shot_count or 0),
        "episode_parse_records": [
            {
                "episode": r.episode_no,
                "asset_type": r.asset_type,
                "status": r.status,
                "asset_count": r.asset_count,
                "error_msg": r.error_msg,
            }
            for r in sorted(records, key=lambda x: (x.episode_no, x.asset_type))
        ][:40],
        "composition_count": int(
            db.scalar(
                select(func.count(Composition.id)).where(Composition.project_id == project_id)
            )
            or 0
        ),
    }


def tool_list_compositions(db: Session, project_id: str, limit: int = 10) -> dict:
    """合成记录：集数、状态、是否过期、时长、告警（供判断能否导出）。"""
    from app.models import Composition

    if db.get(Project, project_id) is None:
        raise ToolError("project_not_found", "项目不存在")
    rows = db.scalars(
        select(Composition)
        .where(Composition.project_id == project_id)
        .order_by(Composition.created_at.desc())
        .limit(min(max(1, limit), 50))
    ).all()
    return {
        "items": [
            {
                "composition_id": c.id,
                "episode": c.episode,
                "status": c.status,
                "stale": bool(c.stale),
                "duration": c.duration,
                "has_file": bool(c.file_path),
                "warnings": (c.warnings or [])[:5],
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ],
        "count": len(rows),
    }


def tool_list_exports(db: Session, project_id: str) -> dict:
    """导出记录：交付档位、状态、文件大小（导出 id 可直接用于下载）。"""
    from app.models import Export

    if db.get(Project, project_id) is None:
        raise ToolError("project_not_found", "项目不存在")
    rows = db.scalars(
        select(Export).where(Export.project_id == project_id).order_by(Export.created_at.desc())
    ).all()
    return {
        "items": [
            {
                "export_id": e.id,
                "composition_id": e.composition_id,
                "format": e.format,
                "delivery_profile": e.delivery_profile,
                "status": e.status,
                "file_name": e.file_name,
                "size": e.size,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in rows[:20]
        ],
        "count": len(rows),
    }


def tool_list_asset_versions(db: Session, asset_id: str, limit: int = 20) -> dict:
    """资产版本历史：版本号、有效性、是否候选版本、来源任务、创建时间（供采纳/回滚决策）。"""
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise ToolError("asset_not_found", "资产不存在")
    rows = db.scalars(
        select(Asset)
        .where(Asset.project_id == asset.project_id, Asset.name == asset.name)
        .order_by(Asset.version.desc())
        .limit(min(max(1, limit), 100))
    ).all()
    return {
        "asset_id": asset.id,
        "name": asset.name,
        "asset_type": asset.asset_type,
        "current_version": asset.version,
        "lock": bool(asset.locked),
        "versions": [
            {
                "asset_id": a.id,
                "version": a.version,
                "validity": a.validity,
                "locked": bool(a.locked),
                "quality_status": a.quality_status,
                "source_job_id": a.source_job_id,
                "created_at": a.created_at.isoformat() if getattr(a, "created_at", None) else None,
            }
            for a in rows
        ],
    }


def tool_get_shot_impact(db: Session, shot_id: str) -> dict:
    """镜头改动影响面：该镜现有产物、生效资产是否锁定、下游是否需要重做（重生成前预判）。"""
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise ToolError("shot_not_found", "镜头不存在")
    scene = db.get(Scene, shot.scene_id) if shot.scene_id else None
    rows = (
        db.query(Asset.asset_type, Asset.id, Asset.locked, Asset.version)
        .join(Job, Job.id == Asset.source_job_id)
        .filter(
            Job.shot_id == shot.id,
            Job.status == "succeeded",
            Asset.validity == "current",
        )
        .all()
    )
    return {
        "shot_id": shot.id,
        "episode": shot.episode,
        "scene_id": shot.scene_id,
        "scene_title": scene.title if scene else None,
        "seq": shot.seq,
        "shot_status": shot.status,
        "keyframe_confirmed": bool(shot.keyframe_confirmed),
        "current_assets": [
            {"asset_type": t, "asset_id": aid, "version": ver, "locked": bool(locked)}
            for t, aid, locked, ver in rows
        ],
        "current_asset_types": sorted({t for t, *_ in rows}),
        "requires_prev_frame": bool(shot.requires_prev_frame),
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


# 制作阶段顺序（与 progress.derive_progress 的 12 阶段一致，ARCHITECTURE.md §4.1）
_STAGE_ORDER = [
    "estimate",
    "select_range",
    "parse",
    "characters",
    "scenes",
    "assets_review",
    "keyframes",
    "clips",
    "audio",
    "compose",
    "export",
    "done",
]

# 生成目标 → 允许生成的最早制作阶段（状态机门禁：
# 目标生成不得早于其所属制作阶段，阶段判定以 derive_progress 为唯一权威）
_STAGE_GATE_MIN_STAGE = {
    "character": "characters",  # 角色定妆图：characters 阶段起（重生成会重置资产审核，D-23）
    "scene": "scenes",  # 场景基准图：scenes 阶段起
    "prop": "characters",  # 道具锚定图：公共资产，与角色同批可生成
    # 分镜图：公共资产齐全（assets_review 阶段）即可发起申请，
    # 审核未通过/资产缺失/版本无效等细粒度阻断由 D-23 evaluate_assets_gate 给出精确错误码
    # （assets_review_pending 等），阶段门禁只拦截更早阶段（estimate/parse/characters/scenes）
    "keyframe": "assets_review",
    # clip/audio 与 API 通道同等强度：项目级门禁到 keyframes 阶段
    # （公共资产审核通过），单镜细粒度由 D-13（keyframe_confirmed）逐镜校验，
    # 不要求全部分镜图/视频完成（逐镜生成是正常操作）
    "clip": "keyframes",
    "audio": "keyframes",
}


def _assert_stage_gate(db: Session, project_id: str, target: str) -> None:
    """状态机门禁：生成目标不得早于其所属制作阶段。

    阶段以 derive_progress（ARCHITECTURE.md §4.1）为唯一权威判定；
    阶段不足时拒绝，不猜测、不降级、不跳过人工审核闸口。
    bgm/sfx 不在 12 阶段状态机内，不做阶段门禁。
    """
    required = _STAGE_GATE_MIN_STAGE.get(target)
    if required is None:
        return
    from app.models import Project
    from app.services.progress import derive_progress

    project = db.get(Project, project_id)
    if project is None:
        raise ToolError("project_not_found", "项目不存在")
    _total, _done, stage = derive_progress(db, project)
    if _STAGE_ORDER.index(stage) < _STAGE_ORDER.index(required):
        raise ToolError(
            "stage_gate_blocked",
            f"当前制作阶段为 {stage}，生成 {target} 需项目进入 {required} 阶段；"
            f"请先完成当前阶段的操作（含人工审核）后再发起",
        )


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
        # 状态机门禁：申请侧快速失败，阶段不足不建审批记录
        _assert_stage_gate(db, project_id, target)
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
    # 状态机门禁：申请侧快速失败，阶段不足不建审批记录
    _assert_stage_gate(db, project_id, target)
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
    # 状态机门禁（纵深防御）：执行侧再次校验，
    # 防止审批通过后项目阶段回退（如剧本变更回到 estimate）仍执行生成
    _assert_stage_gate(db, project_id, target)

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
        # 空 prompt 兜底：统一走阶段 B 提示词编译器（与 API 通道完全同源），
        # 按实体结构化资料 + 集中模板编译，避免空提示词提交 ComfyUI 浪费算力
        from app.services.asset_prompts import ASSET_PROMPTS_VERSION

        from app.api.v1.generate import _compile_asset_prompt

        if not params.get("prompt"):
            params.pop("prompt", None)
        params, _compilation = _compile_asset_prompt(db, target, entity.id, wf, params)
        params["__asset_prompt_version__"] = ASSET_PROMPTS_VERSION
        params[owner_key] = entity.id
    else:
        # 镜头目标：与 API 通道（generate.py 单镜生成端点）行为对齐
        from fastapi import HTTPException

        from app.api.v1.generate import (
            _compile_shot_prompt,
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
            # 阶段 B：分镜图/分镜视频提示词编译（与 API 通道同源，参考槽位按工作流真实能力校验）
            compilation: dict | None = None
            if target in ("keyframe", "clip"):
                _fallback = params.get("prompt") or shot.image_prompt or ""
                params, compilation = _compile_shot_prompt(
                    db, shot, wf, params, target, _fallback
                )
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
            **(
                {"prompt_compilation": compilation}
                if (owner_key is None and locals().get("compilation"))
                else {}
            ),
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

# --- 风格配置工具（读 / 申请 / 执行，写操作走审批） ---
_STYLE_FIELDS = (
    "genre_tags",
    "narrative_styles",
    "visual_style",
    "pacing",
    "emotional_tone",
    "conflict_intensity",
    "twist_density",
    "dialogue_style",
    "narration_mode",
    "target_audience",
    "custom_style_requirements",
)


class RunStyleUpdateInput(BaseModel):
    """run_style_update 入参。"""

    confirmation_id: str = Field(min_length=1, description="审批记录 ID")


def tool_get_style_config(db: Session, project_id: str) -> dict:
    """读取项目风格配置：规范化结果 + 校验问题 + 可选值清单（只读）。"""
    from app.services import style_config

    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    cfg, issues = style_config.validate_style_config(p.settings or {})
    return {
        "project_id": p.id,
        "version": style_config.STYLE_CONFIG_VERSION,
        "saved_version": (p.settings or {}).get("style_config_version"),
        "config": cfg,
        "issues": issues,
        "options": style_config.options_payload(),
    }


def tool_request_style_update(db: Session, session_id: str, project_id: str, **fields) -> dict:
    """提交风格配置变更申请（需人工审批）：只建审批记录，不改配置、不影响在途任务。"""
    from app.services import approvals

    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    patch = {k: v for k, v in fields.items() if k in _STYLE_FIELDS and v is not None}
    if not patch:
        raise ToolError("tool_invalid_args", "没有需要修改的风格字段")
    confirmation = approvals.create_confirmation(
        db,
        session_id=session_id,
        tool_name="run_style_update",
        args={"project_id": project_id, **patch},
        goal="更新项目剧本类型与风格配置（需人工审批）",
        risk_level="medium",
    )
    return {
        "confirmation_id": confirmation.id,
        "status": confirmation.status,
        "project_id": project_id,
        "pending_changes": patch,
        "note": "风格配置仅在审批通过后生效，审批前项目配置保持不变",
    }


def tool_run_style_update(db: Session, confirmation_id: str) -> dict:
    """执行已批准的风格变更：合并现有配置后规范化落库，返回问题码与变更字段。

    双重门禁：审批状态必须为 approved，且审批记录必须真实存在，
    避免仅凭调用方传入的放行标志绕过审批。
    """
    from app.models import AgentConfirmation
    from app.services import approvals, style_config

    confirmation = db.get(AgentConfirmation, confirmation_id)
    if confirmation is None:
        raise ToolError("confirmation_not_found", "审批记录不存在")
    if confirmation.status != "approved":
        raise ToolError(
            "approval_required",
            f"审批未通过（当前 {confirmation.status}），不能修改风格配置",
        )
    args = dict(confirmation.args or {})
    project_id = args.pop("project_id", None)
    if not project_id:
        raise ToolError("tool_invalid_args", "审批记录缺少项目 ID")
    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    before, _ = style_config.validate_style_config(p.settings or {})
    merged = dict(p.settings or {})
    merged.update(args)
    cfg, issues = style_config.validate_style_config(merged)
    merged.update(cfg)
    merged["style_config_version"] = style_config.STYLE_CONFIG_VERSION
    p.settings = merged
    db.commit()
    db.refresh(p)
    approvals.mark_task_finished(db, confirmation_id, "已更新项目风格配置")
    changed = [k for k in _STYLE_FIELDS if before.get(k) != cfg.get(k)]
    return {
        "project_id": p.id,
        "version": style_config.STYLE_CONFIG_VERSION,
        "config": cfg,
        "issues": issues,
        "changed_fields": changed,
        "note": "配置仅影响后续任务，不会自动重生成已审核资产与已生成结果",
    }


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
    "get_style_config": {
        "description": "读取项目剧本类型与风格配置：题材、叙事风格、主视觉风格、节奏、情绪、受众及校验问题",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": GetStyleConfigInput,
        "params": {"project_id": "项目 ID（prj_ 前缀）"},
        "fn": tool_get_style_config,
    },
    "request_style_update": {
        "description": "提交项目风格配置变更申请（需人工审批）：只建审批记录，不改配置；只提交需要修改的字段",
        "permission": "write",
        "risk_level": "medium",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": UpdateStyleConfigInput,
        "params": {
            "session_id": "会话 ID（审批记录归属）",
            "project_id": "项目 ID",
            "visual_style": "可选：主视觉风格（单选）",
            "genre_tags": "可选：题材标签列表",
            "narrative_styles": "可选：叙事风格列表",
            "pacing": "可选：剧情节奏",
            "emotional_tone": "可选：情绪基调列表",
            "custom_style_requirements": "可选：自定义风格要求",
        },
        "fn": tool_request_style_update,
    },
    "run_style_update": {
        "description": "执行已批准的风格配置变更：合并现有配置后规范化落库（须先通过人工审批）",
        "permission": "write",
        "risk_level": "high",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": True,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": RunStyleUpdateInput,
        "params": {"confirmation_id": "审批记录 ID"},
        "fn": tool_run_style_update,
    },
    # --- 生成进度（只读：与资产库/分镜图/分镜看板页面同口径） ---
    "get_asset_generation_progress": {
        "description": (
            "查询公共资产图生成进度：角色定妆图/场景基准图/道具锚定图的 done/total、"
            "正在生成的条目与百分比、最近失败"
        ),
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ProjectProgressInput,
        "params": {
            "project_id": "项目 ID",
            "failed_hours": "可选：最近失败统计窗口（小时），默认 24",
        },
        "fn": tool_get_asset_generation_progress,
    },
    "get_keyframe_generation_progress": {
        "description": (
            "查询分镜图生成进度：全片镜头数、已出图、已人工确认、正在生成的镜头、最近失败"
        ),
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ProjectProgressInput,
        "params": {
            "project_id": "项目 ID",
            "failed_hours": "可选：最近失败统计窗口（小时），默认 24",
        },
        "fn": tool_get_keyframe_generation_progress,
    },
    "get_clip_generation_progress": {
        "description": (
            "查询分镜视频生成进度：已生成/总数、正在渲染的镜头、排队名次、"
            "剩余耗时估算（按近 7 日平均单镜耗时）、最近失败"
        ),
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ProjectProgressInput,
        "params": {
            "project_id": "项目 ID",
            "failed_hours": "可选：最近失败统计窗口（小时），默认 24",
        },
        "fn": tool_get_clip_generation_progress,
    },
    # --- 任务诊断（只读） ---
    "get_job": {
        "description": "查询单个任务详情：状态、错误码与失败原因、重试次数、可重试性、产物与入参摘要",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": GetJobInput,
        "params": {"job_id": "任务 ID（job_ 前缀）"},
        "fn": tool_get_job,
    },
    # --- 实体与镜头定位（只读：把名称解析成 ID） ---
    "list_project_entities": {
        "description": (
            "列出项目角色/场景/道具的 ID 与名称（支持名称模糊匹配），并标注是否已有定妆图/基准图/锚定图；"
            "用于把用户说的名字解析成 chr_/scn_/prp_ 实体 ID，不要反问用户要 ID"
        ),
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListProjectEntitiesInput,
        "params": {
            "project_id": "项目 ID",
            "query": "可选：名称或 ID 关键字（模糊匹配）",
            "entity_type": "可选：character / scene / prop",
            "limit": "可选：返回条数上限，默认 50",
        },
        "fn": tool_list_project_entities,
    },
    "list_project_shots": {
        "description": (
            "列出项目镜头：集数、场景、镜号、是否有分镜图/视频、分镜图是否已确认；"
            "按 episode + shot_seq 可定位『第 N 镜』，用于把镜号解析成 sht_ 镜头 ID"
        ),
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListProjectShotsInput,
        "params": {
            "project_id": "项目 ID",
            "episode": "可选：集数",
            "scene_id": "可选：场景 ID",
            "shot_seq": "可选：场景内镜号",
            "limit": "可选：返回条数上限，默认 60",
        },
        "fn": tool_list_project_shots,
    },
    # --- 解析 / 合成 / 导出 / 资产版本（只读） ---
    "get_parse_status": {
        "description": "查询剧本分析状态：是否已保存剧本、当前解析版本、各集解析进度、镜头与资产是否已产出",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": GetParseStatusInput,
        "params": {"project_id": "项目 ID"},
        "fn": tool_get_parse_status,
    },
    "list_compositions": {
        "description": "查询合成记录：集数、状态、是否过期、时长、告警（用于判断能否导出）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListCompositionsInput,
        "params": {"project_id": "项目 ID", "limit": "可选：返回条数上限，默认 10"},
        "fn": tool_list_compositions,
    },
    "list_exports": {
        "description": "查询导出记录：导出 ID、交付档位、状态、文件大小（导出 ID 可用于下载）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListExportsInput,
        "params": {"project_id": "项目 ID"},
        "fn": tool_list_exports,
    },
    "list_asset_versions": {
        "description": "查询资产版本历史：版本号、有效性、是否锁定、质量状态、来源任务（用于采纳候选/回滚决策）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListAssetVersionsInput,
        "params": {"asset_id": "资产 ID（ast_ 前缀）", "limit": "可选：返回条数上限，默认 20"},
        "fn": tool_list_asset_versions,
    },
    "get_shot_impact": {
        "description": "查询镜头改动影响面：该镜生效产物、是否锁定、是否需前序尾帧、确认状态（重生成前预判）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": GetShotImpactInput,
        "params": {"shot_id": "镜头 ID（sht_ 前缀）"},
        "fn": tool_get_shot_impact,
    },
}

# 意图 → 候选工具映射（只向模型暴露当前意图需要的工具，降低选错/越权/Token 消耗）
# 全部为只读工具：查询/分析意图不暴露任何生成或写操作工具
_READ_ONLY_TOOLS = [
    "project_overview",
    "list_jobs",
    "get_job",
    "asset_stats",
    "list_assets",
    "list_asset_versions",
    "list_asset_plans",
    "list_project_entities",
    "list_project_shots",
    "get_shot_impact",
    "get_asset_generation_progress",
    "get_keyframe_generation_progress",
    "get_clip_generation_progress",
    "get_parse_status",
    "list_compositions",
    "list_exports",
    "get_style_config",
]

INTENT_TOOL_MAPPING: dict[str, list[str]] = {
    "query": list(_READ_ONLY_TOOLS),
    "analysis": list(_READ_ONLY_TOOLS),
}


def get_allowed_tools(intent: str) -> list[str]:
    """根据意图返回候选工具名列表（未映射意图返回空列表，由调用方决定降级策略）。"""
    return list(INTENT_TOOL_MAPPING.get(intent, []))


# --- 对话上下文用只读工具编排（Agent chat 注入上下文时调用） ---

# 进度类关键词：命中即注入三张进度表（生成进度是最高频问题）
_PROGRESS_HINTS = (
    "进度", "进展", "到哪", "哪一步", "还有多少", "还剩", "排队", "多久", "什么时候",
    "生成", "出图", "渲染", "完成", "状态",
)
# 实体/镜头定位关键词：命中即注入角色/场景/道具与镜头清单（把名称解析成 ID，避免反问）
# 只收领域名词+定位词，避免「哪些/哪个」这类泛化词把无关问题也拖进实体查询
_ENTITY_HINTS = (
    "角色", "场景", "道具", "人物", "镜头", "第几镜", "哪一镜", "第几集", "谁", "资产", "定妆",
    "基准图", "锚定",
)
# 疑问词 + 实体对象才算「要列实体」：『有哪些角色』命中，『导出记录有哪些』不命中
_ENTITY_LIST_PATTERN = __import__("re").compile(
    r"(有哪些|哪几个|列出|所有|全部|哪些)(角色|场景|道具|人物|镜头|资产)"
)
# 解析与成片关键词
_PARSE_HINTS = ("剧本", "解析", "分析", "场景", "镜头", "资产定义", "集")
_COMPOSE_HINTS = ("合成", "导出", "成片", "交付", "视频文件", "下载", "验收")
_JOB_HINTS = ("失败", "报错", "错误", "重试", "卡住", "超时", "失败原因")
# 镜头序号：只认『第 N 镜 / 第 N 个镜头』，避免『第一帧』『第三集』误命中
_SHOT_SEQ_PATTERN = __import__("re").compile(r"第\s*(\d+)\s*(?:个)?\s*镜头?")
# 集数：『第 2 集』
_EPISODE_PATTERN = __import__("re").compile(r"第\s*(\d+)\s*集")


def build_read_tool_calls(text: str, project_id: str) -> list[tuple[str, dict]]:
    """按用户消息挑选要执行的只读工具（返回 [(tool_name, args)]）。

    - 进度类问题：三张进度表全部注入（回答进度必须准确到 X/Y、正在生成哪几个、排队与预计耗时）
    - 实体/镜头类问题：注入项目实体与镜头清单，用于把名称/镜号解析成 chr_/scn_/prh_/sht_ ID
    - 解析/合成/导出/失败诊断：按关键词补充
    - 不做任何写操作：这里只返回只读工具
    """
    content = text or ""
    calls: list[tuple[str, dict]] = []

    if any(k in content for k in _PROGRESS_HINTS):
        for tool in (
            "get_asset_generation_progress",
            "get_keyframe_generation_progress",
            "get_clip_generation_progress",
        ):
            calls.append((tool, {"project_id": project_id, "failed_hours": 24}))

    # 『第 3 镜』『第 2 集』这类明确指代也算要定位镜头/实体（否则助手答得对但说不出是哪一镜）
    wanted_entities = (
        any(k in content for k in _ENTITY_HINTS)
        or bool(_ENTITY_LIST_PATTERN.search(content))
        or bool(_SHOT_SEQ_PATTERN.search(content))
        or bool(_EPISODE_PATTERN.search(content))
    )
    if wanted_entities:
        calls.append(("list_project_entities", {"project_id": project_id, "limit": 60}))
        shot_args: dict = {"project_id": project_id, "limit": 60}
        seq_m = _SHOT_SEQ_PATTERN.search(content)
        if seq_m:
            shot_args["shot_seq"] = int(seq_m.group(1))
        ep_m = _EPISODE_PATTERN.search(content)
        if ep_m:
            shot_args["episode"] = int(ep_m.group(1))
        calls.append(("list_project_shots", shot_args))

    if any(k in content for k in _PARSE_HINTS):
        calls.append(("get_parse_status", {"project_id": project_id}))

    if any(k in content for k in _COMPOSE_HINTS):
        calls.append(("list_compositions", {"project_id": project_id, "limit": 5}))
        calls.append(("list_exports", {"project_id": project_id}))

    if any(k in content for k in _JOB_HINTS):
        # 失败诊断：取最近失败任务再看详情，给出具体原因而不是「有失败」
        calls.append(("list_jobs", {"project_id": project_id, "status": "failed", "limit": 5}))
    return calls


def entity_shot_hint(db: Session, project_id: str, *, entity_limit: int = 12) -> str:
    """给澄清消息附上可选的实体与镜头清单，让用户（或下一轮识别）知道有哪些 ID 可选。

    生成目标定位不到时使用：既不猜测目标，也不把「自己去找 ID」的负担丢给用户。
    """
    try:
        entities = tool_list_project_entities(db, project_id, limit=entity_limit)
        shots = tool_list_project_shots(db, project_id, limit=entity_limit)
    except ToolError:
        return ""
    lines: list[str] = []
    if entities.get("items"):
        lines.append(
            "项目内可选实体："
            + "、".join(
                f"{it['name']}（{it['type']} {it['id']}）" for it in entities["items"][:entity_limit]
            )
        )
    if shots.get("items"):
        lines.append(
            "项目内可选镜头："
            + "、".join(
                f"第{it['episode']}集 {it['scene_title']} 镜{it['seq']}（{it['shot_id']}）"
                for it in shots["items"][:entity_limit]
            )
        )
    return "\n".join(lines)


def _short(value, limit: int = 120) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_tool_context(tool_name: str, result: ToolResult) -> str:
    """把只读工具结果压成一行提示词上下文（与页面展示口径一致，便于助手直接复述）。"""
    if not result.ok or not result.data:
        return ""
    d = result.data

    if tool_name == "get_asset_generation_progress":
        groups = d.get("groups") or {}
        group_text = "、".join(
            f"{g.get('label', k)} {g.get('done', 0)}/{g.get('total', 0)}"
            for k, g in groups.items()
            if g.get("total")
        )
        line = f"公共资产图进度：{d.get('done', 0)}/{d.get('total', 0)}（{d.get('percent', 0)}%）"
        if group_text:
            line += f"；分组：{group_text}"
        inflight = d.get("inflight") or []
        if inflight:
            line += "；生成中：" + "、".join(
                f"{j.get('kind_label') or j.get('kind')}·{j.get('entity_name')}"
                f"（{j.get('status')}{' ' + str(j.get('progress')) + '%' if j.get('progress') is not None else ''}）"
                for j in inflight[:6]
            )
        if d.get("failed_recent_count"):
            line += f"；最近失败 {d['failed_recent_count']} 个：" + "、".join(
                f"{f.get('entity_name')}（{_short(f.get('error_msg'), 60)}）"
                for f in (d.get("failed_recent") or [])[:3]
            )
        if d.get("all_done"):
            line += "；已全部完成"
        return line

    if tool_name == "get_keyframe_generation_progress":
        line = (
            f"分镜图进度：已出图 {d.get('with_keyframe', 0)}/{d.get('total', 0)}"
            f"（{d.get('percent', 0)}%），已人工确认 {d.get('confirmed', 0)}"
        )
        inflight = d.get("inflight") or []
        if inflight:
            line += "；生成中：" + "、".join(
                f"{j.get('scene_title') or '未知场景'} 镜 {j.get('shot_seq') or '—'}"
                f"（{j.get('status')}）"
                for j in inflight[:6]
            )
        if d.get("failed_recent_count"):
            line += f"；最近失败 {d['failed_recent_count']} 张：" + "、".join(
                f"{f.get('scene_title') or ''} 镜 {f.get('shot_seq') or '—'}（{_short(f.get('error_msg'), 60)}）"
                for f in (d.get("failed_recent") or [])[:3]
            )
        return line

    if tool_name == "get_clip_generation_progress":
        line = (
            f"分镜视频进度：已生成 {d.get('with_clip', 0)}/{d.get('total', 0)}"
            f"（{d.get('percent', 0)}%），渲染中 {d.get('inflight_running', 0)}"
            f"，排队/等待前序 {(d.get('inflight_queued') or 0) + (d.get('inflight_chained_wait') or 0)}"
        )
        running = next(
            (j for j in (d.get("inflight") or []) if j.get("status") == "running"), None
        )
        if running:
            line += (
                f"；当前渲染：{running.get('scene_title') or '未知场景'} 镜 {running.get('shot_seq') or '—'}"
                + (f" {running.get('progress')}%" if running.get("progress") is not None else "")
            )
        wait = d.get("estimated_wait_seconds")
        if isinstance(wait, int) and wait > 0:
            line += f"；预计剩余约 {max(1, round(wait / 60))} 分钟"
            if d.get("avg_seconds"):
                line += f"（近 7 日单镜平均 {d['avg_seconds']} 秒）"
        if d.get("failed_recent_count"):
            line += f"；最近失败 {d['failed_recent_count']} 个"
        if d.get("all_done"):
            line += "；已全部完成"
        return line

    if tool_name == "list_project_entities":
        items = d.get("items") or []
        if not items:
            return "项目暂无角色/场景/道具实体"
        text = "、".join(
            f"{it.get('name')}[{it.get('type')}:{it.get('id')}"
            f"{'' if it.get('has_image') else ',未出图'}]"
            for it in items[:40]
        )
        return f"项目实体（共 {d.get('matched_total', len(items))} 个）：{text}"

    if tool_name == "list_project_shots":
        items = d.get("items") or []
        if not items:
            return "未匹配到镜头"
        text = "、".join(
            f"第{it.get('episode')}集/{it.get('scene_title')}/镜{it.get('seq')}"
            f"[{it.get('shot_id')}"
            f"{',有分镜图' if it.get('has_keyframe') else ',无分镜图'}"
            f"{',已确认' if it.get('keyframe_confirmed') else ''}"
            f"{',有视频' if it.get('has_clip') else ''}]"
            for it in items[:20]
        )
        suffix = f"（仅列前 20 条，共 {len(items)} 条）" if len(items) > 20 else ""
        return f"镜头清单：{text}{suffix}"

    if tool_name == "get_parse_status":
        parts = [
            f"剧本{'已保存 v' + str(d.get('script_version')) if d.get('has_script') else '未保存'}",
            f"解析版本 {d.get('parse_version') if d.get('parse_version') else '无'}",
            f"镜头 {d.get('shot_count', 0)} 个",
            f"合成记录 {d.get('composition_count', 0)} 条",
        ]
        records = d.get("episode_parse_records") or []
        if records:
            bad = [r for r in records if r.get("status") not in ("succeeded", "done")]
            if bad:
                parts.append(
                    "解析未完成明细："
                    + "、".join(
                        f"第{r['episode']}集{r['asset_type']}({r['status']})" for r in bad[:8]
                    )
                )
        return "解析状态：" + "；".join(parts)

    if tool_name == "list_compositions":
        items = d.get("items") or []
        if not items:
            return "暂无合成记录"
        return "合成记录（新→旧）：" + "、".join(
            f"{c.get('composition_id')}(第{c.get('episode')}集,{c.get('status')}"
            f"{',已过期' if c.get('stale') else ''})"
            for c in items[:5]
        )

    if tool_name == "list_exports":
        items = d.get("items") or []
        if not items:
            return "暂无导出记录"
        return "导出记录（新→旧）：" + "、".join(
            f"{e.get('export_id')}({e.get('delivery_profile')},{e.get('status')}"
            f"{',' + str(e.get('size')) + 'B' if e.get('size') else ''})"
            for e in items[:5]
        )

    if tool_name == "list_jobs":
        items = d.get("items") or []
        if not items:
            return "暂无匹配任务"
        return "任务明细：" + "、".join(
            f"{j.get('id')}({j.get('job_type')},{j.get('status')}"
            f"{',' + str(j.get('error_code')) if j.get('error_code') else ''})"
            for j in items[:5]
        )

    if tool_name == "get_job":
        return (
            f"任务 {d.get('id')}：{d.get('job_type')} {d.get('status')}，"
            f"错误 {_short(d.get('error_msg'), 120) if d.get('error_msg') else '无'}，"
            f"尝试 {d.get('attempts')} 次，可重试={d.get('retryable')}"
        )

    if tool_name == "list_asset_versions":
        versions = d.get("versions") or []
        return f"资产 {d.get('asset_id')}（{d.get('name')}）版本：" + "、".join(
            f"v{v.get('version')}({v.get('validity')}"
            f"{',锁定' if v.get('locked') else ''})"
            for v in versions[:8]
        )

    if tool_name == "get_shot_impact":
        return (
            f"镜头 {d.get('shot_id')}（第{d.get('episode')}集/{d.get('scene_title')}/镜{d.get('seq')}）："
            f"确认={d.get('keyframe_confirmed')}，已有产物类型={d.get('current_asset_types') or '无'}"
        )
    return ""


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
