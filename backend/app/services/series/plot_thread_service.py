"""剧情线管理服务。

纪律：
- AI 可以建议推进剧情线，但不能擅自认定已解决；状态变更只走本服务的显式接口。
- 已 resolved / abandoned 的剧情线不会被自动复活。
- 续写时优先加载 open / in_progress。
"""
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import PlotThread, SeriesEpisode
from app.models import ids
from app.models.series_story import PLOT_THREAD_STATUS
from app.services import errors

from .series_service import get_series


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def list_threads(
    db: Session, series_id: str, *, status: str | None = None
) -> list[PlotThread]:
    get_series(db, series_id)
    q = db.query(PlotThread).filter(PlotThread.series_id == series_id)
    if status:
        if status not in PLOT_THREAD_STATUS:
            raise errors.invalid_request(f"status 须为 {PLOT_THREAD_STATUS} 之一")
        q = q.filter(PlotThread.status == status)
    return q.order_by(PlotThread.created_at.asc()).all()


def get_thread(db: Session, thread_id: str) -> PlotThread:
    row = db.get(PlotThread, thread_id)
    if row is None:
        raise errors.plot_thread_not_found()
    return row


def create_thread(db: Session, series_id: str, payload: Any) -> PlotThread:
    get_series(db, series_id)
    if payload.introduced_episode_id:
        ep = db.get(SeriesEpisode, payload.introduced_episode_id)
        if ep is None or ep.series_id != series_id:
            raise errors.series_episode_not_found()
    row = PlotThread(
        id=ids.plot_thread_id(),
        series_id=series_id,
        title=payload.title.strip(),
        description=payload.description,
        introduced_episode_id=payload.introduced_episode_id,
        last_updated_episode_id=payload.introduced_episode_id,
        status="open",
        related_asset_ids=payload.related_asset_ids,
        related_event_ids=None,
        resolution_condition=payload.resolution_condition,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_thread(db: Session, thread_id: str, payload: Any) -> PlotThread:
    row = get_thread(db, thread_id)
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "title" in data:
        data["title"] = data["title"].strip()
    if "last_updated_episode_id" in data:
        ep = db.get(SeriesEpisode, data["last_updated_episode_id"])
        if ep is None or ep.series_id != row.series_id:
            raise errors.series_episode_not_found()
    for key, value in data.items():
        setattr(row, key, value)
    row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return row


def attach_related_events(
    db: Session, thread_id: str, event_ids: list[str]
) -> PlotThread:
    """仅追加关联事件，不改变剧情线状态，避免误判为已解决。"""
    row = get_thread(db, thread_id)
    current = list(row.related_event_ids or [])
    for eid in event_ids:
        if eid not in current:
            current.append(eid)
    row.related_event_ids = current
    row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return row
