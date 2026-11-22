"""依赖变更影响预演（Phase 3 切片）：只读计算上游变化波及的下游对象。

设计约定：
- 只读：不写依赖记录、不标记失效、不重生成、不删除任何旧产物。
- 资产按「类型 + 名称」识别同一血缘，比较边缘记录的上游版本与血缘当前
  版本，判断下游是使用旧版本还是当前版本。
- 下游产物已有输出但输入过期 → ``redo_required``；尚未生成 → ``not_generated``；
  镜头绑定指向旧资产 → ``rebind_required``；依赖链上游已需重做时向下传播。
- 结果只描述影响范围与建议动作，是否重做由用户触发。
"""
from collections import defaultdict, deque

from sqlalchemy.orm import Session

from app.models import Asset, Scene, Shot
from app.services.dependency_graph import (
    _PROJECT_MODELS,
    DependencyNodeNotFound,
    build_dependency_graph,
)

IMPACT_ALGORITHM = "dependency-impact-v1"
_ARTIFACT_TYPES = frozenset({"asset", "job", "composition", "export"})
_REPLAN_STATUSES = frozenset({"redo_required", "rebind_required", "stale_reference"})


def _load_root_node(db: Session, project_id: str, node_type: str,
                    node_id: str) -> dict | None:
    """读取根节点并校验项目归属；不存在或跨项目返回 None。"""
    if node_type == "shot":
        row = db.get(Shot, node_id)
        scene = db.get(Scene, row.scene_id) if row is not None else None
        if row is None or scene is None or scene.project_id != project_id:
            return None
        status = getattr(row, "status", None)
    else:
        model = _PROJECT_MODELS.get(node_type)
        row = db.get(model, node_id) if model is not None else None
        if row is None or row.project_id != project_id:
            return None
        status = row.validity if isinstance(row, Asset) else getattr(row, "status", None)
    return {
        "key": f"{node_type}:{node_id}", "type": node_type, "id": node_id,
        "exists": True, "version": getattr(row, "version", None), "status": status,
    }


def _asset_context(db: Session, project_id: str) -> tuple[dict, dict]:
    """返回（资产行索引，血缘当前版本）；血缘键为（资产类型，名称）。"""
    assets = db.query(Asset).filter(Asset.project_id == project_id).all()
    by_id = {asset.id: asset for asset in assets}
    lineage_current: dict[tuple[str, str], int] = {}
    for asset in assets:
        lineage = (asset.asset_type, asset.name)
        lineage_current[lineage] = max(lineage_current.get(lineage, 0), asset.version)
    return by_id, lineage_current


def _current_version(node: dict, asset_by_id: dict,
                     lineage_current: dict) -> int | None:
    if node["type"] == "asset":
        asset = asset_by_id.get(node["id"])
        if asset is not None:
            return lineage_current.get((asset.asset_type, asset.name))
    return node["version"]


def _has_output(node: dict, asset_by_id: dict) -> bool:
    if node["type"] == "asset":
        asset = asset_by_id.get(node["id"])
        return bool(asset is not None and asset.validity == "current" and asset.file_path)
    return node["status"] == "succeeded"


def _classify(node_type: str, has_output: bool, stale: bool, unknown: bool) -> str:
    if node_type in _ARTIFACT_TYPES:
        if not has_output:
            return "not_generated"
        if stale:
            return "redo_required"
        return "unknown_version" if unknown else "up_to_date"
    if node_type == "shot":
        if stale:
            return "rebind_required"
        return "unknown_version" if unknown else "up_to_date"
    if stale:
        return "stale_reference"
    return "unknown_version" if unknown else "up_to_date"


def _bfs_affected(graph: dict, start_keys: list[str]) -> dict[str, dict]:
    outgoing: dict[str, list[dict]] = defaultdict(list)
    for edge in graph["edges"]:
        outgoing[edge["source"]].append(edge)
    order: list[str] = []
    depth: dict[str, int] = {}
    via: dict[str, dict] = {}
    queue = deque((key, 0) for key in start_keys)
    for key in start_keys:
        depth[key] = 0
    while queue:
        current, current_depth = queue.popleft()
        if current_depth > 0:
            order.append(current)
        for edge in outgoing[current]:
            target = edge["target"]
            if target in depth:
                continue
            depth[target] = current_depth + 1
            via[target] = edge
            queue.append((target, current_depth + 1))
    return {"order": order, "depth": depth, "via": via}


