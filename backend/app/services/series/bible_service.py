"""系列设定（Series Bible）版本管理。

纪律：
- 更新设定时只新增版本，绝不覆盖历史版本。
- 同一时刻只有一个 is_active=True 的版本，由同一事务内切换。
- 不自动重写历史剧本，影响分析交由上层展示。
"""
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import SeriesBibleVersion
from app.models import ids
from app.services import errors

from .series_service import get_series


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def list_bible_versions(db: Session, series_id: str) -> list[SeriesBibleVersion]:
    get_series(db, series_id)
    return (
        db.query(SeriesBibleVersion)
        .filter(SeriesBibleVersion.series_id == series_id)
        .order_by(SeriesBibleVersion.version.desc())
        .all()
    )


def get_bible_version(db: Session, series_id: str, version: int) -> SeriesBibleVersion:
    get_series(db, series_id)
    row = (
        db.query(SeriesBibleVersion)
        .filter(
            SeriesBibleVersion.series_id == series_id,
            SeriesBibleVersion.version == version,
        )
        .first()
    )
    if row is None:
        raise errors.series_bible_not_found()
    return row


def save_bible_version(
    db: Session, series_id: str, *, content: dict, change_summary: str | None, created_by: str
) -> SeriesBibleVersion:
    """保存新版本：递增版本号并激活，旧版本保留但置为非激活。"""
    get_series(db, series_id)
    latest = (
        db.query(func.max(SeriesBibleVersion.version))
        .filter(SeriesBibleVersion.series_id == series_id)
        .scalar()
    )
    next_version = int(latest or 0) + 1
    db.query(SeriesBibleVersion).filter(
        SeriesBibleVersion.series_id == series_id,
        SeriesBibleVersion.is_active.is_(True),
    ).update({SeriesBibleVersion.is_active: False}, synchronize_session=False)

    row = SeriesBibleVersion(
        id=ids.series_bible_version_id(),
        series_id=series_id,
        version=next_version,
        content=content,
        change_summary=change_summary,
        created_by=created_by,
        created_at=_now(),
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def bible_payload_for_context(db: Session, series_id: str) -> dict[str, Any] | None:
    """供上下文构建使用：只读取当前激活版本；缺失时返回 None，由调用方决定是否失败。"""
    row = (
        db.query(SeriesBibleVersion)
        .filter(
            SeriesBibleVersion.series_id == series_id,
            SeriesBibleVersion.is_active.is_(True),
        )
        .order_by(SeriesBibleVersion.version.desc())
        .first()
    )
    if row is None:
        return None
    return {"version": row.version, "content": row.content or {}}
