"""系列化创作：系列、剧集、系列设定（Series Bible）与叙事剧本版本。

模块边界（重要）：
- `models/continuity.py` + `services/continuity.py` 解决的是「跨集视频首尾帧连续性」，与本文件无关。
- 本文件的 `episode` 指系列维度剧集，与 `shot.episode`、`episode_continuity.episode`
  （项目内部集号）语义不同，禁止互相替代。
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import ids
from app.models.base import Base, TimestampMixin

# 剧本状态（与生产状态独立管理）
SCRIPT_STATUS: tuple[str, ...] = (
    "draft",
    "analyzing",
    "awaiting_review",
    "confirmed",
    "superseded",
    "rejected",
)

# 生产状态
PRODUCTION_STATUS: tuple[str, ...] = (
    "not_started",
    "queued",
    "running",
    "reviewing",
    "completed",
    "failed",
    "cancelled",
)

# 剧情连续性状态
EPISODE_CONTINUITY_STATUS: tuple[str, ...] = (
    "not_checked",
    "checking",
    "consistent",
    "warning",
    "conflict",
    "needs_review",
)

# 系列状态
SERIES_STATUS: tuple[str, ...] = ("active", "archived")


class Series(TimestampMixin, Base):
    """系列：长期故事管理实体。V1 只维护一条正式主线（mainline_id 固定 main）。"""

    __tablename__ = "series"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.series_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_image: Mapped[str | None] = mapped_column(String(500), nullable=True)
    genre: Mapped[str | None] = mapped_column(String(64), nullable=True)
    style: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_audience: Mapped[str | None] = mapped_column(String(64), nullable=True)
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="zh-CN", server_default="zh-CN"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    mainline_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default="main", server_default="main"
    )
    # 最新确认剧本的剧集 与 最新完成视频生产的剧集，必须分开记录
    latest_confirmed_episode_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    latest_completed_episode_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 剧情连续性状态版本（乐观锁 + 快照版本号）
    state_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


Index("ix_series_status", Series.status)


class SeriesEpisode(TimestampMixin, Base):
    """剧集：系列下的单集，绑定一个独立生产工作空间 Project。

    兼容说明：历史项目可能一个 Project 覆盖多集，因此 `project_id` 可空，
    且用 `project_episode_no` 记录其在 Project 内的真实集号；新剧集固定为 1。
    """

    __tablename__ = "series_episodes"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=ids.series_episode_id
    )
    series_id: Mapped[str] = mapped_column(String(32), ForeignKey("series.id"), nullable=False)
    project_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("project.id"), nullable=True
    )
    project_episode_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    synopsis: Mapped[str | None] = mapped_column(Text, nullable=True)
    script_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", server_default="draft"
    )
    production_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="not_started", server_default="not_started"
    )
    script_current_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confirmed_script_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    context_manifest_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    continuity_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="not_checked", server_default="not_checked"
    )
    mainline_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default="main", server_default="main"
    )

    __table_args__ = (
        UniqueConstraint("series_id", "episode_number", name="uq_series_episode_number"),
    )


Index(
    "ix_series_episodes_series",
    SeriesEpisode.series_id,
    SeriesEpisode.episode_number,
)
Index(
    "ix_series_episodes_status",
    SeriesEpisode.series_id,
    SeriesEpisode.script_status,
    SeriesEpisode.production_status,
)


class SeriesBibleVersion(Base):
    """系列设定版本：只新增，不覆盖历史版本。"""

    __tablename__ = "series_bible_versions"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=ids.series_bible_version_id
    )
    series_id: Mapped[str] = mapped_column(String(32), ForeignKey("series.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    __table_args__ = (
        UniqueConstraint("series_id", "version", name="uq_series_bible_series_version"),
    )


Index("ix_series_bible_series_active", SeriesBibleVersion.series_id, SeriesBibleVersion.is_active)


class SeriesScriptVersion(Base):
    """叙事剧本版本：不可变快照，确认后才允许入账正式剧情。"""

    __tablename__ = "series_script_versions"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=ids.series_script_version_id
    )
    episode_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("series_episodes.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    synopsis: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_context_manifest_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prompt_template_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    validation_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    continuity_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", server_default="draft"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("episode_id", "version", name="uq_series_script_episode_version"),
    )


Index(
    "ix_series_script_episode_status",
    SeriesScriptVersion.episode_id,
    SeriesScriptVersion.status,
)
