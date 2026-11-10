"""系列化创作：共享资产关联与每集资产引用。

设计原则（对应需求 §5）：
- 不新增「系列角色」「续集角色」等资产类型，资产仍是现有的 character / scene / prop。
- asset 表保持原样（project_id 归属不变），系列共享通过 series_assets 关联实现，
  禁止复制资产记录，也禁止仅按名称合并两个同名资产。
- episode_asset_refs 记录每集实际使用的资产版本；后续修改资产不会污染历史集数。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import ids
from app.models.base import Base

# 资产使用类型（与现有资产类型保持一致）
EPISODE_ASSET_USAGE_TYPES: tuple[str, ...] = ("character", "scene", "prop")


class SeriesAsset(Base):
    """系列共享资产关联：把既有 asset 记录纳入某个系列。"""

    __tablename__ = "series_assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.series_asset_id)
    series_id: Mapped[str] = mapped_column(String(32), ForeignKey("series.id"), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(32), ForeignKey("asset.id"), nullable=False)
    first_episode_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("series_id", "asset_id", name="uq_series_asset_series_asset"),
    )


Index("ix_series_assets_series", SeriesAsset.series_id)
Index("ix_series_assets_asset", SeriesAsset.asset_id)


class EpisodeAssetRef(Base):
    """每集资产引用：必须记录实际使用的资产版本，避免历史集数被后续版本覆盖。"""

    __tablename__ = "episode_asset_refs"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=ids.episode_asset_ref_id
    )
    episode_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("series_episodes.id"), nullable=False
    )
    asset_id: Mapped[str] = mapped_column(String(32), ForeignKey("asset.id"), nullable=False)
    asset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    usage_type: Mapped[str] = mapped_column(String(24), nullable=False)
    is_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "episode_id",
            "asset_id",
            "asset_version",
            "usage_type",
            name="uq_episode_asset_ref_unique",
        ),
    )


Index("ix_episode_asset_refs_episode", EpisodeAssetRef.episode_id)
Index("ix_episode_asset_refs_asset", EpisodeAssetRef.asset_id)
