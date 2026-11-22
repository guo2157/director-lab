"""依赖失效标记（Phase 3 / 6.4）：把用户确认的失效写入 ``DependencyRef.validity``。

设计约定：
- 显式写：只在用户确认后调用；不自动重生成、不删除产物、不改资产有效性。
- 只标记「来源落后于当前版本」的 current 边；已失效的边幂等跳过。
- 资产按同名版本序列处理：引用旧版本资产的边全部标记为 stale。
- 非资产来源（如镜头）没有版本概念：标记其全部 current 边。
- 只影响依赖图与影响预演的 stale 诊断，不改变任何产物的可用性。
"""
from sqlalchemy.orm import Session

from app.models import Asset, DependencyRef, Scene, Shot
from app.services.dependency_graph import DependencyNodeNotFound

INVALIDATE_ALGORITHM = "dependency-invalidate-v1"

_WRITE_POLICY = {
    "auto_regenerate": False,
    "mark_stale": True,
    "delete_existing": False,
    "writes": True,
    "note": "仅写依赖失效标记；重做仍由用户显式触发",
}


def _resolve_source(db: Session, project_id: str, node_type: str,
                    node_id: str) -> tuple[list[str], int | None]:
    """校验节点归属并返回来源 id 列表与当前版本（非资产来源为 None）。"""
    if node_type == "asset":
        asset = db.get(Asset, node_id)
        if asset is None or asset.project_id != project_id:
            raise DependencyNodeNotFound(node_type, node_id)
        lineage = (
            db.query(Asset)
            .filter(Asset.project_id == project_id, Asset.name == asset.name)
            .all()
        )
        return [row.id for row in lineage], max(row.version for row in lineage)
    if node_type == "shot":
        shot = db.get(Shot, node_id)
        scene = db.get(Scene, shot.scene_id) if shot is not None else None
        if shot is None or scene is None or scene.project_id != project_id:
            raise DependencyNodeNotFound(node_type, node_id)
        return [node_id], None
    raise DependencyNodeNotFound(node_type, node_id)


def invalidate_dependency_refs(db: Session, project_id: str, *,
                               node_type: str, node_id: str) -> dict:
    """把指定来源下游仍引用旧版本的依赖边标记为 stale；返回标记明细。"""
    source_ids, current_version = _resolve_source(db, project_id, node_type, node_id)
    refs = (
        db.query(DependencyRef)
        .filter(
            DependencyRef.source_type == node_type,
            DependencyRef.source_id.in_(source_ids),
        )
        .order_by(DependencyRef.id)
        .all()
    )

    marked: list[dict] = []
    already_stale = 0
    kept_current = 0
    for ref in refs:
        if ref.validity != "current":
            already_stale += 1
            continue
        if current_version is not None and ref.source_version == current_version:
            kept_current += 1
            continue
        ref.validity = "stale"
        marked.append({
            "edge_id": ref.id,
            "source_type": ref.source_type,
            "source_id": ref.source_id,
            "source_version": ref.source_version,
            "target_type": ref.target_type,
            "target_id": ref.target_id,
            "role": ref.dependency_role,
        })
    if marked:
        db.commit()

    return {
        "algorithm": INVALIDATE_ALGORITHM,
        "policy": dict(_WRITE_POLICY),
        "root": {
            "type": node_type,
            "id": node_id,
            "version": current_version,
            "source_ids": source_ids,
        },
        "marked": marked,
        "summary": {
            "marked": len(marked),
            "already_stale": already_stale,
            "kept_current": kept_current,
        },
    }
