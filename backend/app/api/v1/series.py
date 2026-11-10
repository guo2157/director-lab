"""系列化创作与续集管理接口。

约定（对齐 API.md）：
- 统一错误格式由全局异常处理器输出 {"detail": {"code", "message"}}
- 长耗时续写返回 202 + task/job 标识，不同步等待剧本生成
- 只做参数与归属校验；正式剧情状态只能由 StateCommitService 写入
- 权限说明：本系统为单用户本地工具，无多用户鉴权；此处执行的是
  「系列—剧集—项目」归属一致性校验，不冒充多租户权限模型
"""
from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Asset, PlotThread, Series, SeriesEpisode, SeriesScriptVersion, StoryEvent
from app.services import errors
from app.services.series import (
    asset_registry_service,
    bible_service,
    impact_analysis_service,
    context_builder_service,
    continue_service,
    episode_service,
    narrative_continuity_service,
    plot_thread_service,
    series_service,
    state_commit_service,
)
from app.schemas.series import (
    BibleOut,
    BibleSaveIn,
    ContextManifestOut,
    ContinueIn,
    ContinueOut,
    ContinuityOverviewOut,
    ContinuitySnapshotOut,
    EpisodeAssetRefOut,
    EpisodeCreate,
    EpisodeOut,
    EpisodePageOut,
    PlotThreadCreate,
    PlotThreadOut,
    PlotThreadUpdate,
    ScriptConfirmIn,
    ScriptConfirmOut,
    ScriptOut,
    ScriptSaveIn,
    SeriesAssetOut,
    SeriesCreate,
    SeriesListOut,
    SeriesOut,
    SeriesPageOut,
    SeriesUpdate,
    StoryEventOut,
)

router = APIRouter(prefix="/api", tags=["series"])


def _episode_in_series(db: Session, series_id: str, episode_id: str) -> SeriesEpisode:
    """归属校验：剧集必须属于该系列，防止跨系列误操作。"""
    series_service.get_series(db, series_id)
    ep = episode_service.get_episode(db, episode_id)
    if ep.series_id != series_id:
        raise errors.series_episode_not_found()
    return ep


def _script_of_episode(db: Session, episode_id: str, script_id: str) -> SeriesScriptVersion:
    row = db.get(SeriesScriptVersion, script_id)
    if row is None or row.episode_id != episode_id:
        raise errors.series_script_not_found()
    return row


# ---------------- Series ----------------


@router.post("/series", status_code=status.HTTP_201_CREATED)
def create_series(payload: SeriesCreate, db: Session = Depends(get_db)) -> SeriesOut:
    series = series_service.create_series(db, payload)
    return SeriesOut.model_validate(series)


@router.get("/series")
def list_series(
    keyword: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None, max_length=16),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> SeriesPageOut:
    rows, total = series_service.list_series(
        db, keyword=keyword, status=status, page=page, page_size=page_size
    )
    items: list[SeriesListOut] = []
    for row in rows:
        item = SeriesListOut.model_validate(row["series"])
        for key, value in row["stats"].items():
            setattr(item, key, value)
        items.append(item)
    return SeriesPageOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/series/{series_id}")
def get_series(series_id: str, db: Session = Depends(get_db)) -> SeriesListOut:
    series = series_service.get_series(db, series_id)
    item = SeriesListOut.model_validate(series)
    for key, value in series_service.series_stats(db, series_id).items():
        setattr(item, key, value)
    return item


@router.patch("/series/{series_id}")
def update_series(
    series_id: str, payload: SeriesUpdate, db: Session = Depends(get_db)
) -> SeriesOut:
    return SeriesOut.model_validate(series_service.update_series(db, series_id, payload))


@router.post("/series/{series_id}/archive")
def archive_series(series_id: str, db: Session = Depends(get_db)) -> SeriesOut:
    """归档：不物理删除任何历史资产与生产记录。"""
    return SeriesOut.model_validate(series_service.archive_series(db, series_id))


@router.post("/series/{series_id}/unarchive")
def unarchive_series(series_id: str, db: Session = Depends(get_db)) -> SeriesOut:
    return SeriesOut.model_validate(series_service.unarchive_series(db, series_id))


# ---------------- Bible ----------------


@router.get("/series/{series_id}/bible")
def get_active_bible(series_id: str, db: Session = Depends(get_db)) -> BibleOut:
    series_service.get_series(db, series_id)
    row = bible_service.list_bible_versions(db, series_id)
    active = next((r for r in row if r.is_active), None)
    if active is None:
        raise errors.series_bible_not_found()
    return BibleOut.model_validate(active)


