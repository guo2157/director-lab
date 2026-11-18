"""工作流管理接口：API.md §7 字段级契约。

- GET 列表支持 workflow_type / 关键词过滤，返回 is_default 与 used_by_jobs
- POST 上传 multipart JSON ≤10MB：先静态校验（API Format + 标记 + 输出），再构建 Schema
- POST validate 仅静态校验不访问 ComfyUI，返回可定位到节点/字段的结构化 issue
- GET {id}/schema 返回前端渲染用 Schema（剥离 node 绑定）
- PATCH 更新生成新版本号，设默认时同类型其他取消默认
- DELETE 被默认或任务引用时返回 409 workflow_in_use

P1 改动（docs/plan/workflow-dynamic-audit.md D1/D2/D7/D9/D14）：
- 解析改用 services/workflow.schema_builder（原来读画布字段 widgets_values，对 API Format 恒为空）
- 校验改用 services/workflow.validator（原来检查 nodes/links，对 API Format 恒报错）
- 落库 alias / original_filename / file_hash
- 上传前做节点数、参数数、节点 ID 合法性、连线完整性、画布残留键检查
"""

import hashlib
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Job, Workflow
from app.services import errors
from app.services.workflow import build_schema, public_schema, validate_definition
from app.services.workflow.validator import issue_messages

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

_WORKFLOW_TYPES = ("image", "video", "tts", "bgm", "sfx")


def _definition_hash(definition: dict) -> str:
    """定义指纹：键排序序列化后取 SHA-256，用于溯源与「内容是否变化」判断。"""
    canonical = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_dict(wf: Workflow, db: Session) -> dict:
    used_by_jobs = db.query(Job).filter(Job.workflow_id == wf.id).count()
    schema = wf.parsed_schema or {}
    return {
        "id": wf.id,
        "name": wf.name,
        "alias": wf.alias,
        "original_filename": wf.original_filename,
        "file_hash": wf.file_hash,
        "workflow_type": wf.workflow_type,
        "description": wf.description,
        "trigger": wf.trigger,
        "capabilities": wf.capabilities,
        "version": wf.version,
        "definition": wf.definition,
        "parsed_schema": wf.parsed_schema,
        "schema_summary": {
            "params": len(schema.get("params") or {}),
            "outputs": [o.get("key") if isinstance(o, dict) else o
                        for o in (schema.get("outputs") or [])],
            "errors": len(schema.get("errors") or []),
            "warnings": len(schema.get("warnings") or []),
        },
        "is_default": wf.is_default,
        "is_builtin": wf.is_builtin,
        "used_by_jobs": used_by_jobs,
        "created_at": wf.created_at.isoformat() if getattr(wf, "created_at", None) else None,
        "updated_at": wf.updated_at.isoformat() if getattr(wf, "updated_at", None) else None,
    }


@router.get("")
def list_workflows(
    workflow_type: str | None = Query(None),
    q: str | None = Query(None, description="按 name / alias / original_filename 模糊搜索"),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = db.query(Workflow)
    if workflow_type:
        query = query.filter(Workflow.workflow_type == workflow_type)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Workflow.name.like(like))
            | (Workflow.alias.like(like))
            | (Workflow.original_filename.like(like))
        )
    wfs = query.order_by(Workflow.workflow_type, Workflow.name).all()
    return [_to_dict(w, db) for w in wfs]


@router.get("/{workflow_id}")
def get_workflow(workflow_id: str, db: Session = Depends(get_db)) -> dict:
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    return _to_dict(wf, db)


@router.get("/{workflow_id}/schema")
def get_workflow_schema(
    workflow_id: str,
    version: int | None = Query(None, description="预留：指定历史版本 Schema（P2 版本表上线后生效）"),
    db: Session = Depends(get_db),
) -> dict:
    """前端渲染用 Schema：参数为数组、按 order 排序、不含 node 绑定。"""
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    schema = wf.parsed_schema
    if not schema or not (schema.get("params") or schema.get("outputs")):
        # 历史数据（seed 前上传的脏记录）按当前定义重新构建一次，不写库
        schema = build_schema(wf.definition, capabilities=wf.capabilities, trigger=wf.trigger)
    return {
        "workflow_id": wf.id,
        "workflow_name": wf.name,
        "workflow_type": wf.workflow_type,
        "version": wf.version,
        **public_schema(schema),
    }


