'''历史剧本修改影响分析（只读服务）。

边界声明：
- 与 app/services/continuity.py（跨集视频首尾帧连续性）无关。
- 与 narrative_continuity_service 分工：后者在确认某一集时检查该集是否与既有状态冲突；
  本服务在修改某一集历史剧本时，反向推算对后续已确认剧集的影响面。

纪律：
- 只读：不写事件、不写快照、不改任何正式剧情状态，不自动修复。
- 只做可确定性计算：结论均由已落库的结构化数据推导（事件键、资产 id、剧情线状态）。
- 语义级影响不做伪造实现，只在 suggestion 中提示需人工复核。
'''
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    ContinuitySnapshot,
    PlotThread,
    Series,
    SeriesEpisode,
    SeriesScriptVersion,
    StoryEvent,
)

SEVERITY_ERROR = 'error'
SEVERITY_WARNING = 'warning'
SEVERITY_INFO = 'info'


def _issue(
    severity: str,
    issue_type: str,
    message: str,
    *,
    asset_id: str | None = None,
    event_ids: list[str] | None = None,
    episode_ids: list[str] | None = None,
    suggestion: str | None = None,
) -> dict[str, Any]:
    return {
        'severity': severity,
        'type': issue_type,
        'asset_id': asset_id,
        'message': message,
        'source_event_ids': event_ids or [],
        'affected_episode_ids': episode_ids or [],
        'suggestion': suggestion,
    }


def _confirmed_version(db: Session, episode: SeriesEpisode) -> SeriesScriptVersion | None:
    if episode.confirmed_script_version_id:
        row = db.get(SeriesScriptVersion, episode.confirmed_script_version_id)
        if row is not None:
            return row
    return (
        db.query(SeriesScriptVersion)
        .filter(
            SeriesScriptVersion.episode_id == episode.id,
            SeriesScriptVersion.status == 'confirmed',
        )
        .order_by(SeriesScriptVersion.version.desc())
        .first()
    )


def _event_keys(db: Session, script_version_id: str) -> dict[str, dict]:
    row = db.get(SeriesScriptVersion, script_version_id)
    if row is None:
        return {}
    events = (row.validation_result or {}).get('events') or []
    result: dict[str, dict] = {}
    for index, ev in enumerate(events, start=1):
        if not isinstance(ev, dict):
            continue
        key = str(ev.get('event_key') or 'auto_%d' % index)
        result[key] = ev
    return result


def _asset_ids_of(events: dict[str, dict]) -> set[str]:
    ids: set[str] = set()
    for ev in events.values():
        for asset_id in ev.get('involved_asset_ids') or []:
            if asset_id:
                ids.add(str(asset_id))
    return ids


