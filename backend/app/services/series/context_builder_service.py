"""上下文构建服务（Context Builder）：续集系统最重要的后端模块。

设计原则（对应需求第 9 节）：
1. 本服务是确定性的：加载哪些数据由固定规则决定，不由 LLM 自行决定。
2. 必须项缺失时直接失败，不使用空上下文假装成功。
3. 预览与正式生成走同一套规则，避免两者不一致。
4. 每次构建都落库 Manifest，保存实际使用的上下文内容与版本引用，不只存指针。
5. 不把全部历史剧本塞进 Prompt：仅加载上一集确认剧本的摘要与关键事件。

优先级（冲突时按此顺序）：
用户本次要求 > 已确认 Bible > 已确认剧情事件 > 当前连续性状态 > 上一集确认剧本 > 其他历史事件 > AI 推测
"""
import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    ContinuitySnapshot,
    EpisodeAssetRef,
    EpisodeContextManifest,
    PlotThread,
    Series,
    SeriesBibleVersion,
    SeriesEpisode,
    SeriesScriptVersion,
    StoryEvent,
)
from app.models import ids
from app.services import errors

from .episode_service import get_episode, previous_confirmed_episode, previous_episode

PROMPT_TEMPLATE_VERSION = "episode_continue_v1"
MAX_HISTORY_EVENTS = 40
OPEN_THREAD_STATUS = ("open", "in_progress")


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _latest_snapshot(db: Session, series_id: str) -> ContinuitySnapshot | None:
    return (
        db.query(ContinuitySnapshot)
        .filter(ContinuitySnapshot.series_id == series_id)
        .order_by(ContinuitySnapshot.version.desc())
        .first()
    )


def _active_bible(db: Session, series_id: str) -> SeriesBibleVersion | None:
    return (
        db.query(SeriesBibleVersion)
        .filter(
            SeriesBibleVersion.series_id == series_id,
            SeriesBibleVersion.is_active.is_(True),
        )
        .order_by(SeriesBibleVersion.version.desc())
        .first()
    )


def _confirmed_script(db: Session, episode_id: str) -> SeriesScriptVersion | None:
    ep = db.get(SeriesEpisode, episode_id)
    if ep is None:
        return None
    if ep.confirmed_script_version_id:
        row = db.get(SeriesScriptVersion, ep.confirmed_script_version_id)
        if row is not None:
            return row
    return (
        db.query(SeriesScriptVersion)
        .filter(
            SeriesScriptVersion.episode_id == episode_id,
            SeriesScriptVersion.status == "confirmed",
        )
        .order_by(SeriesScriptVersion.version.desc())
        .first()
    )


def _episode_events(db: Session, episode_id: str) -> list[StoryEvent]:
    return (
        db.query(StoryEvent)
        .filter(
            StoryEvent.episode_id == episode_id,
            StoryEvent.fact_status == "confirmed",
        )
        .order_by(StoryEvent.occurred_order.asc(), StoryEvent.created_at.asc())
        .all()
    )


def _history_events(db: Session, series_id: str, before_number: int) -> list[StoryEvent]:
    rows = (
        db.query(StoryEvent)
        .join(SeriesEpisode, StoryEvent.episode_id == SeriesEpisode.id)
        .filter(
            StoryEvent.series_id == series_id,
            StoryEvent.fact_status == "confirmed",
            SeriesEpisode.episode_number < before_number,
        )
        .order_by(StoryEvent.importance.desc(), SeriesEpisode.episode_number.desc())
        .limit(MAX_HISTORY_EVENTS)
        .all()
    )
    return list(reversed(rows))


def _open_threads(db: Session, series_id: str) -> list[PlotThread]:
    return (
        db.query(PlotThread)
        .filter(
            PlotThread.series_id == series_id,
            PlotThread.status.in_(OPEN_THREAD_STATUS),
        )
        .order_by(PlotThread.created_at.asc())
        .all()
    )


def _series_assets(db: Session, series_id: str) -> list[EpisodeAssetRef]:
    rows = (
        db.query(EpisodeAssetRef)
        .join(SeriesEpisode, EpisodeAssetRef.episode_id == SeriesEpisode.id)
        .filter(SeriesEpisode.series_id == series_id)
        .order_by(EpisodeAssetRef.created_at.desc())
        .limit(200)
        .all()
    )
    seen: dict[str, EpisodeAssetRef] = {}
    for row in rows:
        key = f"{row.asset_id}:{row.usage_type}"
        if key not in seen:
            seen[key] = row
    return list(seen.values())


def _event_view(ev: StoryEvent) -> dict[str, Any]:
    return {
        "id": ev.id,
        "episode_id": ev.episode_id,
        "script_version_id": ev.script_version_id,
        "event_type": ev.event_type,
        "title": ev.title,
        "description": ev.description,
        "fact_status": ev.fact_status,
        "importance": ev.importance,
        "involved_asset_ids": ev.involved_asset_ids or [],
        "location_asset_id": ev.location_asset_id,
    }