@router.put("/series/{series_id}/bible", status_code=status.HTTP_201_CREATED)
def save_bible(
    series_id: str, payload: BibleSaveIn, db: Session = Depends(get_db)
) -> BibleOut:
    """保存为**新版本**，不覆盖历史版本。"""
    row = bible_service.save_bible_version(
        db,
        series_id,
        content=payload.content,
        change_summary=payload.change_summary,
        created_by="user",
    )
    return BibleOut.model_validate(row)


@router.get("/series/{series_id}/bible/versions")
def list_bible_versions(series_id: str, db: Session = Depends(get_db)) -> list[BibleOut]:
    rows = bible_service.list_bible_versions(db, series_id)
    return [BibleOut.model_validate(r) for r in rows]


# ---------------- Episodes ----------------


@router.post("/series/{series_id}/episodes", status_code=status.HTTP_201_CREATED)
def create_episode(
    series_id: str, payload: EpisodeCreate, db: Session = Depends(get_db)
) -> EpisodeOut:
    series_service.get_active_series(db, series_id)
    ep = episode_service.create_episode(db, series_id, payload)
    return EpisodeOut.model_validate(ep)


@router.get("/series/{series_id}/episodes")
def list_episodes(
    series_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
) -> EpisodePageOut:
    rows, total = episode_service.list_episodes(
        db, series_id, page=page, page_size=page_size
    )
    return EpisodePageOut(
        items=[EpisodeOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/episodes/{episode_id}")
def get_episode(episode_id: str, db: Session = Depends(get_db)) -> EpisodeOut:
    return EpisodeOut.model_validate(episode_service.get_episode(db, episode_id))


@router.get("/series/{series_id}/next-episode-number")
def get_next_episode_number(series_id: str, db: Session = Depends(get_db)) -> dict:
    series_service.get_series(db, series_id)
    return {"next_episode_number": episode_service.next_episode_number(db, series_id)}


@router.get("/episodes/{episode_id}/workspace")
def get_episode_workspace(episode_id: str, db: Session = Depends(get_db)) -> dict:
    """剧集工作区：返回绑定 Project 与生产进度入口，生产流程本身复用现有单集接口。"""
    ep = episode_service.get_episode(db, episode_id)
    return {
        "episode_id": ep.id,
        "series_id": ep.series_id,
        "episode_number": ep.episode_number,
        "project_id": ep.project_id,
        "project_episode_no": ep.project_episode_no,
        "script_status": ep.script_status,
        "production_status": ep.production_status,
        "continuity_status": ep.continuity_status,
        "project_api": "/api/projects/{project_id}" if ep.project_id else None,
    }


# ---------------- Continue (异步续写) ----------------


@router.post(
    "/series/{series_id}/episodes/continue", status_code=status.HTTP_202_ACCEPTED
)
def continue_episode(
    series_id: str, payload: ContinueIn, db: Session = Depends(get_db)
) -> ContinueOut:
    """创建续写任务：只返回任务标识，不在本请求内等待剧本生成。"""
    series = series_service.get_active_series(db, series_id)
    target_number = payload.target_episode_number or episode_service.next_episode_number(
        db, series_id
    )
    episode = (
        db.query(SeriesEpisode)
        .filter(
            SeriesEpisode.series_id == series_id,
            SeriesEpisode.episode_number == target_number,
        )
        .first()
    )
    if episode is None:
        episode = episode_service.create_episode(
            db,
            series_id,
            EpisodeCreate(
                episode_number=target_number, title=payload.title, synopsis=None
            ),
        )
    elif payload.title:
        episode.title = payload.title[:200]
        db.commit()

    task_id, _job_id = continue_service.create_continue_task(
        db,
        series=series,
        episode=episode,
        user_instruction=payload.user_instruction,
        direction=payload.direction,
        constraints=payload.constraints.model_dump(),
        title=payload.title,
    )
    return ContinueOut(
        episode_id=episode.id,
        episode_number=episode.episode_number,
        task_id=task_id,
        status="context_building",
    )


# ---------------- Context ----------------


@router.get("/series/{series_id}/context-preview")
def preview_context(
    series_id: str,
    target_episode_number: int = Query(ge=1),
    user_instruction: str = Query(default="", max_length=4000),
    db: Session = Depends(get_db),
) -> dict:
    """上下文预览：与正式生成使用同一构建规则，不落库。"""
    series = series_service.get_series(db, series_id)
    return context_builder_service.build_context(
        db,
        series=series,
        target_episode_number=target_episode_number,
        user_instruction=user_instruction,
        constraints=None,
    )


@router.get("/episodes/{episode_id}/context-manifest")
def get_context_manifest(
    episode_id: str, db: Session = Depends(get_db)
) -> ContextManifestOut:
    row = context_builder_service.latest_manifest(db, episode_id)
    if row is None:
        raise errors.biz_error(
            code="context_manifest_not_found", http=404, message="该剧集尚无上下文清单"
        )
    return ContextManifestOut.model_validate(row)


# ---------------- Script versions ----------------


@router.get("/episodes/{episode_id}/scripts")
def list_scripts(episode_id: str, db: Session = Depends(get_db)) -> list[ScriptOut]:
    episode_service.get_episode(db, episode_id)
    rows = (
        db.query(SeriesScriptVersion)
        .filter(SeriesScriptVersion.episode_id == episode_id)
        .order_by(SeriesScriptVersion.version.desc())
        .all()
    )
    return [ScriptOut.model_validate(r) for r in rows]


@router.post("/episodes/{episode_id}/scripts", status_code=status.HTTP_201_CREATED)
def save_script(
    episode_id: str, payload: ScriptSaveIn, db: Session = Depends(get_db)
) -> ScriptOut:
    """保存草稿：始终创建新版本，不覆盖已确认版本。"""
    episode = episode_service.get_episode(db, episode_id)
    from sqlalchemy import func

    latest = (
        db.query(func.max(SeriesScriptVersion.version))
        .filter(SeriesScriptVersion.episode_id == episode_id)
        .scalar()
    )
    from datetime import datetime

    from app.models import ids

    row = SeriesScriptVersion(
        id=ids.series_script_version_id(),
        episode_id=episode_id,
        version=int(latest or 0) + 1,
        content=payload.content,
        synopsis=payload.synopsis,
        source_context_manifest_id=payload.source_context_manifest_id,
        prompt_template_version=payload.prompt_template_version,
        model=payload.model,
        validation_result=None,
        continuity_result=None,
        status="draft",
        created_at=datetime.now().replace(microsecond=0),
        created_by="user",
    )
    db.add(row)
    db.flush()
    episode.script_status = "draft"
    episode.script_current_version_id = row.id
    db.commit()
    db.refresh(row)
    return ScriptOut.model_validate(row)


@router.post("/episodes/{episode_id}/scripts/{script_id}/confirm")
def confirm_script(
    episode_id: str,
    script_id: str,
    payload: ScriptConfirmIn,
    db: Session = Depends(get_db),
) -> ScriptConfirmOut:
    """确认剧本：写入正式剧情事件并生成新快照（单事务 + 乐观锁 + 幂等键）。"""
    script = _script_of_episode(db, episode_id, script_id)
    episode = episode_service.get_episode(db, episode_id)
    series = series_service.get_series(db, episode.series_id)

    validation = dict(script.validation_result or {})
    events = validation.get("events") or []
    proposal = {"events": events}

    result = state_commit_service.confirm_script(
        db,
        series=series,
        episode=episode,
        script_version=script,
        expected_state_version=payload.expected_series_state_version,
        proposal=proposal,
        review_note=payload.review_note,
        created_by="user",
        ignore_warnings=payload.ignore_warnings,
    )
    return ScriptConfirmOut(**result)


@router.post(
    "/episodes/{episode_id}/scripts/{script_id}/regenerate",
    status_code=status.HTTP_202_ACCEPTED,
)
def regenerate_script(
    episode_id: str,
    script_id: str,
    payload: ContinueIn,
    db: Session = Depends(get_db),
) -> ContinueOut:
    """重新生成：创建新版本，绝不覆盖原始确认版本。"""
    script = _script_of_episode(db, episode_id, script_id)
    if script.status == "confirmed":
        raise errors.series_script_status_conflict(
            "已确认版本不可覆盖，请新建草稿版本后再重新生成"
        )
    episode = episode_service.get_episode(db, episode_id)
    series = series_service.get_active_series(db, episode.series_id)
    task_id, _job_id = continue_service.create_continue_task(
        db,
        series=series,
        episode=episode,
        user_instruction=payload.user_instruction,
        direction=payload.direction,
        constraints=payload.constraints.model_dump(),
        title=payload.title,
    )
    return ContinueOut(
        episode_id=episode.id,
        episode_number=episode.episode_number,
        task_id=task_id,
        status="context_building",
    )


# ---------------- Continuity ----------------


@router.get("/series/{series_id}/story-events")
def list_story_events(
    series_id: str,
    episode_id: str | None = Query(default=None),
    fact_status: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[StoryEventOut]:
    series_service.get_series(db, series_id)
    q = db.query(StoryEvent).filter(StoryEvent.series_id == series_id)
    if episode_id:
        q = q.filter(StoryEvent.episode_id == episode_id)
    if fact_status:
        q = q.filter(StoryEvent.fact_status == fact_status)
    rows = q.order_by(StoryEvent.occurred_order.asc(), StoryEvent.created_at.asc()).limit(limit).all()
    return [StoryEventOut.model_validate(r) for r in rows]


@router.get("/series/{series_id}/continuity")
def get_continuity(
    series_id: str, db: Session = Depends(get_db)
) -> ContinuityOverviewOut:
    series = series_service.get_series(db, series_id)
    from app.models import ContinuitySnapshot

    latest = (
        db.query(ContinuitySnapshot)
        .filter(ContinuitySnapshot.series_id == series_id)
        .order_by(ContinuitySnapshot.version.desc())
        .first()
    )
    open_threads = (
        db.query(PlotThread)
        .filter(
            PlotThread.series_id == series_id,
            PlotThread.status.in_(("open", "in_progress")),
        )
        .count()
    )
    conflict_episodes = (
        db.query(SeriesEpisode)
        .filter(
            SeriesEpisode.series_id == series_id,
            SeriesEpisode.continuity_status == "conflict",
        )
        .count()
    )
    return ContinuityOverviewOut(
        series_id=series.id,
        state_version=series.state_version,
        latest_snapshot=(
            ContinuitySnapshotOut.model_validate(latest) if latest is not None else None
        ),
        open_plot_threads=open_threads,
        confirmed_event_count=series_service.confirmed_event_count(db, series_id),
        episodes_with_conflict=conflict_episodes,
    )


@router.get("/series/{series_id}/continuity/snapshots")
def list_snapshots(
    series_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[ContinuitySnapshotOut]:
    series_service.get_series(db, series_id)
    from app.models import ContinuitySnapshot

    rows = (
        db.query(ContinuitySnapshot)
        .filter(ContinuitySnapshot.series_id == series_id)
        .order_by(ContinuitySnapshot.version.desc())
        .limit(limit)
        .all()
    )
    return [ContinuitySnapshotOut.model_validate(r) for r in rows]


@router.post("/episodes/{episode_id}/continuity/check")
def run_continuity_check(episode_id: str, db: Session = Depends(get_db)) -> dict:
    episode = episode_service.get_episode(db, episode_id)
    series = series_service.get_series(db, episode.series_id)
    latest = (
        db.query(SeriesScriptVersion)
        .filter(SeriesScriptVersion.episode_id == episode_id)
        .order_by(SeriesScriptVersion.version.desc())
        .first()
    )
    proposal = None
    if latest is not None:
        proposal = {"events": (latest.validation_result or {}).get("events") or []}
    result = narrative_continuity_service.check_script_version(
        db, series=series, episode=episode, proposal=proposal
    )
    episode.continuity_status = result["status"]
    db.commit()
    return result


@router.get("/episodes/{episode_id}/continuity/issues")
def list_continuity_issues(episode_id: str, db: Session = Depends(get_db)) -> dict:
    episode_service.get_episode(db, episode_id)
    latest = (
        db.query(SeriesScriptVersion)
        .filter(SeriesScriptVersion.episode_id == episode_id)
        .order_by(SeriesScriptVersion.version.desc())
        .first()
    )
    if latest is None or latest.continuity_result is None:
        return {"status": "not_checked", "issues": [], "script_version_id": None}
    return {
        "status": latest.continuity_result.get("status"),
        "issues": latest.continuity_result.get("issues") or [],
        "script_version_id": latest.id,
    }


# ---------------- Plot threads ----------------


@router.get("/series/{series_id}/plot-threads")
def list_plot_threads(
    series_id: str,
    status_filter: str | None = Query(default=None, alias="status", max_length=16),
    db: Session = Depends(get_db),
) -> list[PlotThreadOut]:
    rows = plot_thread_service.list_threads(db, series_id, status=status_filter)
    return [PlotThreadOut.model_validate(r) for r in rows]


@router.post("/series/{series_id}/plot-threads", status_code=status.HTTP_201_CREATED)
def create_plot_thread(
    series_id: str, payload: PlotThreadCreate, db: Session = Depends(get_db)
) -> PlotThreadOut:
    series_service.get_active_series(db, series_id)
    return PlotThreadOut.model_validate(
        plot_thread_service.create_thread(db, series_id, payload)
    )


@router.patch("/plot-threads/{thread_id}")
def update_plot_thread(
    thread_id: str, payload: PlotThreadUpdate, db: Session = Depends(get_db)
) -> PlotThreadOut:
    return PlotThreadOut.model_validate(
        plot_thread_service.update_thread(db, thread_id, payload)
    )


# ---------------- Shared assets ----------------


@router.get("/series/{series_id}/assets")
def list_series_assets(
    series_id: str,
    asset_type: str | None = Query(default=None, max_length=16),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = asset_registry_service.list_series_assets(
        db, series_id, asset_type=asset_type
    )
    return [
        {
            "id": link.id,
            "series_id": link.series_id,
            "asset_id": asset.id,
            "asset_name": asset.name,
            "asset_type": asset.asset_type,
            "current_version": asset.version,
            "file_path": asset.file_path,
            "thumb_path": asset.thumb_path,
            "first_episode_id": link.first_episode_id,
            "created_at": link.created_at,
        }
        for link, asset in rows
    ]


@router.post("/series/{series_id}/assets")
def register_series_asset(
    series_id: str,
    asset_id: str = Query(min_length=1, max_length=32),
    first_episode_id: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_db),
) -> SeriesAssetOut:
    """把既有资产登记为系列共享资产：不复制记录，不改资产归属。"""
    return SeriesAssetOut.model_validate(
        asset_registry_service.register_series_asset(
            db, series_id=series_id, asset_id=asset_id, first_episode_id=first_episode_id
        )
    )


@router.get("/episodes/{episode_id}/assets")
def list_episode_assets(episode_id: str, db: Session = Depends(get_db)) -> list[dict]:
    episode_service.get_episode(db, episode_id)
    return asset_registry_service.list_episode_assets(db, episode_id)


@router.post("/episodes/{episode_id}/assets/resolve")
def resolve_episode_assets(
    episode_id: str, db: Session = Depends(get_db)
) -> dict:
    """资产匹配建议：按名称给出候选并明确标注需人工确认，不自动合并。"""
    episode = episode_service.get_episode(db, episode_id)
    latest = (
        db.query(SeriesScriptVersion)
        .filter(SeriesScriptVersion.episode_id == episode_id)
        .order_by(SeriesScriptVersion.version.desc())
        .first()
    )
    names: list[str] = []
    if latest is not None:
        validation = latest.validation_result or {}
        for key in ("characters", "scenes", "props"):
            for name in validation.get(key) or []:
                if name not in names:
                    names.append(str(name))
    candidates: dict[str, list] = {}
    for name in names[:60]:
        candidates[name] = asset_registry_service.find_name_candidates(
            db, series_id=episode.series_id, name=name
        )
    return {
        "episode_id": episode.id,
        "script_version_id": latest.id if latest else None,
        "candidates": candidates,
        "note": "名称匹配仅为提示，同一角色必须由人工确认 Asset ID，禁止仅凭名称合并",
    }


@router.get("/series/{series_id}/assets/{asset_id}/usage")
def get_asset_usage(
    series_id: str, asset_id: str, db: Session = Depends(get_db)
) -> dict:
    series_service.get_series(db, series_id)
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise errors.biz_error(code="asset_not_found", http=404, message="资产不存在")
    return {
        "asset_id": asset_id,
        "asset_type": asset.asset_type,
        "current_version": asset.version,
        "usage": asset_registry_service.usage_history(
            db, series_id=series_id, asset_id=asset_id
        ),
    }


@router.get("/episodes/{episode_id}/scripts/{script_id}/impact")
def analyze_script_impact(
    episode_id: str, script_id: str, db: Session = Depends(get_db)
) -> dict:
    """历史剧本修改影响分析（只读）。

    对比指定剧本版本与该集已确认版本，推算对后续已确认剧集的影响。
    只读服务：不写事件、不写快照、不改任何正式剧情状态。
    """
    script = _script_of_episode(db, episode_id, script_id)
    episode = episode_service.get_episode(db, episode_id)
    series = series_service.get_series(db, episode.series_id)
    return impact_analysis_service.analyze_script_change(
        db, series=series, episode=episode, script_version=script
    )