def analyze_script_change(
    db: Session,
    *,
    series: Series,
    episode: SeriesEpisode,
    script_version: SeriesScriptVersion,
) -> dict[str, Any]:
    '''分析把 script_version 作为该集剧本时，对后续已确认剧集的影响。

    返回 status / issues / diff / affected_episodes / summary。
    status 语义与叙事连续性一致：conflict（存在 error）/ warning / consistent。
    '''
    issues: list[dict] = []
    confirmed = _confirmed_version(db, episode)

    new_events = _event_keys(db, script_version.id)
    old_events = _event_keys(db, confirmed.id) if confirmed is not None else {}

    added = sorted(set(new_events) - set(old_events))
    removed = sorted(set(old_events) - set(new_events))
    kept = sorted(set(new_events) & set(old_events))

    diff = {
        'base_script_version_id': confirmed.id if confirmed else None,
        'base_version': confirmed.version if confirmed else None,
        'compare_script_version_id': script_version.id,
        'compare_version': script_version.version,
        'added_event_keys': added,
        'removed_event_keys': removed,
        'unchanged_event_keys': kept,
    }

    if confirmed is None:
        issues.append(
            _issue(
                SEVERITY_INFO,
                'no_confirmed_baseline',
                '该集尚无已确认剧本版本，本次修改不会替换任何既有正式剧情事实',
            )
        )
        return {
            'status': 'consistent',
            'issues': issues,
            'diff': diff,
            'affected_episodes': [],
            'summary': '无已确认基线版本，仅记录事件键差异',
        }

    if confirmed.id == script_version.id:
        issues.append(
            _issue(SEVERITY_INFO, 'same_as_confirmed', '对比版本即当前已确认版本，未检测到差异')
        )
        return {
            'status': 'consistent',
            'issues': issues,
            'diff': diff,
            'affected_episodes': [],
            'summary': '与已确认版本一致，无影响',
        }

    later_episodes = (
        db.query(SeriesEpisode)
        .filter(
            SeriesEpisode.series_id == series.id,
            SeriesEpisode.episode_number > episode.episode_number,
            SeriesEpisode.script_status == 'confirmed',
        )
        .order_by(SeriesEpisode.episode_number)
        .all()
    )
    later_ids = [e.id for e in later_episodes]

    later_confirmed_events = (
        db.query(StoryEvent)
        .filter(
            StoryEvent.series_id == series.id,
            StoryEvent.fact_status == 'confirmed',
            StoryEvent.episode_id.in_(later_ids),
        )
        .all()
        if later_ids
        else []
    )

    if removed and later_confirmed_events:
        issues.append(
            _issue(
                SEVERITY_WARNING,
                'removed_event_key_with_later_events',
                '新版本移除了 %d 个事件键，但后续已有 %d 条已确认剧情事件，被移除的事实可能不再有依据'
                % (len(removed), len(later_confirmed_events)),
                event_ids=removed,
                episode_ids=later_ids,
                suggestion='确认后续剧集是否仍依赖这些事实；如依赖，请在后续剧集中补充交代或保留原事件',
            )
        )

    new_assets = _asset_ids_of(new_events)
    later_asset_map: dict[str, list[str]] = {}
    for ev in later_confirmed_events:
        for asset_id in ev.involved_asset_ids or []:
            if not asset_id:
                continue
            later_asset_map.setdefault(str(asset_id), []).append(ev.id)

    for asset_id in sorted(new_assets & set(later_asset_map)):
        event_ids = later_asset_map[asset_id]
        issues.append(
            _issue(
                SEVERITY_WARNING,
                'asset_shared_with_later_episodes',
                '资产 %s 同时出现在本集新剧本与后续 %d 条已确认事件中，本集改动可能影响这些资产的既有剧情状态'
                % (asset_id, len(event_ids)),
                asset_id=asset_id,
                event_ids=event_ids,
                suggestion='逐条复核后续事件中对这些资产的状态描述是否仍然成立',
            )
        )

    snapshot = (
        db.query(ContinuitySnapshot)
        .filter(ContinuitySnapshot.series_id == series.id)
        .order_by(ContinuitySnapshot.version.desc())
        .first()
    )
    state = (snapshot.state_json if snapshot else None) or {}
    snap_props = state.get('props') or {}
    for key, ev in new_events.items():
        for asset_id in ev.get('involved_asset_ids') or []:
            holder = (snap_props.get(str(asset_id)) or {}).get('holder_asset_id')
            if holder is None:
                continue
            declared = ev.get('holder_asset_id')
            if declared is not None and str(declared) != str(holder):
                issues.append(
                    _issue(
                        SEVERITY_ERROR,
                        'prop_state_conflict_with_snapshot',
                        '事件 %s 声明道具 %s 的持有人为 %s，但最新快照记录为 %s'
                        % (key, asset_id, declared, holder),
                        asset_id=str(asset_id),
                        suggestion='修改历史集会导致状态来源不一致，请确认是否改为新增剧情线而非改写历史',
                    )
                )

    updates = (script_version.validation_result or {}).get('plot_thread_updates') or []
    resolve_titles = [
        str(u.get('title'))
        for u in updates
        if isinstance(u, dict) and u.get('action') == 'resolve' and u.get('title')
    ]
    if resolve_titles:
        existing = (
            db.query(PlotThread)
            .filter(PlotThread.series_id == series.id, PlotThread.title.in_(resolve_titles))
            .all()
        )
        for thread in existing:
            if thread.status not in ('resolved', 'abandoned'):
                continue
            issues.append(
                _issue(
                    SEVERITY_WARNING,
                    'plot_thread_already_closed',
                    '剧情线「%s」已是 %s 状态，新版本再次声明 resolve，可能覆盖既有解决时点'
                    % (thread.title, thread.status),
                    suggestion='确认应保留原解决集数，不要改写历史解决时点',
                )
            )

    severities = {i['severity'] for i in issues}
    if SEVERITY_ERROR in severities:
        status = 'conflict'
    elif SEVERITY_WARNING in severities:
        status = 'warning'
    else:
        status = 'consistent'

    return {
        'status': status,
        'issues': issues,
        'diff': diff,
        'affected_episodes': [
            {
                'id': e.id,
                'episode_number': e.episode_number,
                'title': e.title,
                'confirmed_event_count': sum(
                    1 for ev in later_confirmed_events if ev.episode_id == e.id
                ),
            }
            for e in later_episodes
        ],
        'summary': '影响分析：%s（新增 %d / 移除 %d / 保留 %d 个事件键，后续已确认剧集 %d 集）'
        % (status, len(added), len(removed), len(kept), len(later_episodes)),
    }