def build_context(
    db: Session,
    *,
    series: Series,
    target_episode_number: int,
    user_instruction: str,
    constraints: dict | None = None,
) -> dict[str, Any]:
    """构建上下文包；预览与正式生成共用此函数。"""
    warnings: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    bible = _active_bible(db, series.id)
    if bible is None:
        raise errors.series_context_unavailable(
            "系列设定缺失：当前系列没有激活的 Series Bible 版本，无法构建续写上下文"
        )

    snapshot = _latest_snapshot(db, series.id)
    if snapshot is None:
        warnings.append(
            {
                "code": "continuity_snapshot_missing",
                "message": "尚无正式连续性快照，将仅依据 Series Bible 与用户要求创作",
            }
        )

    prev = previous_episode(db, series.id, target_episode_number)
    prev_confirmed = previous_confirmed_episode(db, series.id, target_episode_number)
    if prev is not None and prev_confirmed is None:
        missing.append(
            {
                "code": "previous_episode_not_confirmed",
                "episode_id": prev.id,
                "episode_number": prev.episode_number,
                "message": f"第 {prev.episode_number} 集剧本尚未确认，草稿不能作为正式剧情依据",
            }
        )

    prev_script: SeriesScriptVersion | None = None
    prev_events: list[StoryEvent] = []
    if prev_confirmed is not None:
        prev_script = _confirmed_script(db, prev_confirmed.id)
        prev_events = _episode_events(db, prev_confirmed.id)
        if prev_script is None:
            missing.append(
                {
                    "code": "previous_script_missing",
                    "episode_id": prev_confirmed.id,
                    "message": "上一集标记为已确认，但找不到对应的确认剧本版本",
                }
            )

    threads = _open_threads(db, series.id)
    history_events = _history_events(db, series.id, target_episode_number)
    assets = _series_assets(db, series.id)

    context: dict[str, Any] = {
        "series": {
            "id": series.id,
            "name": series.name,
            "genre": series.genre,
            "style": series.style,
            "target_audience": series.target_audience,
            "language": series.language,
            "mainline_id": series.mainline_id,
            "state_version": series.state_version,
        },
        "target_episode_number": target_episode_number,
        "series_bible": {"version": bible.version, "content": bible.content or {}},
        "continuity": {
            "snapshot_version": snapshot.version if snapshot else None,
            "snapshot_id": snapshot.id if snapshot else None,
            "state": (snapshot.state_json if snapshot else None),
        },
        "previous_episode": (
            {
                "episode_id": prev_confirmed.id,
                "episode_number": prev_confirmed.episode_number,
                "title": prev_confirmed.title,
                "script_version_id": prev_script.id if prev_script else None,
                "script_version": prev_script.version if prev_script else None,
                "synopsis": (prev_script.synopsis if prev_script else None),
                "ending_events": [_event_view(e) for e in prev_events],
            }
            if prev_confirmed is not None
            else None
        ),
        "plot_threads": [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "status": t.status,
                "introduced_episode_id": t.introduced_episode_id,
                "resolution_condition": t.resolution_condition,
            }
            for t in threads
        ],
        "history_events": [_event_view(e) for e in history_events],
        "assets": [
            {
                "asset_id": a.asset_id,
                "asset_version": a.asset_version,
                "usage_type": a.usage_type,
                "is_required": a.is_required,
            }
            for a in assets
        ],
        "user_instruction": user_instruction,
        "constraints": constraints or {},
        "warnings": warnings,
        "missing": missing,
    }

    context["context_hash"] = _hash(
        {
            "bible_version": bible.version,
            "snapshot_version": snapshot.version if snapshot else None,
            "previous_script_version_id": prev_script.id if prev_script else None,
            "plot_thread_ids": [t.id for t in threads],
            "event_ids": [e.id for e in history_events] + [e.id for e in prev_events],
            "asset_refs": [f"{a.asset_id}@{a.asset_version}" for a in assets],
            "user_instruction": user_instruction,
            "constraints": constraints or {},
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        }
    )
    return context


def persist_manifest(
    db: Session,
    *,
    series_id: str,
    episode: SeriesEpisode,
    manifest: dict[str, Any],
    model: str | None = None,
) -> EpisodeContextManifest:
    latest = (
        db.query(func.max(EpisodeContextManifest.context_version))
        .filter(EpisodeContextManifest.episode_id == episode.id)
        .scalar()
    )
    row = EpisodeContextManifest(
        id=ids.episode_context_manifest_id(),
        series_id=series_id,
        episode_id=episode.id,
        context_version=int(latest or 0) + 1,
        manifest_json=manifest,
        context_hash=manifest.get("context_hash"),
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
        model=model,
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    episode.context_manifest_id = row.id
    episode.updated_at = _now()
    db.flush()
    return row


def build_and_persist_for_episode(
    db: Session,
    *,
    series: Series,
    episode: SeriesEpisode,
    user_instruction: str,
    constraints: dict | None = None,
    model: str | None = None,
) -> EpisodeContextManifest:
    """正式续写入口：构建并落库 Manifest，必须项缺失时直接失败。"""
    context = build_context(
        db,
        series=series,
        target_episode_number=episode.episode_number,
        user_instruction=user_instruction,
        constraints=constraints,
    )
    blocking = [
        m for m in context["missing"] if m.get("code") != "previous_episode_not_confirmed"
    ]
    if blocking:
        raise errors.series_context_unavailable(
            "上下文构建失败："
            + "；".join(str(m.get("message") or m.get("code") or "") for m in blocking)
        )
    return persist_manifest(
        db, series_id=series.id, episode=episode, manifest=context, model=model
    )


def latest_manifest(db: Session, episode_id: str) -> EpisodeContextManifest | None:
    get_episode(db, episode_id)
    return (
        db.query(EpisodeContextManifest)
        .filter(EpisodeContextManifest.episode_id == episode_id)
        .order_by(EpisodeContextManifest.context_version.desc())
        .first()
    )
