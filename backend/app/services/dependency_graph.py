"""聚合现有生产依赖记录的只读图，统一方向为上游依赖到下游对象。"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import (
    Asset,
    AssetPlan,
    AssetPlanDependency,
    AssetRef,
    Character,
    Composition,
    DependencyRef,
    Export,
    Job,
    JobInputRef,
    ParseVersion,
    Prop,
    Scene,
    ScriptVersion,
    Shot,
    Workflow,
)


class DependencyNodeNotFound(LookupError):
    """请求节点不存在、未接入当前项目图，或属于其他项目。"""


@dataclass(frozen=True, slots=True)
class _Resolution:
    node: dict
    owned: bool
    allowed: bool


_PROJECT_MODELS = {
    "asset": Asset,
    "asset_plan": AssetPlan,
    "character": Character,
    "composition": Composition,
    "export": Export,
    "job": Job,
    "parse_version": ParseVersion,
    "prop": Prop,
    "scene": Scene,
    "script_version": ScriptVersion,
}


def _key(node_type: str, node_id: str) -> str:
    return f"{node_type}:{node_id}"


def _node(node_type: str, row, *, exists: bool = True) -> dict:
    status = getattr(row, "status", None) if row is not None else None
    if isinstance(row, Asset):
        status = row.validity
    return {
        "key": _key(node_type, row.id if row is not None else "missing"),
        "type": node_type,
        "id": row.id if row is not None else "missing",
        "exists": exists,
        "version": getattr(row, "version", None) if row is not None else None,
        "status": status,
    }


def _missing_node(node_type: str, node_id: str) -> dict:
    return {
        "key": _key(node_type, node_id), "type": node_type, "id": node_id,
        "exists": False, "version": None, "status": None,
    }


def _cycles(nodes: dict[str, dict], edges: list[dict]) -> list[list[str]]:
    """迭代式 Kosaraju，避免长依赖链触发递归上限。"""
    adjacency = {key: set() for key in nodes}
    reverse = {key: set() for key in nodes}
    for edge in edges:
        source, target = edge["source"], edge["target"]
        if source in nodes and target in nodes:
            adjacency[source].add(target)
            reverse[target].add(source)

    visited: set[str] = set()
    finish_order: list[str] = []
    for root in sorted(nodes):
        if root in visited:
            continue
        stack = [(root, False)]
        while stack:
            current, expanded = stack.pop()
            if expanded:
                finish_order.append(current)
                continue
            if current in visited:
                continue
            visited.add(current)
            stack.append((current, True))
            for neighbor in sorted(adjacency[current], reverse=True):
                if neighbor not in visited:
                    stack.append((neighbor, False))

    components: list[list[str]] = []
    assigned: set[str] = set()
    for root in reversed(finish_order):
        if root in assigned:
            continue
        component: list[str] = []
        stack = [root]
        assigned.add(root)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in reverse[current]:
                if neighbor not in assigned:
                    assigned.add(neighbor)
                    stack.append(neighbor)
        component.sort()
        if len(component) > 1 or component[0] in adjacency[component[0]]:
            components.append(component)
    components.sort()
    return components


class _GraphBuilder:
    def __init__(self, db: Session, project_id: str):
        self.db = db
        self.project_id = project_id
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.diagnostics: list[dict] = []
        self._cache: dict[str, _Resolution] = {}
        self._diagnostic_keys: set[tuple] = set()
        self._plan_ids_by_key: dict[str, str] = {}
        self._load_project_nodes()

    def _load_project_nodes(self) -> None:
        for node_type, model in _PROJECT_MODELS.items():
            for row in self.db.query(model).filter(model.project_id == self.project_id).all():
                resolution = _Resolution(_node(node_type, row), owned=True, allowed=True)
                self._cache[resolution.node["key"]] = resolution
                if isinstance(row, AssetPlan):
                    self._plan_ids_by_key[row.asset_key] = row.id
        shots = (
            self.db.query(Shot).join(Scene, Shot.scene_id == Scene.id)
            .filter(Scene.project_id == self.project_id).all()
        )
        for row in shots:
            resolution = _Resolution(_node("shot", row), owned=True, allowed=True)
            self._cache[resolution.node["key"]] = resolution

    def resolve(self, node_type: str, node_id: str) -> _Resolution:
        key = _key(node_type, node_id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        row = None
        owner_id = None
        if node_type in _PROJECT_MODELS:
            row = self.db.get(_PROJECT_MODELS[node_type], node_id)
            owner_id = row.project_id if row is not None else None
        elif node_type == "shot":
            row = self.db.get(Shot, node_id)
            scene = self.db.get(Scene, row.scene_id) if row is not None else None
            owner_id = scene.project_id if scene is not None else None
        elif node_type == "workflow":
            row = self.db.get(Workflow, node_id)
        if row is None:
            resolution = _Resolution(_missing_node(node_type, node_id), owned=False, allowed=True)
        else:
            owned = owner_id == self.project_id
            allowed = owned or (node_type == "workflow" and owner_id is None)
            resolution = _Resolution(_node(node_type, row), owned=owned, allowed=allowed)
        self._cache[key] = resolution
        return resolution

    def _issue(self, code: str, message: str, *, edge_id: str | None = None,
               node: str | None = None, severity: str = "error", **details) -> None:
        identity = (code, edge_id, node, details.get("field"))
        if identity in self._diagnostic_keys:
            return
        self._diagnostic_keys.add(identity)
        issue = {
            "code": code, "message": message, "severity": severity,
            "edge_id": edge_id, "node": node,
        }
        issue.update(details)
        self.diagnostics.append(issue)

    def add_edge(self, record_type: str, record_id: str, *, source_type: str,
                 source_id: str, source_version: int | None, target_type: str,
                 target_id: str, role: str | None, validity: str = "current",
                 slot: str | None = None, required: bool | None = None) -> None:
        source = self.resolve(source_type, source_id)
        target = self.resolve(target_type, target_id)
        if not source.allowed or not target.allowed or not (source.owned or target.owned):
            return
        self.nodes[source.node["key"]] = source.node
        self.nodes[target.node["key"]] = target.node
        edge_id = f"{record_type}:{record_id}"
        edge = {
            "id": edge_id, "record_type": record_type, "record_id": record_id,
            "source": source.node["key"], "target": target.node["key"],
            "source_version": source_version, "role": role, "validity": validity,
            "slot": slot, "required": required,
        }
        self.edges.append(edge)
        for side, resolution in (("source", source), ("target", target)):
            if not resolution.node["exists"]:
                self._issue(
                    "orphan_reference", f"依赖{side}节点不存在", edge_id=edge_id,
                    node=resolution.node["key"], side=side,
                )
        current_version = source.node["version"]
        if source.node["exists"] and source_version is not None and current_version is not None \
                and source_version != current_version:
            self._issue(
                "stale_version", "依赖记录的上游版本已变化", edge_id=edge_id,
                node=source.node["key"], severity="stale", expected_version=source_version,
                current_version=current_version,
            )
        if source.node["type"] == "asset" and source.node["exists"] \
                and source.node["status"] != "current":
            self._issue(
                "source_invalid", "上游资产当前无效", edge_id=edge_id,
                node=source.node["key"], severity="stale", validity=source.node["status"],
            )
        if validity != "current":
            self._issue(
                "dependency_invalid", "依赖记录已失效", edge_id=edge_id,
                node=target.node["key"], severity="stale", validity=validity,
            )

    def add_missing_required_input(self, row: JobInputRef) -> None:
        target = self.resolve("job", row.job_id)
        if not target.owned:
            return
        self.nodes[target.node["key"]] = target.node
        self._issue(
            "required_input_missing", "必需任务输入没有资产引用",
            edge_id=f"job_input_ref:{row.id}", node=target.node["key"], slot=row.slot_param,
        )

    def build(self) -> dict:
        for row in self.db.query(DependencyRef).all():
            self.add_edge(
                "dependency_ref", row.id, source_type=row.source_type, source_id=row.source_id,
                source_version=row.source_version, target_type=row.target_type,
                target_id=row.target_id, role=row.dependency_role, validity=row.validity,
            )
        for row in self.db.query(AssetRef).all():
            self.add_edge(
                "asset_ref", row.id, source_type="asset", source_id=row.asset_id,
                source_version=row.source_version, target_type="shot", target_id=row.shot_id,
                role=row.role,
            )
        for row in self.db.query(AssetPlanDependency).all():
            ref_plan_id = row.ref_plan_id or self._plan_ids_by_key.get(row.ref_key)
            self.add_edge(
                "asset_plan_dependency", row.id, source_type="asset_plan",
                source_id=ref_plan_id or f"key:{row.ref_key}", source_version=None,
                target_type="asset_plan", target_id=row.plan_id, role=row.slot, slot=row.slot,
            )
        for row in self.db.query(JobInputRef).all():
            if not row.enabled:
                continue
            if row.asset_id is None:
                if row.required:
                    self.add_missing_required_input(row)
                continue
            self.add_edge(
                "job_input_ref", row.id, source_type="asset", source_id=row.asset_id,
                source_version=row.asset_version, target_type="job", target_id=row.job_id,
                role=row.role, slot=row.slot_param, required=row.required,
            )
        self.diagnostics.sort(key=lambda issue: (
            issue["code"], issue.get("edge_id") or "", issue.get("node") or "",
        ))
        return self._result(self.nodes, self.edges, self.diagnostics)

    def _result(self, nodes: dict[str, dict], edges: list[dict], diagnostics: list[dict]) -> dict:
        cycles = _cycles(nodes, edges)
        return {
            "direction": "upstream_to_downstream",
            "nodes": [nodes[key] for key in sorted(nodes)],
            "edges": sorted(edges, key=lambda edge: edge["id"]),
            "cycles": cycles,
            "diagnostics": diagnostics,
            "summary": {
                "node_count": len(nodes), "edge_count": len(edges),
                "cycle_count": len(cycles),
                "diagnostic_count": len(diagnostics),
            },
        }


def build_dependency_graph(db: Session, project_id: str) -> dict:
    return _GraphBuilder(db, project_id).build()


def traverse_dependency_graph(db: Session, project_id: str, *, node_type: str,
                              node_id: str, direction: str) -> dict:
    if direction not in ("prerequisites", "impact"):
        raise ValueError("direction 须为 prerequisites 或 impact")
    builder = _GraphBuilder(db, project_id)
    builder.build()
    root = builder.resolve(node_type, node_id)
    root_key = _key(node_type, node_id)
    if not root.allowed or (not root.owned and root_key not in builder.nodes):
        raise DependencyNodeNotFound(root_key)
    builder.nodes[root_key] = root.node

    adjacency: dict[str, set[str]] = {key: set() for key in builder.nodes}
    for edge in builder.edges:
        if direction == "impact":
            upstream, downstream = edge["source"], edge["target"]
        else:
            upstream, downstream = edge["target"], edge["source"]
        adjacency.setdefault(upstream, set()).add(downstream)
    visited = {root_key}
    queue = [root_key]
    while queue:
        current = queue.pop(0)
        for neighbor in sorted(adjacency.get(current, set())):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    nodes = {key: node for key, node in builder.nodes.items() if key in visited}
    edges = [edge for edge in builder.edges if edge["source"] in visited and edge["target"] in visited]
    edge_ids = {edge["id"] for edge in edges}
    diagnostics = [
        issue for issue in builder.diagnostics
        if issue.get("edge_id") in edge_ids or issue.get("node") in visited
    ]
    result = builder._result(nodes, edges, diagnostics)
    result.update({"root": root_key, "traversal": direction})
    return result
