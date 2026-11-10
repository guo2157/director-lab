"""叙事连续性检查服务（Continuity Validator，叙事维度）。

边界：与 `app/services/continuity.py`（跨集视频首尾帧连续性）无关。

实现范围说明（避免夸大能力）：
本服务只做**可确定性计算**的检查，即所有结论都能由数据库中的结构化数据推导：
- 上一集是否已确认（草稿不能作为正式依据）
- 连续性快照是否存在
- 提案中的道具持有人是否与快照状态冲突
- 提案是否试图把已解决剧情线重新置为未解决
- 提案中的事件是否与既有确认事件重复
- 提案引用的资产是否属于该系列
- 新增资产名称是否与既有系列资产重名（只提示，不自动合并）

语义级剧情冲突（需要自然语言理解）不在本服务内伪造实现，
由上层 Agent 产出候选后，仍必须经过本服务的结构化校验与人工确认。
"""
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    ContinuitySnapshot,
    EpisodeAssetRef,
    PlotThread,
    Series,
    SeriesAsset,
    SeriesEpisode,
    StoryEvent,
)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

OPEN_THREAD_STATUS = ("open", "in_progress")


def _issue(
    severity: str,
    issue_type: str,
    message: str,
    *,
    asset_id: str | None = None,
    event_ids: list[str] | None = None,
    suggestion: str | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "type": issue_type,
        "asset_id": asset_id,
        "message": message,
        "source_event_ids": event_ids or [],
        "suggestion": suggestion,
    }


def _latest_snapshot(db: Session, series_id: str) -> ContinuitySnapshot | None:
    return (
        db.query(ContinuitySnapshot)
        .filter(ContinuitySnapshot.series_id == series_id)
        .order_by(ContinuitySnapshot.version.desc())
        .first()
    )


def check_previous_episode(db: Session, series: Series, episode: SeriesEpisode) -> list[dict]:
    """上一集未确认时给出警告：允许继续创作，但不得把草稿当作正式依据。"""
    issues: list[dict] = []
    prev = (
        db.query(SeriesEpisode)
        .filter(
            SeriesEpisode.series_id == series.id,
            SeriesEpisode.episode_number < episode.episode_number,
        )
        .order_by(SeriesEpisode.episode_number.desc())
        .first()
    )
    if prev is None:
        issues.append(
            _issue(
                SEVERITY_INFO,
                "first_episode",
                "本集是本系列第一集，无历史剧情约束",
            )
        )
        return issues
    if prev.script_status != "confirmed":
        issues.append(
            _issue(
                SEVERITY_WARNING,
                "previous_episode_not_confirmed",
                f"第 {prev.episode_number} 集剧本状态为 {prev.script_status}，未确认草稿不能作为正式剧情依据",
                suggestion="先确认上一集剧本，或在创作要求中明确本次仅做全新开场",
            )
        )
    return issues


