"""正式剧情状态提交服务（State Commit Service）。

唯一性：只有本服务可以修改正式连续性状态（story_events / continuity_snapshots /
series.state_version / series_episodes.script_status / latest_confirmed_episode_id）。
Agent 只能写 continuity_change_proposals。

并发与幂等（对应需求 §12.3）：
1. `series.state_version` 乐观锁：提交方必须携带 expected 值，不匹配直接拒绝。
2. 幂等键：`continuity_change_proposals.idempotency_key` 唯一，重复提交命中已有提案
   并直接返回既有结果，不重复入账。
3. 单事务：事件、快照、剧集状态、系列指针在同一事务内提交，失败全部回滚。
4. 冲突处理：版本不匹配时不自动覆盖，返回冲突让调用方刷新后重新提交。

本服务不生成内容，只做校验与提交。
"""
import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ContinuityChangeProposal,
    ContinuitySnapshot,
    Series,
    SeriesEpisode,
    SeriesScriptVersion,
    StoryEvent,
)
from app.models import ids
from app.services import errors

from .narrative_continuity_service import check_script_version


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _state_hash(state: dict | None) -> str:
    raw = json.dumps(state or {}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_idempotency_key(episode_id: str, script_version_id: str, action: str = "confirm") -> str:
    return f"{action}:{episode_id}:{script_version_id}"


def _latest_snapshot(db: Session, series_id: str) -> ContinuitySnapshot | None:
    return (
        db.query(ContinuitySnapshot)
        .filter(ContinuitySnapshot.series_id == series_id)
        .order_by(ContinuitySnapshot.version.desc())
        .first()
    )


def _apply_state_patch(state: dict, proposal: dict) -> dict:
    """把提案中的状态变更合并进快照状态；只支持结构化字段，不做语义推断。"""
    merged = json.loads(json.dumps(state or {}))
    for key in ("characters", "props", "scenes"):
        incoming = proposal.get(key)
        if not isinstance(incoming, dict):
            continue
        bucket = merged.setdefault(key, {})
        for asset_id, patch in incoming.items():
            if not isinstance(patch, dict):
                continue
            current = bucket.get(asset_id) or {}
            current.update(patch)
            bucket[asset_id] = current
    return merged


def _persist_events(
    db: Session,
    *,
    series_id: str,
    episode: SeriesEpisode,
    script_version_id: str,
    events: list[dict],
    created_by: str,
    promote_proposed: bool = True,
) -> tuple[int, list[str]]:
    """写入正式事件。冲突事件不自动入账，返回冲突 id 列表由人工处理。"""
    written = 0
    contradicted: list[str] = []
    now = _now()
    existing_keys = {
        row.event_key
        for row in db.query(StoryEvent.event_key)
        .filter(
            StoryEvent.episode_id == episode.id,
            StoryEvent.script_version_id == script_version_id,
        )
        .all()
    }
    order_base = (
        db.query(func.max(StoryEvent.occurred_order))
        .filter(StoryEvent.series_id == series_id)
        .scalar()
        or 0
    )
    for index, ev in enumerate(events, start=1):
        key = ev.get("event_key") or f"auto_{index}"
        if key in existing_keys:
            continue
        fact_status = ev.get("fact_status") or "confirmed"
        if fact_status == "contradicted":
            contradicted.append(key)
            continue
        # 用户确认剧本即承认本集候选事实：proposed 随确认转为正式事实 confirmed。
        # derived / uncertain 表示证据不足，保持原状，不作为正式剧情依据。
        if promote_proposed and fact_status == "proposed":
            fact_status = "confirmed"
        if fact_status != "confirmed":
            fact_status = "derived" if fact_status == "derived" else "uncertain"
        db.add(
            StoryEvent(
                id=ids.story_event_id(),
                series_id=series_id,
                episode_id=episode.id,
                script_version_id=script_version_id,
                event_key=key,
                event_type=ev.get("event_type") or "other",
                title=(ev.get("title") or key)[:200],
                description=ev.get("description"),
                involved_asset_ids=ev.get("involved_asset_ids") or [],
                location_asset_id=ev.get("location_asset_id"),
                source_scene_id=ev.get("source_scene_id"),
                source_text=ev.get("source_text"),
                fact_status=fact_status,
                importance=int(ev.get("importance") or 3),
                occurred_order=order_base + index,
                created_at=now,
                created_by=created_by,
            )
        )
        existing_keys.add(key)
        written += 1
    return written, contradicted


def confirm_script(
    db: Session,
    *,
    series: Series,
    episode: SeriesEpisode,
    script_version: SeriesScriptVersion,
    expected_state_version: int,
    proposal: dict | None,
    review_note: str | None,
    created_by: str = "user",
    ignore_warnings: bool = False,
) -> dict[str, Any]:
    """确认剧本并提交正式状态。

    失败场景：
    - 剧本状态不是 awaiting_review / draft（非法流转）
    - expected_state_version 与当前不一致（乐观锁）
    - 连续性检查存在 error 且未显式处理
    - 幂等键已提交过（直接返回既有结果）
    """
    if script_version.episode_id != episode.id:
        raise errors.series_script_status_conflict("剧本版本不属于该剧集")
    if script_version.status == "confirmed":
        # 已确认过的版本再次确认：按幂等处理，不重复入账
        existing = (
            db.query(ContinuityChangeProposal)
            .filter(
                ContinuityChangeProposal.idempotency_key
                == build_idempotency_key(episode.id, script_version.id)
            )
            .first()
        )
        snapshot = _latest_snapshot(db, series.id)
        return {
            "episode_id": episode.id,
            "script_version_id": script_version.id,
            "script_status": "confirmed",
            "series_state_version": series.state_version,
            "snapshot_version": snapshot.version if snapshot else 0,
            "committed_event_count": 0,
            "issues": [],
            "ignored_issue_ids": [],
            "idempotent_replay": True,
            "proposal_id": existing.id if existing else None,
        }
    if script_version.status not in ("draft", "awaiting_review", "analyzing"):
        raise errors.series_script_status_conflict(
            f"剧本状态为 {script_version.status}，不允许确认"
        )

    if expected_state_version != series.state_version:
        raise errors.series_state_version_conflict(
            f"系列状态版本已变化（期望 {expected_state_version}，当前 {series.state_version}），"
            "请刷新最新剧情状态后重新提交"
        )

    check = check_script_version(
        db, series=series, episode=episode, proposal=proposal
    )
    blocking = [i for i in check["issues"] if i["severity"] == "error"]
    if blocking and not ignore_warnings:
        # error 级问题必须显式处理，不允许默认忽略
        raise errors.series_script_status_conflict(
            "存在阻断性连续性问题（"
            + "；".join(str(i.get("message")) for i in blocking)
            + "），请先处理或显式忽略非阻断警告"
        )

    idempotency_key = build_idempotency_key(episode.id, script_version.id)
    proposal_row = ContinuityChangeProposal(
        id=ids.continuity_proposal_id(),
        series_id=series.id,
        episode_id=episode.id,
        script_version_id=script_version.id,
        base_state_version=series.state_version,
        proposal_json=proposal or {},
        status="pending",
        idempotency_key=idempotency_key,
        created_at=_now(),
    )
    db.add(proposal_row)

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise errors.series_state_version_conflict(
            "该剧本版本已提交过状态变更，请刷新后确认结果"
        ) from None

    previous_snapshot = _latest_snapshot(db, series.id)
    base_state = previous_snapshot.state_json if previous_snapshot else {}
    new_state = _apply_state_patch(base_state, proposal or {})

    events = (proposal or {}).get("events") or []
    written, contradicted = _persist_events(
        db,
        series_id=series.id,
        episode=episode,
        script_version_id=script_version.id,
        events=events,
        created_by=created_by,
    )

    next_version = (previous_snapshot.version + 1) if previous_snapshot else 1
    snapshot = ContinuitySnapshot(
        id=ids.continuity_snapshot_id(),
        series_id=series.id,
        mainline_id=series.mainline_id or "main",
        version=next_version,
        based_on_episode_id=episode.id,
        based_on_script_version_id=script_version.id,
        state_json=new_state,
        state_hash=_state_hash(new_state),
        created_at=_now(),
        created_by=created_by,
    )
    db.add(snapshot)

    now = _now()
    script_version.status = "confirmed"
    script_version.continuity_result = check
    if review_note:
        existing_validation = dict(script_version.validation_result or {})
        existing_validation["review_note"] = review_note
        script_version.validation_result = existing_validation

    # 同一剧集此前确认的版本置为 superseded，保证「当前正式版本」唯一
    db.query(SeriesScriptVersion).filter(
        SeriesScriptVersion.episode_id == episode.id,
        SeriesScriptVersion.id != script_version.id,
        SeriesScriptVersion.status == "confirmed",
    ).update({SeriesScriptVersion.status: "superseded"}, synchronize_session=False)

    episode.script_status = "confirmed"
    episode.confirmed_script_version_id = script_version.id
    episode.script_current_version_id = script_version.id
    episode.continuity_status = check["status"]
    episode.updated_at = now

    series.latest_confirmed_episode_id = episode.id
    series.state_version = series.state_version + 1
    series.updated_at = now

    proposal_row.status = "committed"
    proposal_row.committed_at = now

    db.commit()

    return {
        "episode_id": episode.id,
        "script_version_id": script_version.id,
        "script_status": "confirmed",
        "series_state_version": series.state_version,
        "snapshot_version": snapshot.version,
        "committed_event_count": written,
        "issues": check["issues"],
        "ignored_issue_ids": [i.get("type") for i in check["issues"] if ignore_warnings],
        "contradicted_event_keys": contradicted,
        "idempotent_replay": False,
        "proposal_id": proposal_row.id,
    }


def mark_production_completed(
    db: Session, *, series: Series, episode: SeriesEpisode
) -> None:
    """视频生产完成时更新系列指针；与剧本确认完全解耦，失败不影响剧本状态。"""
    if episode.production_status != "completed":
        return
    series.latest_completed_episode_id = episode.id
    series.updated_at = _now()
    db.commit()