def _path_for(key: str, roots: set[str], via: dict[str, dict]) -> list[str]:
    path = [key]
    while path[-1] not in roots and path[-1] in via:
        path.append(via[path[-1]]["source"])
    return list(reversed(path))


def _collect_affected(graph: dict, nodes: dict[str, dict], start_keys: list[str],
                      roots: set[str], asset_by_id: dict,
                      lineage_current: dict) -> tuple[list[dict], dict[str, str]]:
    """从起点集合向下游 BFS，逐个下游节点判定变更状态。

    ``roots`` 是「变化源」，本身不进入影响清单；返回（影响清单，状态索引），
    状态索引供下游节点判断「上游是否已需重做」。
    """
    traversal = _bfs_affected(graph, start_keys)
    incoming: dict[str, list[dict]] = defaultdict(list)
    for edge in graph["edges"]:
        incoming[edge["target"]].append(edge)

    affected: list[dict] = []
    status_by_key: dict[str, str] = {}
    for key in traversal["order"]:
        node = nodes.get(key)
        if node is None or key in roots:
            continue
        stale_inputs: list[dict] = []
        stale = False
        unknown = False
        for edge in incoming.get(key, []):
            source_node = nodes.get(edge["source"])
            if source_node is None:
                continue
            current_version = _current_version(source_node, asset_by_id, lineage_current)
            if edge["validity"] != "current":
                stale = True
                stale_inputs.append({
                    "edge_id": edge["id"], "source": edge["source"],
                    "source_version": edge["source_version"],
                    "current_version": current_version,
                    "reason": "dependency_invalid",
                })
            elif edge["source_version"] is None:
                unknown = True
            elif current_version is not None and edge["source_version"] != current_version:
                stale = True
                stale_inputs.append({
                    "edge_id": edge["id"], "source": edge["source"],
                    "source_version": edge["source_version"],
                    "current_version": current_version,
                    "reason": "source_version_changed",
                })
            if status_by_key.get(edge["source"]) in _REPLAN_STATUSES:
                stale = True
                stale_inputs.append({
                    "edge_id": edge["id"], "source": edge["source"],
                    "source_version": edge["source_version"],
                    "current_version": current_version,
                    "reason": "upstream_needs_redo",
                })
        has_output = _has_output(node, asset_by_id)
        change_status = _classify(node["type"], has_output, stale, unknown)
        status_by_key[key] = change_status
        via = traversal["via"].get(key)
        affected.append({
            "key": key,
            "type": node["type"],
            "id": node["id"],
            "version": node["version"],
            "status": node["status"],
            "depth": traversal["depth"].get(key, 0),
            "path": _path_for(key, roots, traversal["via"]),
            "via": {
                "edge_id": via["id"], "record_type": via["record_type"],
                "role": via["role"],
            } if via is not None else None,
            "change_status": change_status,
            "has_output": has_output,
            "requires_regeneration": change_status == "redo_required",
            "stale_inputs": stale_inputs,
        })

    affected.sort(key=lambda item: (item["depth"], item["key"]))
    return affected, status_by_key


def _summarize(affected: list[dict]) -> dict:
    return {
        "affected_count": len(affected),
        "redo_required": sum(item["change_status"] == "redo_required" for item in affected),
        "rebind_required": sum(item["change_status"] == "rebind_required" for item in affected),
        "not_generated": sum(item["change_status"] == "not_generated" for item in affected),
        "up_to_date": sum(item["change_status"] == "up_to_date" for item in affected),
        "unknown_version": sum(item["change_status"] == "unknown_version" for item in affected),
    }


_READ_ONLY_POLICY = {
    "auto_regenerate": False,
    "mark_stale": False,
    "delete_existing": False,
    "writes": False,
    "note": "仅预演影响范围；重做、标记失效与重生成必须由用户确认后单独触发",
}


