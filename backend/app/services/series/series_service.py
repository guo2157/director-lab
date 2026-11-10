"""系列管理服务：创建、查询、更新、归档。

纪律：
- 归档不物理删除任何历史数据。
- 列表中的统计数字全部来自真实查询，缺少数据时返回 0，不填充假数据。
"""
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    PlotThread,
    Series,
    SeriesBibleVersion,
    SeriesEpisode,
    StoryEvent,
)
from app.models import ids
from app.services import errors


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def get_series(db: Session, series_id: str) -> Series:
    s = db.get(Series, series_id)
    if s is None:
        raise errors.series_not_found()
    return s


def get_active_series(db: Session, series_id: str) -> Series:
    s = get_series(db, series_id)
    if s.status != "active":
        raise errors.series_script_status_conflict("系列已归档，请先恢复后再操作")
    return s


def create_series(db: Session, payload: Any) -> Series:
    s = Series(
        id=ids.series_id(),
        name=payload.name.strip(),
        description=payload.description,
        cover_image=payload.cover_image,
        genre=payload.genre,
        style=payload.style,
        target_audience=payload.target_audience,
        language=payload.language or "zh-CN",
        status="active",
        mainline_id="main",
        state_version=1,
    )
    db.add(s)
    db.flush()
    # 创建即建立 Bible V1，避免后续上下文构建时缺项
    bible = SeriesBibleVersion(
        id=ids.series_bible_version_id(),
        series_id=s.id,
        version=1,
        content=payload.bible or {},
        change_summary="初始版本",
        created_by="user",
        created_at=_now(),
        is_active=True,
    )
    db.add(bible)
    db.commit()
    db.refresh(s)
    return s


def update_series(db: Session, series_id: str, payload: Any) -> Series:
    s = get_series(db, series_id)
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in data.items():
        if key == "name":
            value = value.strip()
        setattr(s, key, value)
    s.updated_at = _now()
    db.commit()
    db.refresh(s)
    return s


def archive_series(db: Session, series_id: str) -> Series:
    s = get_series(db, series_id)
    if s.status == "archived":
        raise errors.invalid_state_transition("系列已归档")
    s.status = "archived"
    s.updated_at = _now()
    db.commit()
    db.refresh(s)
    return s


def unarchive_series(db: Session, series_id: str) -> Series:
    s = get_series(db, series_id)
    if s.status != "archived":
        raise errors.invalid_state_transition("系列未归档")
    s.status = "active"
    s.updated_at = _now()
    db.commit()
    db.refresh(s)
    return s


def series_stats(db: Session, series_id: str) -> dict[str, int]:
    """系列派生统计：全部为真实聚合查询。"""
    episode_count = (
        db.query(func.count(SeriesEpisode.id))
        .filter(SeriesEpisode.series_id == series_id)
        .scalar()
        or 0
    )
    confirmed_script_count = (
        db.query(func.count(SeriesEpisode.id))
        .filter(
            SeriesEpisode.series_id == series_id,
            SeriesEpisode.script_status == "confirmed",
        )
        .scalar()
        or 0
    )
    completed_video_count = (
        db.query(func.count(SeriesEpisode.id))
        .filter(
            SeriesEpisode.series_id == series_id,
            SeriesEpisode.production_status == "completed",
        )
        .scalar()
        or 0
    )
    open_plot_thread_count = (
        db.query(func.count(PlotThread.id))
        .filter(
            PlotThread.series_id == series_id,
            PlotThread.status.in_(("open", "in_progress")),
        )
        .scalar()
        or 0
    )
    bible_version = (
        db.query(func.max(SeriesBibleVersion.version))
        .filter(
            SeriesBibleVersion.series_id == series_id,
            SeriesBibleVersion.is_active.is_(True),
        )
        .scalar()
        or 0
    )
    return {
        "episode_count": int(episode_count),
        "confirmed_script_count": int(confirmed_script_count),
        "completed_video_count": int(completed_video_count),
        "open_plot_thread_count": int(open_plot_thread_count),
        "bible_version": int(bible_version),
    }


def list_series(
    db: Session,
    *,
    keyword: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    q = db.query(Series)
    if status:
        q = q.filter(Series.status == status)
    if keyword:
        like = f"%{keyword.strip()}%"
        q = q.filter(Series.name.like(like))
    total = q.count()
    rows = (
        q.order_by(Series.updated_at.desc(), Series.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items: list[dict] = []
    for s in rows:
        stats = series_stats(db, s.id)
        items.append({"series": s, "stats": stats})
    return items, total


def active_bible(db: Session, series_id: str) -> SeriesBibleVersion | None:
    return (
        db.query(SeriesBibleVersion)
        .filter(
            SeriesBibleVersion.series_id == series_id,
            SeriesBibleVersion.is_active.is_(True),
        )
        .order_by(SeriesBibleVersion.version.desc())
        .first()
    )


def confirmed_event_count(db: Session, series_id: str) -> int:
    return int(
        db.query(func.count(StoryEvent.id))
        .filter(
            StoryEvent.series_id == series_id,
            StoryEvent.fact_status == "confirmed",
        )
        .scalar()
        or 0
    )
