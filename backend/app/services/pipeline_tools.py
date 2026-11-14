"""流程工具（导演台 Agent 执行流程 P2：流程接口工具化 + 状态机门禁）。

将制作流程各节点接口注册为受控工具，每个工具执行前经 pipeline_state 状态机门禁：
- 操作不得早于其所属流程节点（阶段不足 → pipeline_gate_blocked，不猜测、不降级）
- 审核闸口（资产审核/分镜审核/视频审核）未通过前不得进入下一节点
- 高风险操作（合成/导出）须人工审批后执行（requires_approval）
- 工具返回真实执行结果与执行后的流程状态（节点 + 下一步指引）

工具清单（与流程节点链一一对应）：
- create_project        新建项目
- save_script           保存剧本
- analyze_script        分析剧本（创建解析任务）
- review_assets         人工审核资产库（approve/reject）
- confirm_keyframes     批量确认分镜图（人工审核）
- compose_video         合成成片（需审批）
- export_video          导出成片（需审批）
- get_pipeline_status   查询当前流程节点与状态（只读）

生成类操作（角色图/场景图/道具图/分镜图/视频）复用既有 request_generate/run_generate
工具与阶段门禁（agent_tools._assert_stage_gate），本模块不重复注册。
"""
import hashlib
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.models import Composition, Job, ParseVersion, Project, ScriptVersion, Shot, Scene, ids
from app.models.enums import ASPECT_RATIO, CREATION_TYPE
from app.services.agent_tools import ToolError, ToolResult
from app.services.pipeline_state import (
    PipelineStateError,
    assert_action_allowed,
    get_pipeline_status,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pipeline_ctx(db: Session, project_id: str) -> dict:
    """执行后附带流程状态（节点 + 下一步指引），供 agent 给出下一步操作提示。"""
    st = get_pipeline_status(db, project_id)
    return {
        "pipeline_node": st.node_key,
        "pipeline_node_name": st.node_name,
        "stage": st.stage,
        "next_hint": st.next_hint,
    }


def _gate_error(e: PipelineStateError) -> ToolResult:
    return ToolResult(
        ok=False,
        error_code="pipeline_gate_blocked",
        error_message=e.message,
        data={"current_node": e.current_node, "required_node": e.required_node},
    )


# --- 入参 Schema ---

class CreateProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    aspect_ratio: str
    creation_type: str = "novel"
    description: str | None = Field(default=None, max_length=500)


class SaveScriptInput(BaseModel):
    project_id: str
    text: str = Field(min_length=1, max_length=50000)


class AnalyzeScriptInput(BaseModel):
    project_id: str
    episode_start: int = Field(default=1, ge=1)
    episode_count: int | None = Field(default=None, ge=1)
    episode_end: int | None = Field(default=None, ge=1)
    # 发起方会话：解析为异步任务，完成后据此把结果回推到对应对话框
    session_id: str | None = None


class ReviewAssetsInput(BaseModel):
    project_id: str
    action: str  # approve / reject


class ConfirmKeyframesInput(BaseModel):
    project_id: str
    shot_ids: list[str] | None = None  # 缺省为全部已生成分镜图的镜头


class ComposeVideoInput(BaseModel):
    project_id: str
    episode: int | None = Field(default=None, ge=1)
    include_subtitle: bool = True
    subtitle_mode: str = Field(default="burn", pattern="^(burn|sidecar)$")


class ExportVideoInput(BaseModel):
    project_id: str
    composition_id: str
    delivery_profile: str = Field(default="standard", pattern="^(standard|high_quality)$")


class ConfirmParseInput(BaseModel):
    project_id: str
    parse_version_id: str | None = None  # 缺省为项目最新候选解析版本


class QueryParseStatusInput(BaseModel):
    project_id: str


class PipelineStatusInput(BaseModel):
    project_id: str


# --- 工具实现 ---

def tool_confirm_parse(db: Session, project_id: str, parse_version_id: str | None = None) -> dict:
    """确认候选解析版本（人工审核闸口）：把解析结果落库为角色/场景/道具/镜头，
    并切换为项目当前解析版本；须已完成剧本分析。

    确认前叙事实体表为空、资产库无输入，后续生成无法推进，故该步骤必须显式确认。
    落库复用 script_parse.apply_parse_result，与 REST 确认接口同一套逻辑。
    """
    try:
        assert_action_allowed(db, project_id, "confirm_parse")
    except PipelineStateError as e:
        raise ToolError("pipeline_gate_blocked", e.message)
    p = db.get(Project, project_id)
    if p is None or p.deleted_at is not None:
        raise ToolError("project_not_found", "项目不存在")
    q = db.query(ParseVersion).filter(ParseVersion.project_id == project_id)
    if parse_version_id:
        pv = q.filter(ParseVersion.id == parse_version_id).first()
    else:
        pv = q.filter(ParseVersion.status == "candidate").order_by(
            ParseVersion.version.desc()).first()
    if pv is None:
        raise ToolError("parse_version_not_found", "未找到待确认的解析版本，请先发起剧本分析")
    if pv.status != "candidate":
        raise ToolError("parse_version_conflict", "仅候选状态的解析版本可确认")
    parse_jobs = (
        db.query(Job)
        .filter(Job.project_id == project_id,
                Job.job_type.in_(["parse", "script_agent"]),
                Job.status == "succeeded")
        .order_by(Job.finished_at.desc())
        .all()
    )
    target_results = [
        (j.result, j.finished_at.isoformat() if j.finished_at else "")
        for j in parse_jobs if (j.result or {}).get("parse_version_id") == pv.id
    ]
    target_results.sort(key=lambda t: (int(t[0].get("episode_start") or 1), t[1]))
    target_results = [t[0] for t in target_results]
    stats: dict = {}
    if target_results:
        from app.models import Scene as SceneModel
        from app.services.script_parse import apply_parse_result
        current_scene_ids = [
            row[0] for row in db.query(SceneModel.id).filter(
                SceneModel.project_id == project_id).all()
        ]
        for tr in target_results:
            stats = apply_parse_result(
                db, p, tr,
                episode_start=int(tr.get("episode_start") or 1),
                episode_count=int(tr.get("episode_count") or 1),
                current_scene_ids=current_scene_ids,
            )
    old_id = p.current_parse_version_id
    if old_id:
        old = db.get(ParseVersion, old_id)
        if old is not None and old.status == "current":
            old.status = "superseded"
    pv.status = "current"
    pv.confirmed_at = _now()
    p.current_parse_version_id = pv.id
    db.commit()
    # 统计口径：直接查库统计落库后的实际实体数（apply_parse_result 的 stats 字段
    # 与增量 upsert 语义不完全对应，直接计数才能反映用户看到的真实资产规模）
    from app.models import Character as CharacterModel
    from app.models import Prop as PropModel
    from app.models import Scene as SceneModel
    from app.models import Shot as ShotModel
    scene_ids = [
        row[0] for row in db.query(SceneModel.id).filter(
            SceneModel.project_id == project_id).all()
    ]
    counts = {
        "characters": db.query(CharacterModel).filter(
            CharacterModel.project_id == project_id).count(),
        "scenes": len(scene_ids),
        "props": db.query(PropModel).filter(
            PropModel.project_id == project_id).count(),
        "shots": db.query(ShotModel).filter(
            ShotModel.scene_id.in_(scene_ids)).count() if scene_ids else 0,
    }
    return {
        "parse_version_id": pv.id,
        "version": pv.version,
        "status": pv.status,
        "counts": counts,
        "parse_stats": stats,
        **_pipeline_ctx(db, project_id),
    }


def tool_query_parse_status(db: Session, project_id: str) -> dict:
    """查询剧本分析（解析）进度（只读）：任务状态、已用时、结果摘要。

    解析是异步耗时任务，用户提交后需要能主动问『分析好了吗』并看到进展，
    否则对话只能停在受理话术上。耗时取任务开始时间与当前时间的差值。
    """
    p = db.get(Project, project_id)
    if p is None or p.deleted_at is not None:
        raise ToolError("project_not_found", "项目不存在")
    job = (
        db.query(Job)
        .filter(Job.project_id == project_id,
                Job.job_type.in_(["parse", "script_agent"]))
        .order_by(Job.created_at.desc())
        .first()
    )
    if job is None:
        return {
            "has_job": False,
            "status": "none",
            "message": "尚未发起剧本分析，可以直接对我说『开始分析剧本』。",
            **_pipeline_ctx(db, project_id),
        }
    base = job.started_at or job.created_at
    # 数据库读出的时间可能是 naive，统一去掉时区后再与当前时间相减，避免比较报错
    if base is not None and base.tzinfo is not None:
        base = base.replace(tzinfo=None)
    elapsed = int((_now() - base).total_seconds()) if base is not None else 0
    pv = None
    pv_id = (job.result or {}).get("parse_version_id")
    if pv_id:
        pv = db.get(ParseVersion, pv_id)
    data = {
        "has_job": True,
        "job_id": job.id,
        "status": job.status,
        "elapsed_seconds": elapsed,
        "started_at": base.isoformat() if base is not None else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error_code": job.error_code,
        "error_msg": (job.error_msg or "")[:300] or None,
        "parse_version_id": pv.id if pv is not None else None,
        "parse_version_status": pv.status if pv is not None else None,
        **_pipeline_ctx(db, project_id),
    }
    if job.status in ("queued", "running"):
        data["message"] = (
            f"剧本仍在分析中，已用时 {elapsed} 秒（任务 {job.id}）。\n"
            "分析完成后我会把结果直接发到这里，也可以随时问我进度。"
        )
    elif job.status == "succeeded":
        if pv is not None and pv.status == "candidate":
            data["message"] = (
                f"剧本分析已完成（用时 {elapsed} 秒），解析版本 v{pv.version} 待确认。\n"
                "回复『确认解析结果』即可把角色/场景/道具/镜头落库，并继续生成资产。"
            )
        else:
            data["message"] = (
                f"剧本分析已完成（用时 {elapsed} 秒），解析结果已确认落库，可以继续生成资产。"
            )
    elif job.status == "failed":
        data["message"] = (
            f"剧本分析失败（{job.error_code or 'unknown'}）：{(job.error_msg or '')[:200]}\n"
            "可以修正后重新对我说『开始分析剧本』。"
        )
    else:
        data["message"] = f"剧本分析任务状态：{job.status}（已用时 {elapsed} 秒）。"
    return data


def tool_create_project(db: Session, name: str, aspect_ratio: str,
                         creation_type: str = "novel", description: str | None = None) -> dict:
    """新建项目（流程起点）：校验画幅/创作类型枚举，创建后返回项目与流程状态。"""
    if aspect_ratio not in ASPECT_RATIO:
        raise ToolError("tool_invalid_args", f"aspect_ratio 须为 {ASPECT_RATIO} 之一")
    if creation_type not in CREATION_TYPE:
        raise ToolError("tool_invalid_args", f"creation_type 须为 {CREATION_TYPE} 之一")
    p = Project(
        name=name, aspect_ratio=aspect_ratio,
        creation_type=creation_type, description=description,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return {
        "project_id": p.id,
        "name": p.name,
        "status": p.status,
        "pipeline_node": "create_project",
        "pipeline_node_name": "新建项目",
        "next_hint": "项目已创建，请保存剧本",
    }


def tool_save_script(db: Session, project_id: str, text: str) -> dict:
    """保存剧本（单版本覆盖）：版本号递增，返回版本信息与流程状态。"""
    try:
        assert_action_allowed(db, project_id, "save_script")
    except PipelineStateError as e:
        raise ToolError("pipeline_gate_blocked", e.message)
    p = db.get(Project, project_id)
    if p is None or p.deleted_at is not None:
        raise ToolError("project_not_found", "项目不存在")
    if p.status == "archived":
        raise ToolError("invalid_state_transition", "已归档项目不可保存剧本")
    sv = (
        db.query(ScriptVersion)
        .filter(ScriptVersion.project_id == project_id)
        .order_by(ScriptVersion.version.desc())
        .first()
    )
    if sv is None:
        sv = ScriptVersion(
            id=ids.script_version_id(), project_id=project_id, version=1,
            content_snapshot=text, content_hash=_hash(text), created_at=_now(),
        )
        db.add(sv)
    else:
        sv.content_snapshot = text
        sv.content_hash = _hash(text)
        sv.version += 1
        sv.created_at = _now()
    p.current_script_version_id = sv.id
    if p.status == "draft":
        p.status = "active"
    db.commit()
    return {
        "script_version_id": sv.id,
        "version": sv.version,
        "content_hash": sv.content_hash,
        **_pipeline_ctx(db, project_id),
    }


def tool_analyze_script(db: Session, project_id: str, episode_start: int = 1,
                         episode_count: int | None = None,
                         episode_end: int | None = None,
                         session_id: str | None = None) -> dict:
    """分析剧本：创建解析任务（异步），返回任务 ID 与流程状态。"""
    try:
        assert_action_allowed(db, project_id, "analyze_script")
    except PipelineStateError as e:
        raise ToolError("pipeline_gate_blocked", e.message)
    p = db.get(Project, project_id)
    if p is None or p.deleted_at is not None:
        raise ToolError("project_not_found", "项目不存在")
    if p.status == "archived":
        raise ToolError("invalid_state_transition", "已归档项目不可解析")
    sv = (
        db.query(ScriptVersion)
        .filter(ScriptVersion.project_id == project_id)
        .order_by(ScriptVersion.version.desc())
        .first()
    )
    if sv is None:
        raise ToolError("script_not_found", "项目尚无剧本，请先保存剧本")
    if episode_count is not None and episode_end is not None:
        raise ToolError("tool_invalid_args", "episode_count 与 episode_end 只能提供其一")
    if episode_end is not None:
        if episode_end < episode_start:
            raise ToolError("tool_invalid_args", "episode_end 不得小于 episode_start")
        episode_count = episode_end - episode_start + 1
    job = Job(
        id=ids.job_id(), project_id=project_id, job_type="parse",
        params={
            "script_version_id": sv.id,
            "episode_start": episode_start,
            "episode_count": episode_count,
            # 发起方会话：解析为异步任务，执行器完成后据此把结果回推到该对话
            "session_id": session_id,
        },
        status="queued", created_at=_now(),
    )
    db.add(job)
    # 统一任务编排层（Phase 3）：解析任务同步创建统一 Task + parse 阶段（关联 job_id），
    # Job 事件经 bridge_job_event 自动桥接为 task 事件，驱动前端任务卡片动态更新
    try:
        from app.services import task_orchestrator
        _task = task_orchestrator.create_task(
            db, session_id=session_id, task_type="parse", title="剧本解析",
            project_id=project_id,
        )
        task_orchestrator.start_task(db, _task)
        task_orchestrator.add_stage(
            db, _task, stage_key="parse", stage_name="剧本解析", job_id=job.id,
        )
    except Exception:  # noqa: BLE001
        pass  # 编排层失败不影响既有解析链路
    # 同步完成评估标记（与 API 流程的 estimate 步骤对齐）：
    # 未标记时阶段停留在 estimate，保存剧本→分析剧本的流程衔接会断裂
    if not p.estimated_text_hash or p.estimated_text_hash != sv.content_hash:
        p.estimated_at = _now()
        p.estimated_text_hash = sv.content_hash
    if p.status == "draft":
        p.status = "active"
    db.commit()
    return {
        "job_id": job.id,
        "job_status": job.status,
        "script_version_id": sv.id,
        "note": "解析任务已入队，可通过 list_jobs 查询进度",
        **_pipeline_ctx(db, project_id),
    }


def tool_review_assets(db: Session, project_id: str, action: str) -> dict:
    """人工审核资产库（审核闸口）：approve 放行分镜图生成 / reject 驳回返工。"""
    if action not in ("approve", "reject"):
        raise ToolError("tool_invalid_args", "action 必须为 approve 或 reject")
    try:
        assert_action_allowed(db, project_id, "review_assets")
    except PipelineStateError as e:
        raise ToolError("pipeline_gate_blocked", e.message)
    p = db.get(Project, project_id)
    if p is None or p.deleted_at is not None:
        raise ToolError("project_not_found", "项目不存在")
    if action == "approve":
        from app.services.assets_gate import evaluate_assets_gate
        gate = evaluate_assets_gate(db, project_id)
        if not gate.ready and gate.reason_code in ("assets_incomplete", "assets_generating"):
            raise ToolError(gate.reason_code, gate.message)
    p.assets_review_status = "approved" if action == "approve" else "rejected"
    p.assets_reviewed_at = _now()
    db.commit()
    return {
        "project_id": project_id,
        "assets_review_status": p.assets_review_status,
        **_pipeline_ctx(db, project_id),
    }


def tool_confirm_keyframes(db: Session, project_id: str,
                            shot_ids: list[str] | None = None) -> dict:
    """批量确认分镜图（人工审核闸口，D-13）：仅确认已具备当前有效分镜图的镜头。"""
    try:
        assert_action_allowed(db, project_id, "confirm_keyframe")
    except PipelineStateError as e:
        raise ToolError("pipeline_gate_blocked", e.message)
    p = db.get(Project, project_id)
    if p is None or p.deleted_at is not None:
        raise ToolError("project_not_found", "项目不存在")
    from app.services.progress import _shot_has_current_asset
    q = (
        db.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
    )
    if shot_ids:
        q = q.filter(Shot.id.in_(shot_ids))
    confirmed, skipped = [], []
    for s in q.all():
        if _shot_has_current_asset(db, s.id, "keyframe"):
            s.keyframe_confirmed = True
            confirmed.append(s.id)
        else:
            skipped.append(s.id)
    db.commit()
    return {
        "confirmed": confirmed,
        "skipped": skipped,
        "confirmed_count": len(confirmed),
        "skipped_count": len(skipped),
        **_pipeline_ctx(db, project_id),
    }


def tool_compose_video(db: Session, project_id: str, episode: int | None = None,
                       include_subtitle: bool = True, subtitle_mode: str = "burn") -> dict:
    """合成成片（需人工审批）：创建合成任务（异步），返回任务 ID 与流程状态。"""
    try:
        assert_action_allowed(db, project_id, "compose_video")
    except PipelineStateError as e:
        raise ToolError("pipeline_gate_blocked", e.message)
    p = db.get(Project, project_id)
    if p is None or p.deleted_at is not None:
        raise ToolError("project_not_found", "项目不存在")
    if p.status == "archived":
        raise ToolError("invalid_state_transition", "已归档项目不可合成")
    blocking = (
        db.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id, Shot.status == "review")
    )
    if episode is not None:
        blocking = blocking.filter(Shot.episode == episode)
    if blocking.count() > 0:
        raise ToolError(
            "duration_mismatch_review_required",
            "存在未审核放行的超容差镜头，须先审核放行或重生成",
        )
    job = Job(
        id=ids.job_id(), project_id=project_id, job_type="compose",
        params={
            "episode": episode,
            "include_subtitle": include_subtitle,
            "subtitle_mode": subtitle_mode,
        },
        status="queued", created_at=_now(),
    )
    db.add(job)
    # 统一任务编排层（Phase 6）：合成任务同步创建统一 Task + compose 阶段（关联 job_id）
    try:
        from app.services import task_orchestrator
        _task = task_orchestrator.create_task(
            db, session_id=None, task_type='compose', title='成片合成',
            project_id=project_id,
        )
        task_orchestrator.start_task(db, _task)
        task_orchestrator.add_stage(
            db, _task, stage_key='compose', stage_name='成片合成', job_id=job.id,
        )
    except Exception:  # noqa: BLE001
        pass  # 编排层失败不影响既有合成链路
    if p.status == "draft":
        p.status = "active"
    db.commit()
    return {
        "job_id": job.id,
        "job_status": job.status,
        "note": "合成任务已入队，可通过 list_jobs 查询进度",
        **_pipeline_ctx(db, project_id),
    }


def tool_export_video(db: Session, project_id: str, composition_id: str,
                      delivery_profile: str = "standard") -> dict:
    """导出成片（需人工审批）：基于成功的合成版本创建导出任务。"""
    try:
        assert_action_allowed(db, project_id, "export_video")
    except PipelineStateError as e:
        raise ToolError("pipeline_gate_blocked", e.message)
    p = db.get(Project, project_id)
    if p is None or p.deleted_at is not None:
        raise ToolError("project_not_found", "项目不存在")
    if p.status == "archived":
        raise ToolError("invalid_state_transition", "已归档项目不可导出")
    comp = db.get(Composition, composition_id)
    if comp is None or comp.project_id != project_id:
        raise ToolError("composition_required", "不存在成功的合成版本，需先合成")
    if comp.stale:
        raise ToolError("composition_stale", "合成版本已失效，需重新合成后再导出")
    if comp.status != "done":
        raise ToolError("composition_required", "合成版本未完成")
    job = Job(
        id=ids.job_id(), project_id=project_id, job_type="export",
        params={
            "composition_id": comp.id,
            "delivery_profile": delivery_profile,
        },
        status="queued", created_at=_now(),
    )
    db.add(job)
    # 统一任务编排层（Phase 6）：导出任务同步创建统一 Task + export 阶段（关联 job_id）
    try:
        from app.services import task_orchestrator
        _task = task_orchestrator.create_task(
            db, session_id=None, task_type='export', title='成片导出',
            project_id=project_id,
        )
        task_orchestrator.start_task(db, _task)
        task_orchestrator.add_stage(
            db, _task, stage_key='export', stage_name='成片导出', job_id=job.id,
        )
    except Exception:  # noqa: BLE001
        pass  # 编排层失败不影响既有导出链路
    db.commit()
    return {
        "job_id": job.id,
        "job_status": job.status,
        "composition_id": comp.id,
        "note": "导出任务已入队，可通过 list_jobs 查询进度",
        **_pipeline_ctx(db, project_id),
    }


def tool_get_pipeline_status(db: Session, project_id: str) -> dict:
    """查询项目当前流程节点与状态（只读）：节点、阶段、审核闸口、下一步指引。"""
    try:
        st = get_pipeline_status(db, project_id)
    except PipelineStateError as e:
        return {"ok": False, "error_message": e.message}
    return {
        "ok": True,
        "project_id": st.project_id,
        "pipeline_node": st.node_key,
        "pipeline_node_name": st.node_name,
        "pipeline_node_description": st.node_description,
        "stage": st.stage,
        "review_gate": st.review_gate,
        "next_hint": st.next_hint,
        "shot_total": st.shot_total,
        "shot_done": st.shot_done,
        "extra": st.extra,
    }


# --- 工具注册表（Schema 化：与 agent_tools.TOOL_REGISTRY 同构） ---

PIPELINE_TOOL_REGISTRY: dict = {
    "create_project": {
        "description": "新建制作项目（流程起点）：设定名称、画幅（16:9/9:16/1:1）、创作类型",
        "permission": "write",
        "risk_level": "low",
        "side_effect": True,
        "idempotent": False,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": CreateProjectInput,
        "params": {
            "name": "项目名称（1-100 字）",
            "aspect_ratio": "画幅：16:9 / 9:16 / 1:1",
            "creation_type": "创作类型：novel/script（默认 novel）",
            "description": "可选：项目描述（500 字内）",
        },
        "fn": tool_create_project,
    },
    "save_script": {
        "description": "保存剧本（单版本覆盖，版本号递增）；须已创建项目",
        "permission": "write",
        "risk_level": "medium",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": SaveScriptInput,
        "params": {"project_id": "项目 ID", "text": "剧本文本（1-50000 字）"},
        "fn": tool_save_script,
    },
    "analyze_script": {
        "description": "分析剧本（创建解析任务，异步）：解析场景、镜头与资产定义；须已保存剧本",
        "permission": "write",
        "risk_level": "medium",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": AnalyzeScriptInput,
        "params": {
            "project_id": "项目 ID",
            "episode_start": "起始集数（默认 1）",
            "episode_count": "可选：解析集数（与 episode_end 二选一）",
            "episode_end": "可选：结束集数（与 episode_count 二选一）",
        },
        "fn": tool_analyze_script,
    },
    "review_assets": {
        "description": "人工审核资产库（审核闸口）：approve=审核通过放行分镜图 / reject=驳回返工；须公共资产已生成",
        "permission": "write",
        "risk_level": "medium",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ReviewAssetsInput,
        "params": {"project_id": "项目 ID", "action": "approve / reject"},
        "fn": tool_review_assets,
    },
    "confirm_keyframes": {
        "description": "批量确认分镜图（人工审核闸口）：仅确认已具备当前有效分镜图的镜头，返回确认/跳过明细",
        "permission": "write",
        "risk_level": "medium",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ConfirmKeyframesInput,
        "params": {
            "project_id": "项目 ID",
            "shot_ids": "可选：镜头 ID 列表（缺省为全部已生成分镜图的镜头）",
        },
        "fn": tool_confirm_keyframes,
    },
    "compose_video": {
        "description": "合成成片（需人工审批）：创建合成任务；须分镜视频与音频就绪且无未放行的超容差镜头",
        "permission": "write",
        "risk_level": "high",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": True,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ComposeVideoInput,
        "params": {
            "project_id": "项目 ID",
            "episode": "可选：指定集数（缺省全部）",
            "include_subtitle": "是否包含字幕（默认 true）",
            "subtitle_mode": "字幕模式：burn（内嵌）/ sidecar（外挂），默认 burn",
        },
        "fn": tool_compose_video,
    },
    "export_video": {
        "description": "导出成片（需人工审批）：基于成功的合成版本创建导出任务；须合成已完成",
        "permission": "write",
        "risk_level": "high",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": True,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ExportVideoInput,
        "params": {
            "project_id": "项目 ID",
            "composition_id": "合成版本 ID（来自合成任务完成后的 compositions 查询）",
            "delivery_profile": "交付档位：standard / high_quality（默认 standard）",
        },
        "fn": tool_export_video,
    },
    "confirm_parse": {
        "description": "确认剧本解析结果（人工审核闸口）：把候选解析版本落库为角色/场景/道具/镜头并切换为当前版本；须已完成剧本分析",
        "permission": "write",
        "risk_level": "medium",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 60,
        "max_retries": 0,
        "input_model": ConfirmParseInput,
        "params": {
            "project_id": "项目 ID",
            "parse_version_id": "可选：解析版本 ID（缺省为最新候选版本）",
        },
        "fn": tool_confirm_parse,
    },
    "query_parse_status": {
        "description": "查询剧本分析进度（只读）：任务状态、已用时、解析版本与结果摘要；用于分析中询问进度或确认是否已完成",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": QueryParseStatusInput,
        "params": {"project_id": "项目 ID"},
        "fn": tool_query_parse_status,
    },
    "get_pipeline_status": {
        "description": "查询项目当前流程节点与状态（只读）：节点、制作阶段、审核闸口、镜头进度、下一步指引",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": PipelineStatusInput,
        "params": {"project_id": "项目 ID"},
        "fn": tool_get_pipeline_status,
    },
}


def execute_pipeline_tool(db: Session, tool_name: str, *, approved: bool = False, **kwargs) -> ToolResult:
    """执行流程工具：审批门禁 → Schema 校验 → 状态机门禁（工具内）→ 执行 → 结构化结果。

    与 agent_tools.execute_tool 同构：
    - requires_approval=True 且 approved=False：返回 approval_required，不执行、无副作用
    - 状态机门禁阻断返回 pipeline_gate_blocked（含当前节点与所需节点）

    形参命名注意：工具名用 tool_name 而非 name，因为 create_project 的业务参数本身
    就叫 name（项目名称），形参同名会让 **kwargs 展开时抛
    TypeError: got multiple values for argument 'name'。
    """
    import time as _time
    from pydantic import ValidationError
    start = _time.monotonic()
    spec = PIPELINE_TOOL_REGISTRY.get(tool_name)
    if not spec:
        return ToolResult(ok=False, error_code="tool_not_found", error_message=f"未注册的工具: {tool_name}")
    if spec.get("requires_approval") and not approved:
        return ToolResult(
            ok=False,
            error_code="approval_required",
            error_message=f"工具 {tool_name} 需要人工审批后执行",
            data={"tool": tool_name, "params": kwargs, "permission": spec.get("permission")},
        )
    input_model = spec.get("input_model")
    try:
        if input_model is not None:
            validated = input_model.model_validate(kwargs)
            data = spec["fn"](db, **validated.model_dump())
        else:
            data = spec["fn"](db, **kwargs)
        return ToolResult(ok=True, data=data, duration_ms=int((_time.monotonic() - start) * 1000))
    except ToolError as e:
        return ToolResult(ok=False, error_code=e.code, error_message=e.message,
                          duration_ms=int((_time.monotonic() - start) * 1000))
    except ValidationError as e:
        return ToolResult(ok=False, error_code="tool_invalid_args", error_message=f"工具参数错误: {e}",
                          duration_ms=int((_time.monotonic() - start) * 1000))
    except Exception:
        return ToolResult(ok=False, error_code="tool_internal_error", error_message="工具执行失败",
                          duration_ms=int((_time.monotonic() - start) * 1000))


def pipeline_tool_specs() -> list[dict]:
    """流程工具元数据清单（供意图路由与文档展示）。"""
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
        for name, spec in PIPELINE_TOOL_REGISTRY.items()
    ]
