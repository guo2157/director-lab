"""共享资产登记服务（Asset Registry）。

纪律（对应需求 §5.6）：
1. 不新增资产类型，复用现有 asset 表（character / scene / prop 语义由 asset_type 承载）。
2. 系列共享通过 series_assets 关联实现，禁止复制资产记录。
3. 每集必须记录实际使用的资产版本（episode_asset_refs.asset_version），
   否则未来资产升级会污染历史集数。
4. 只按名称匹配一律视为不可靠：同名资产仅返回提示，不自动合并。
5. 不删除、不覆盖任何已被历史集数使用的资产。
"""
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import Asset, EpisodeAssetRef, SeriesAsset, SeriesEpisode
from app.models import ids
from app.models.series_asset import EPISODE_ASSET_USAGE_TYPES
from app.services import errors

from .series_service import get_series


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def register_series_asset(
    db: Session, *, series_id: str, asset_id: str, first_episode_id: str | None = None
) -> SeriesAsset:
    get_series(db, series_id)
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise errors.biz_error(code="asset_not_found", http=404, message="资产不存在")
    existing = (
        db.query(SeriesAsset)
        .filter(
            SeriesAsset.series_id == series_id,
            SeriesAsset.asset_id == asset_id,
        )
        .first()
    )
    if existing is not None:
        return existing
    row = SeriesAsset(
        id=ids.series_asset_id(),
        series_id=series_id,
        asset_id=asset_id,
        first_episode_id=first_episode_id,
        created_at=_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_series_assets(
    db: Session, series_id: str, *, asset_type: str | None = None
) -> list[tuple[SeriesAsset, Asset]]:
    get_series(db, series_id)
    q = (
        db.query(SeriesAsset, Asset)
        .join(Asset, SeriesAsset.asset_id == Asset.id)
        .filter(SeriesAsset.series_id == series_id)
    )
    if asset_type:
        q = q.filter(Asset.asset_type == asset_type)
    return q.order_by(Asset.asset_type.asc(), Asset.name.asc()).all()


def register_episode_asset_ref(
    db: Session,
    *,
    episode: SeriesEpisode,
    asset_id: str,
    asset_version: int,
    usage_type: str,
    is_required: bool = False,
    source: str | None = None,
) -> EpisodeAssetRef:
    """登记某集实际使用的资产版本；同一 (集, 资产, 版本, 用途) 幂等。"""
    if usage_type not in EPISODE_ASSET_USAGE_TYPES:
        raise errors.invalid_request(
            f"usage_type 须为 {EPISODE_ASSET_USAGE_TYPES} 之一"
        )
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise errors.biz_error(code="asset_not_found", http=404, message="资产不存在")
    if asset_version != asset.version:
        raise errors.invalid_request(
            f"资产 {asset_id} 当前版本为 {asset.version}，与提交的 {asset_version} 不一致"
        )
    existing = (
        db.query(EpisodeAssetRef)
        .filter(
            EpisodeAssetRef.episode_id == episode.id,
            EpisodeAssetRef.asset_id == asset_id,
            EpisodeAssetRef.asset_version == asset_version,
            EpisodeAssetRef.usage_type == usage_type,
        )
        .first()
    )
    if existing is not None:
        return existing
    row = EpisodeAssetRef(
        id=ids.episode_asset_ref_id(),
        episode_id=episode.id,
        asset_id=asset_id,
        asset_version=asset_version,
        usage_type=usage_type,
        is_required=is_required,
        source=source,
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def list_episode_assets(db: Session, episode_id: str) -> list[dict[str, Any]]:
    rows = (
        db.query(EpisodeAssetRef, Asset)
        .join(Asset, EpisodeAssetRef.asset_id == Asset.id)
        .filter(EpisodeAssetRef.episode_id == episode_id)
        .order_by(EpisodeAssetRef.usage_type.asc(), Asset.name.asc())
        .all()
    )
    result: list[dict[str, Any]] = []
    for ref, asset in rows:
        result.append(
            {
                "id": ref.id,
                "episode_id": ref.episode_id,
                "asset_id": ref.asset_id,
                "asset_version": ref.asset_version,
                "usage_type": ref.usage_type,
                "is_required": ref.is_required,
                "source": ref.source,
                "created_at": ref.created_at,
                "asset_name": asset.name,
                "asset_type": asset.asset_type,
                "current_version": asset.version,
                "version_drift": ref.asset_version != asset.version,
            }
        )
    return result


def find_name_candidates(
    db: Session, *, series_id: str, name: str, asset_type: str | None = None
) -> list[dict[str, Any]]:
    """按名称给出候选，仅作提示：名称相同不代表同一资产，必须人工确认。"""
    q = (
        db.query(SeriesAsset, Asset)
        .join(Asset, SeriesAsset.asset_id == Asset.id)
        .filter(
            SeriesAsset.series_id == series_id,
            Asset.name == name,
        )
    )
    if asset_type:
        q = q.filter(Asset.asset_type == asset_type)
    return [
        {
            "asset_id": asset.id,
            "name": asset.name,
            "asset_type": asset.asset_type,
            "version": asset.version,
            "match_reason": "name_equal",
            "requires_manual_confirm": True,
        }
        for _, asset in q.all()
    ]


def usage_history(db: Session, *, series_id: str, asset_id: str) -> list[dict[str, Any]]:
    """某资产在系列内的使用历史：用于资产详情页展示使用集数与版本。"""
    rows = (
        db.query(EpisodeAssetRef, SeriesEpisode)
        .join(SeriesEpisode, EpisodeAssetRef.episode_id == SeriesEpisode.id)
        .filter(
            SeriesEpisode.series_id == series_id,
            EpisodeAssetRef.asset_id == asset_id,
        )
        .order_by(SeriesEpisode.episode_number.asc())
        .all()
    )
    return [
        {
            "episode_id": ep.id,
            "episode_number": ep.episode_number,
            "asset_version": ref.asset_version,
            "usage_type": ref.usage_type,
        }
        for ref, ep in rows
    ]
