"""工作流管理接口：API.md §7 字段级契约。
- GET 列表支持 workflow_type 过滤，返回 is_default 与 used_by_jobs
- POST 上传 multipart JSON ≤10MB，返回 parsed_schema
- POST validate 仅静态校验不访问 ComfyUI
- PATCH 更新生成新版本，设默认时同类型其他取消默认
- DELETE 被默认或任务引用时返回 409 workflow_in_use
"""
import json
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.models import Job, Workflow
from app.services import errors

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def _to_dict(wf: Workflow, db: Session) -> dict:
    used_by_jobs = db.query(Job).filter(Job.workflow_id == wf.id).count()
    return {
        "id": wf.id,
        "name": wf.name,
        "workflow_type": wf.workflow_type,
        "description": wf.description,
        "trigger": wf.trigger,
        "capabilities": wf.capabilities,
        "version": wf.version,
        "definition": wf.definition,
        "parsed_schema": wf.parsed_schema,
        "is_default": wf.is_default,
        "used_by_jobs": used_by_jobs,
        "created_at": wf.created_at.isoformat() if hasattr(wf, 'created_at') and wf.created_at else None,
        "updated_at": wf.updated_at.isoformat() if hasattr(wf, 'updated_at') and wf.updated_at else None,
    }


@router.get("")
def list_workflows(
    workflow_type: str | None = Query(None),
    db: Session = Depends(get_db),
) -> list[dict]:
    q = db.query(Workflow)
    if workflow_type:
        q = q.filter(Workflow.workflow_type == workflow_type)
    wfs = q.order_by(Workflow.workflow_type, Workflow.name).all()
    return [_to_dict(w, db) for w in wfs]


@router.post("", status_code=201)
async def upload_workflow(
    name: str = Form(...),
    workflow_type: str = Form(...),
    description: str | None = Form(None),
    trigger: str | None = Form(None),
    capabilities: str | None = Form(None),
    file: bytes = File(...),
    db: Session = Depends(get_db),
) -> dict:
    if len(file) > MAX_FILE_SIZE:
        raise errors.AppError("workflow_format_invalid", "文件超过 10MB 限制", 422)
    try:
        definition = json.loads(file.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise errors.AppError("workflow_format_invalid", f"JSON 解析失败: {e}", 422)
    if not isinstance(definition, dict):
        raise errors.AppError("workflow_format_invalid", "工作流定义必须是 JSON 对象", 422)

    trigger_dict = json.loads(trigger) if trigger else None
    caps_dict = json.loads(capabilities) if capabilities else None

    parsed_schema = _parse_schema(definition)

    from app.models.ids import workflow_id
    wf = Workflow(
        id=workflow_id(),
        name=name,
        workflow_type=workflow_type,
        description=description,
        trigger=trigger_dict,
        capabilities=caps_dict,
        definition=definition,
        parsed_schema=parsed_schema,
        is_default=False,
    )
    db.add(wf)
    db.commit()
    db.refresh(wf)
    return _to_dict(wf, db)


@router.post("/{workflow_id}/validate")
def validate_workflow(workflow_id: str, db: Session = Depends(get_db)) -> dict:
    wf = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if not wf:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    issues = []
    if not wf.definition.get("nodes"):
        issues.append("缺少 nodes 定义")
    if not wf.definition.get("links") and wf.definition.get("nodes"):
        issues.append("缺少 links 定义")
    if not wf.parsed_schema or not wf.parsed_schema.get("outputs"):
        issues.append("缺少输出标记")
    return {"valid": len(issues) == 0, "issues": issues}


@router.patch("/{workflow_id}")
def update_workflow(
    workflow_id: str,
    payload: dict,
    db: Session = Depends(get_db),
) -> dict:
    wf = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if not wf:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)

    if "name" in payload:
        wf.name = payload["name"]
    if "is_default" in payload:
        if payload["is_default"]:
            db.query(Workflow).filter(
                Workflow.workflow_type == wf.workflow_type,
                Workflow.is_default == True,
                Workflow.id != wf.id,
            ).update({"is_default": False})
        wf.is_default = payload["is_default"]
    if "definition" in payload:
        wf.definition = payload["definition"]
        wf.version += 1
        wf.parsed_schema = _parse_schema(payload["definition"])
    if "description" in payload:
        wf.description = payload["description"]

    db.commit()
    db.refresh(wf)
    return _to_dict(wf, db)


@router.delete("/{workflow_id}", status_code=204)
def delete_workflow(workflow_id: str, db: Session = Depends(get_db)):
    wf = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if not wf:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    if wf.is_default:
        raise errors.AppError("workflow_in_use", "工作流为默认，禁止删除", 409)
    job_count = db.query(Job).filter(Job.workflow_id == workflow_id).count()
    if job_count > 0:
        raise errors.AppError("workflow_in_use", f"工作流被 {job_count} 个任务引用，禁止删除", 409)
    db.delete(wf)
    db.commit()
    return Response(status_code=204)


def _parse_schema(definition: dict) -> dict:
    """从工作流定义中提取参数与输出标记。"""
    params = []
    outputs = []
    nodes = definition.get("nodes", [])
    for node in nodes:
        if not isinstance(node, dict):
            continue
        widgets = node.get("widgets_values", [])
        title = node.get("title", node.get("type", ""))
        if "$param" in str(widgets) or "$output" in str(widgets):
            for w in widgets:
                if isinstance(w, str) and "$param:" in w:
                    key = w.split("$param:")[1].split(":")[0].strip()
                    params.append({"key": key, "label": key, "type": "string", "required": True})
                if isinstance(w, str) and "$output:" in w:
                    key = w.split("$output:")[1].split(":")[0].strip()
                    outputs.append({"key": key, "label": key, "type": "string"})
    return {"params": params, "outputs": outputs}
