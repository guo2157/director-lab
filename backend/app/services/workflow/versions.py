"""工作流版本管理（P2）：不可变版本 + 发布 / 回滚。

规则（对应 docs/API.md:431 与 docs/plan/workflow-dynamic-audit.md D3）：
- 每次定义变化生成一条新的 `workflow_version` 行，**旧行不再修改**
- `publish` 把 `workflow.current_version_id` 指向该版本，并同步 workflow 上的
  definition / parsed_schema / file_hash / version 冗余字段（供老代码路径继续读）
- `rollback` 即「发布一个历史版本」，不复制、不覆盖任何历史内容
- 历史任务通过 `(workflow_id, job.workflow_version)` 精确定位定义，天然不受后续发布影响
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.production import Workflow, WorkflowVersion
from app.services import errors
from app.services.workflow.schema_builder import build_schema
from app.services.workflow.validator import issue_messages, validate_definition


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(definition: dict) -> str:
    import hashlib
    import json
    canonical = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def latest_version_no(db: Session, workflow_id: str) -> int:
    row = (
        db.query(WorkflowVersion)
        .filter(WorkflowVersion.workflow_id == workflow_id)
        .order_by(WorkflowVersion.version.desc())
        .first()
    )
    return row.version if row else 0


def get_version(db: Session, workflow_id: str, version: int) -> WorkflowVersion:
    row = (
        db.query(WorkflowVersion)
        .filter(WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version)
        .first()
    )
    if row is None:
        raise errors.AppError("workflow_version_not_found", f"工作流版本 v{version} 不存在", 404)
    return row


def get_version_by_id(db: Session, version_id: str) -> WorkflowVersion:
    row = db.get(WorkflowVersion, version_id)
    if row is None:
        raise errors.AppError("workflow_version_not_found", "工作流版本不存在", 404)
    return row


def create_version(db: Session, wf: Workflow, definition: dict, *, changelog: str | None = None,
                   publish: bool = False) -> WorkflowVersion:
    """写入一条新版本（不可变）。definition 会先做静态校验。"""
    result = validate_definition(definition, trigger=wf.trigger)
    version_no = latest_version_no(db, wf.id) + 1
    schema = build_schema(definition, capabilities=wf.capabilities, trigger=wf.trigger)
    row = WorkflowVersion(
        id=f"wfv_{__import__('uuid').uuid4().hex[:12]}",
        workflow_id=wf.id,
        version=version_no,
        definition=definition,
        file_hash=_hash(definition),
        parsed_schema=schema,
        schema_version=schema.get("schema_version"),
        parser_version=schema.get("parser_version"),
        validation_status="static_validated" if result["valid"] else "invalid",
        runtime_status="unchecked",
        dependency_snapshot=_dependency_snapshot(definition),
        changelog=changelog,
        is_current=False,
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    if publish:
        if not result["valid"]:
            raise errors.AppError(
                "workflow_format_invalid",
                "版本校验未通过，禁止发布：" + "；".join(issue_messages(result)),
                422,
                issues=result["errors"],
            )
        publish_version(db, wf, row)
    return row


def publish_version(db: Session, wf: Workflow, row: WorkflowVersion) -> WorkflowVersion:
    """把指定版本设为当前生效版本（发布 / 回滚同一实现）。"""
    db.query(WorkflowVersion).filter(
        WorkflowVersion.workflow_id == wf.id,
        WorkflowVersion.is_current == True,  # noqa: E712
    ).update({"is_current": False}, synchronize_session=False)
    row.is_current = True
    wf.current_version_id = row.id
    wf.version = row.version
    wf.definition = row.definition
    wf.parsed_schema = row.parsed_schema
    wf.file_hash = row.file_hash
    wf.status = "published"
    wf.validation_status = row.validation_status
    db.flush()
    return row


def _dependency_snapshot(definition: dict) -> dict:
    """记录节点类型清单与模型文件名引用，供运行环境检查（P3）比对。"""
    node_types: dict[str, int] = {}
    model_files: set[str] = set()
    for node in (definition or {}).values():
        if not isinstance(node, dict):
            continue
        ctype = node.get("class_type")
        if isinstance(ctype, str):
            node_types[ctype] = node_types.get(ctype, 0) + 1
        for value in (node.get("inputs") or {}).values():
            if isinstance(value, str) and value.lower().endswith(
                (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".onnx")
            ):
                model_files.add(value)
    return {
        "node_types": dict(sorted(node_types.items())),
        "model_files": sorted(model_files),
        "captured_at": _now().isoformat(),
    }


def version_to_dict(row: WorkflowVersion, *, with_definition: bool = False) -> dict:
    out = {
        "id": row.id,
        "workflow_id": row.workflow_id,
        "version": row.version,
        "file_hash": row.file_hash,
        "schema_version": row.schema_version,
        "parser_version": row.parser_version,
        "validation_status": row.validation_status,
        "runtime_status": row.runtime_status,
        "changelog": row.changelog,
        "is_current": row.is_current,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "dependency_snapshot": row.dependency_snapshot,
        "schema_summary": {
            "params": len((row.parsed_schema or {}).get("params") or {}),
            "outputs": [o.get("key") if isinstance(o, dict) else o
                        for o in ((row.parsed_schema or {}).get("outputs") or [])],
        },
    }
    if with_definition:
        out["definition"] = row.definition
    return out
