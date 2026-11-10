"""剧集管理服务：系列下的单集，绑定独立生产工作空间 Project。

关键约束：
- 剧本状态与生产状态必须独立管理，禁止互相推导。
- 集号在系列内唯一；创建时若未指定则自动取「当前最大集号 + 1」。
- 历史项目兼容：`project_id` 可空，`project_episode_no` 记录其在 Project 内的真实集号。
"""
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Project, SeriesEpisode
from app.models import ids
from app.models.series import PRODUCTION_STATUS, SCRIPT_STATUS
from app.services import errors

from .series_service import get_series


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def next_episode_number(db: Session, series_id: str) -> int:
    current = (
        db.query(func.max(SeriesEpisode.episode_number))
        .filter(SeriesEpisode.series_id == series_id)
        .scalar()
    )
    return int(current or 0) + 1


def get_episode(db: Session, episode_id: str) -> SeriesEpisode:
    ep = db.get(SeriesEpisode, episode_id)
    if ep is None:
        raise errors.series_episode_not_found()
    return ep


def create_episode(db: Session, series_id: str, payload: Any) -> SeriesEpisode:
    get_series(db, series_id)
    number = payload.episode_number or next_episode_number(db, series_id)
    exists = (
        db.query(SeriesEpisode.id)
        .filter(
            SeriesEpisode.series_id == series_id,
            SeriesEpisode.episode_number == number,
        )
        .first()
    )
    if exists is not None:
        raise errors.series_episode_conflict(f"第 {number} 集已存在")
    ep = SeriesEpisode(
        id=ids.series_episode_id(),
        series_id=series_id,
        project_id=payload.project_id,
        project_episode_no=1 if payload.project_id else None,
        episode_number=number,
        title=payload.title,
        synopsis=payload.synopsis,
        script_status="draft",
        production_status="not_started",
        continuity_status="not_checked",
        mainline_id="main",
    )
    db.add(ep)
    db.commit()
    db.refresh(ep)
    return ep


def list_episodes(
    db: Session, series_id: str, *, page: int = 1, page_size: int = 100
) -> tuple[list[SeriesEpisode], int]:
    get_series(db, series_id)
    q = db.query(SeriesEpisode).filter(SeriesEpisode.series_id == series_id)
    total = q.count()
    rows = (
        q.order_by(SeriesEpisode.episode_number.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return rows, total


def set_script_status(db: Session, episode: SeriesEpisode, status: str) -> SeriesEpisode:
    """剧本状态流转：仅允许白名单值，非法值直接拒绝。"""
    if status not in SCRIPT_STATUS:
        raise errors.invalid_request(f"script_status 须为 {SCRIPT_STATUS} 之一")
    episode.script_status = status
    episode.updated_at = _now()
    db.flush()
    return episode


def set_production_status(
    db: Session, episode: SeriesEpisode, status: str
) -> SeriesEpisode:
    """生产状态流转：与剧本状态完全独立，视频未完成不影响剧本创作。"""
    if status not in PRODUCTION_STATUS:
        raise errors.invalid_request(f"production_status 须为 {PRODUCTION_STATUS} 之一")
    episode.production_status = status
    episode.updated_at = _now()
    db.flush()
    return episode


def bind_project(db: Session, episode: SeriesEpisode, project_id: str) -> SeriesEpisode:
    """绑定生产工作空间；Project 不存在时拒绝，避免脏关联。"""
    project = db.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise errors.project_not_found()
    episode.project_id = project_id
    episode.project_episode_no = 1
    episode.updated_at = _now()
    db.flush()
    return episode


def previous_confirmed_episode(
    db: Session, series_id: str, episode_number: int
) -> SeriesEpisode | None:
    """取上一集（<= episode_number-1）中最新一个已确认剧本的剧集。"""
    return (
        db.query(SeriesEpisode)
        .filter(
            SeriesEpisode.series_id == series_id,
            SeriesEpisode.episode_number < episode_number,
            SeriesEpisode.script_status == "confirmed",
        )
        .order_by(SeriesEpisode.episode_number.desc())
        .first()
    )


def previous_episode(
    db: Session, series_id: str, episode_number: int
) -> SeriesEpisode | None:
    return (
        db.query(SeriesEpisode)
        .filter(
            SeriesEpisode.series_id == series_id,
            SeriesEpisode.episode_number < episode_number,
        )
        .order_by(SeriesEpisode.episode_number.desc())
        .first()
    )