def check_proposal(
    db: Session,
    *,
    series: Series,
    episode: SeriesEpisode,
    proposal: dict | None,
) -> list[dict]:
    """校验状态变更提案：结构化、可定位、可追溯。"""
    issues: list[dict] = []
    proposal = proposal or {}
    snapshot = _latest_snapshot(db, series.id)

    # 1. 道具持有人冲突：提案声明的 holder 与快照中的 holder 不一致
    snapshot_state = (snapshot.state_json if snapshot else None) or {}
    snap_props = snapshot_state.get("props") or {}
    for prop in proposal.get("props") or []:
        asset_id = prop.get("asset_id")
        if not asset_id:
            continue
        new_holder = prop.get("holder_asset_id")
        old_holder = (snap_props.get(asset_id) or {}).get("holder_asset_id")
        if new_holder is not None and old_holder is not None and new_holder != old_holder:
            issues.append(
                _issue(
                    SEVERITY_ERROR,
                    "prop_state_conflict",
                    f"道具 {asset_id} 在最近快照中的持有人为 {old_holder}，本集提案变更为 {new_holder}",
                    asset_id=asset_id,
                    suggestion="补充道具转移动因与过程，或由用户确认历史状态是否需要修正",
                )
            )

    # 2. 已解决剧情线被重新打开
    reopen_ids = proposal.get("reopen_plot_thread_ids") or []
    if reopen_ids:
        resolved = (
            db.query(PlotThread)
            .filter(
                PlotThread.series_id == series.id,
                PlotThread.id.in_(reopen_ids),
                PlotThread.status.in_(("resolved", "abandoned")),
            )
            .all()
        )
        for thread in resolved:
            issues.append(
                _issue(
                    SEVERITY_ERROR,
                    "plot_thread_reopen_conflict",
                    f"剧情线「{thread.title}」状态为 {thread.status}，不能无理由重新变为未解决",
                    suggestion="如需重启该剧情线，请先由用户确认改为新的剧情线",
                )
            )

    # 3. 事件键重复（同一剧本版本内）
    events = proposal.get("events") or []
    seen_keys: set[str] = set()
    for ev in events:
        key = ev.get("event_key")
        if not key:
            issues.append(
                _issue(
                    SEVERITY_ERROR,
                    "event_key_missing",
                    "事件候选缺少 event_key，无法保证幂等入账",
                )
            )
            continue
        if key in seen_keys:
            issues.append(
                _issue(
                    SEVERITY_ERROR,
                    "event_key_duplicated",
                    f"事件键 {key} 在本次提案中重复出现",
                )
            )
        seen_keys.add(key)

    # 4. 提案引用的资产必须属于本系列（或本集已引用）
    series_asset_ids = {
        row.asset_id
        for row in db.query(SeriesAsset).filter(SeriesAsset.series_id == series.id).all()
    }
    episode_asset_ids = {
        row.asset_id
        for row in db.query(EpisodeAssetRef)
        .filter(EpisodeAssetRef.episode_id == episode.id)
        .all()
    }
    allowed = series_asset_ids | episode_asset_ids
    for ev in events:
        for asset_id in ev.get("involved_asset_ids") or []:
            if asset_id and asset_id not in allowed:
                issues.append(
                    _issue(
                        SEVERITY_WARNING,
                        "asset_not_in_series",
                        f"事件引用了未登记到本系列的资产 {asset_id}",
                        asset_id=asset_id,
                        suggestion="先在共享资产中登记该资产，或确认是否为新建资产",
                    )
                )

    # 5. 新增资产名称与既有系列资产重名：只提示，禁止自动合并
    duplicate_names = proposal.get("duplicate_asset_names") or []
    for name in duplicate_names:
        issues.append(
            _issue(
                SEVERITY_WARNING,
                "asset_name_duplicated",
                f"资产名称「{name}」与系列中已有资产重名，但名称相同不代表同一角色",
                suggestion="请人工确认是否为同一资产，禁止仅凭名称合并",
            )
        )

    return issues


def check_script_version(
    db: Session,
    *,
    series: Series,
    episode: SeriesEpisode,
    proposal: dict | None = None,
) -> dict[str, Any]:
    """汇总检查结果：返回 status + issues。预览与正式提交共用。"""
    issues: list[dict] = []
    issues.extend(check_previous_episode(db, series, episode))
    if _latest_snapshot(db, series.id) is None:
        issues.append(
            _issue(
                SEVERITY_WARNING,
                "continuity_snapshot_missing",
                "系列尚无正式连续性快照，本集将成为首个确认集",
            )
        )
    confirmed_count = (
        db.query(StoryEvent)
        .filter(
            StoryEvent.series_id == series.id,
            StoryEvent.fact_status == "confirmed",
        )
        .count()
    )
    if confirmed_count == 0:
        issues.append(
            _issue(
                SEVERITY_INFO,
                "no_confirmed_events",
                "系列当前没有任何已确认剧情事件",
            )
        )
    issues.extend(check_proposal(db, series=series, episode=episode, proposal=proposal))

    severities = {i["severity"] for i in issues}
    if SEVERITY_ERROR in severities:
        status = "conflict"
    elif SEVERITY_WARNING in severities:
        status = "warning"
    else:
        status = "consistent"
    return {"status": status, "issues": issues}


def summary_text(result: dict[str, Any]) -> str:
    """给前端/日志的简短摘要，避免把整个 issues 打印到日志。"""
    errors = sum(1 for i in result.get("issues", []) if i["severity"] == SEVERITY_ERROR)
    warnings = sum(1 for i in result.get("issues", []) if i["severity"] == SEVERITY_WARNING)
    return f"连续性检查：{result.get('status')}（error {errors} / warning {warnings}）"