@router.post("", status_code=201)
async def upload_workflow(
    name: str = Form(...),
    workflow_type: str = Form(...),
    alias: str | None = Form(None),
    description: str | None = Form(None),
    trigger: str | None = Form(None),
    capabilities: str | None = Form(None),
    is_default: bool = Form(False),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise errors.AppError("workflow_format_invalid", "文件超过 10MB 限制", 422)
    if workflow_type not in _WORKFLOW_TYPES:
        raise errors.AppError(
            "invalid_request",
            f"workflow_type 必须是 {', '.join(_WORKFLOW_TYPES)} 之一（枚举隔离，见 WORKFLOW.md §7.3）",
            422,
        )
    try:
        definition = json.loads(content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise errors.AppError("workflow_format_invalid", f"JSON 解析失败: {e}", 422)

    try:
        trigger_dict = json.loads(trigger) if trigger else None
        caps_dict = json.loads(capabilities) if capabilities else None
    except json.JSONDecodeError as e:
        raise errors.AppError("invalid_request", f"trigger / capabilities 不是合法 JSON: {e}", 422)

    result = validate_definition(definition, trigger=trigger_dict)
    if not result["valid"]:
        raise errors.AppError(
            "workflow_format_invalid",
            "工作流校验未通过：" + "；".join(issue_messages(result)),
            422,
            issues=result["errors"],
        )

    schema = build_schema(definition, capabilities=caps_dict, trigger=trigger_dict)

    from app.models.ids import workflow_id
    wf = Workflow(
        id=workflow_id(),
        name=name,
        alias=alias,
        original_filename=file.filename,
        file_hash=_definition_hash(definition),
        workflow_type=workflow_type,
        description=description,
        trigger=trigger_dict,
        capabilities=caps_dict,
        definition=definition,
        parsed_schema=schema,
        is_default=bool(is_default),
    )
    if is_default:
        db.query(Workflow).filter(
            Workflow.workflow_type == workflow_type,
            Workflow.is_default == True,  # noqa: E712
        ).update({"is_default": False})
    db.add(wf)
    db.commit()
    db.refresh(wf)
    out = _to_dict(wf, db)
    out["warnings"] = schema.get("warnings") or []
    return out


@router.post("/{workflow_id}/validate")
def validate_workflow(workflow_id: str, db: Session = Depends(get_db)) -> dict:
    """仅静态校验，不访问 ComfyUI：API Format / 标记 / 连线 / 输出 / 安全上限。"""
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    result = validate_definition(wf.definition, trigger=wf.trigger)
    return {
        "valid": result["valid"],
        "issues": issue_messages(result),
        "errors": result["errors"],
        "warnings": result["warnings"],
        "schema_summary": {
            "params": len((wf.parsed_schema or {}).get("params") or {}),
            "outputs": [o.get("key") if isinstance(o, dict) else o
                        for o in ((wf.parsed_schema or {}).get("outputs") or [])],
        },
    }


@router.patch("/{workflow_id}")
def update_workflow(
    workflow_id: str,
    payload: dict,
    db: Session = Depends(get_db),
) -> dict:
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)

    if "name" in payload:
        wf.name = payload["name"]
    if "alias" in payload:
        wf.alias = payload["alias"]
    if "description" in payload:
        wf.description = payload["description"]
    if "is_default" in payload:
        if payload["is_default"]:
            db.query(Workflow).filter(
                Workflow.workflow_type == wf.workflow_type,
                Workflow.is_default == True,  # noqa: E712
                Workflow.id != wf.id,
            ).update({"is_default": False})
        wf.is_default = bool(payload["is_default"])
    if "definition" in payload:
        result = validate_definition(payload["definition"], trigger=wf.trigger)
        if not result["valid"]:
            raise errors.AppError(
                "workflow_format_invalid",
                "工作流校验未通过：" + "；".join(issue_messages(result)),
                422,
                issues=result["errors"],
            )
        wf.definition = payload["definition"]
        wf.file_hash = _definition_hash(payload["definition"])
        wf.version += 1
        wf.parsed_schema = build_schema(
            payload["definition"], capabilities=wf.capabilities, trigger=wf.trigger
        )

    db.commit()
    db.refresh(wf)
    return _to_dict(wf, db)


@router.delete("/{workflow_id}", status_code=204)
def delete_workflow(workflow_id: str, db: Session = Depends(get_db)):
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    if wf.is_builtin:
        raise errors.AppError("workflow_in_use", "系统内置工作流，禁止删除", 409)
    if wf.is_default:
        raise errors.AppError("workflow_in_use", "工作流为默认，禁止删除", 409)
    job_count = db.query(Job).filter(Job.workflow_id == workflow_id).count()
    if job_count > 0:
        raise errors.AppError("workflow_in_use", f"工作流被 {job_count} 个任务引用，禁止删除", 409)
    db.delete(wf)
    db.commit()
    return Response(status_code=204)