def preview_dependency_impact(db: Session, project_id: str, *, node_type: str,
                              node_id: str) -> dict:
    """计算上游节点变化的下游影响；不写库、不触发重生成。"""
    graph = build_dependency_graph(db, project_id)
    nodes = {node["key"]: node for node in graph["nodes"]}
    root_key = f"{node_type}:{node_id}"
    root_node = nodes.get(root_key)
    if root_node is None:
        root_node = _load_root_node(db, project_id, node_type, node_id)
        if root_node is None:
            raise DependencyNodeNotFound(root_key)

    asset_by_id, lineage_current = _asset_context(db, project_id)
    root_asset = asset_by_id.get(node_id) if node_type == "asset" else None
    start_keys = [root_key]
    lineage_versions: list[int] = []
    if root_asset is not None:
        lineage = (root_asset.asset_type, root_asset.name)
        lineage_versions = sorted({
            asset.version for asset in asset_by_id.values()
            if (asset.asset_type, asset.name) == lineage
        })
        start_keys = [
            f"asset:{asset.id}" for asset in asset_by_id.values()
            if (asset.asset_type, asset.name) == lineage and f"asset:{asset.id}" in nodes
        ]
        if root_key not in start_keys:
            start_keys.append(root_key)

    affected, _ = _collect_affected(
        graph, nodes, start_keys, set(start_keys), asset_by_id, lineage_current,
    )
    affected_keys = {item["key"] for item in affected}
    return {
        "algorithm": IMPACT_ALGORITHM,
        "root": {
            "key": root_key,
            "type": node_type,
            "id": node_id,
            "version": root_node["version"],
            "lineage_versions": lineage_versions,
        },
        "policy": dict(_READ_ONLY_POLICY),
        "affected": affected,
        "diagnostics": [
            issue for issue in graph["diagnostics"] if issue.get("node") in affected_keys
        ],
        "summary": _summarize(affected),
    }


def preview_project_impact(db: Session, project_id: str) -> dict:
    """项目级总览：以「所有已过期依赖的上游」为变化源，一次性算出全项目待处理对象。

    与单节点预演共用同一套分类逻辑，同样只读。用于看板/资产库直接回答
    「当前有哪些东西因为上游变化需要重做或重绑」，不必逐个节点点开查询。
    """
    graph = build_dependency_graph(db, project_id)
    nodes = {node["key"]: node for node in graph["nodes"]}
    asset_by_id, lineage_current = _asset_context(db, project_id)

    stale_roots: set[str] = set()
    markable_by_root: dict[str, int] = {}
    for edge in graph["edges"]:
        source_node = nodes.get(edge["source"])
        if source_node is None:
            continue
        current_version = _current_version(source_node, asset_by_id, lineage_current)
        if edge["validity"] != "current":
            stale_roots.add(edge["source"])
        elif edge["source_version"] is not None and current_version is not None \
                and edge["source_version"] != current_version:
            stale_roots.add(edge["source"])
            # 可写失效标记的边：仍是 current，但引用了旧版本
            markable_by_root[edge["source"]] = markable_by_root.get(edge["source"], 0) + 1

    affected, _ = _collect_affected(
        graph, nodes, sorted(stale_roots), stale_roots, asset_by_id, lineage_current,
    )
    affected_keys = {item["key"] for item in affected}
    return {
        "algorithm": IMPACT_ALGORITHM,
        "policy": dict(_READ_ONLY_POLICY),
        "changed_roots": [
            {
                "key": key, "type": nodes[key]["type"], "id": nodes[key]["id"],
                "version": nodes[key]["version"],
                "markable_edge_count": markable_by_root.get(key, 0),
            }
            for key in sorted(stale_roots) if key in nodes
        ],
        "affected": affected,
        "diagnostics": [
            issue for issue in graph["diagnostics"]
            if issue.get("node") in affected_keys or issue.get("node") in stale_roots
        ],
        "summary": _summarize(affected),
    }
