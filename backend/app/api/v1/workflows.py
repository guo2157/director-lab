"""工作流管理接口：API.md §7 字段级契约 + P1/P2 动态工作流扩展。

- GET 列表：workflow_type / 关键词过滤，返回 is_default、used_by_jobs、能力摘要
- POST 上传：先静态校验（API Format + 标记 + 输出 + 安全上限）再落库，并生成 v1 版本
- GET {id}/schema：前端渲染用 Schema（剥离 node 绑定），可按 version 取历史版本
- GET {id}/capabilities：归一化能力（CapabilityRegistry），不按名称推断
- GET {id}/versions、POST {id}/versions、POST .../publish、POST .../rollback：不可变版本
- PATCH：定义变化生成**新版本**（不再原地覆盖），并发布
- DELETE：被默认 / 内置 / 任务引用时 409 workflow_in_use

P1 修复（审计 D1/D2/D7/D9/D14）：解析与校验不再读画布字段。
P2 修复（审计 D3）：定义变更生成不可变版本，历史任务可复现。
"""

import hashlib
import json

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Job, Workflow
from app.models.production import WorkflowVersion
from app.services import errors
from app.services.workflow import build_schema, public_schema, validate_definition
from app.services.workflow import runtime_checker
from app.services.workflow import versions as versions_svc
from app.services.workflow.capabilities import normalize as normalize_capabilities
from app.services.workflow.validator import issue_messages

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

_WORKFLOW_TYPES = ("image", "video", "tts", "bgm", "sfx")


def _definition_hash(definition: dict) -> str:
    canonical = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_dict(wf: Workflow, db: Session) -> dict:
    used_by_jobs = db.query(Job).filter(Job.workflow_id == wf.id).count()
    schema = wf.parsed_schema or {}
    version_count = (
        db.query(WorkflowVersion).filter(WorkflowVersion.workflow_id == wf.id).count()
    )
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
        "current_version_id": wf.current_version_id,
        "description_source": wf.description_source,
        "status": wf.status,
        "validation_status": wf.validation_status,
        "runtime_status": wf.runtime_status,
        "version_count": version_count,
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


@router.post("/resolve")
def resolve_workflow(payload: dict, db: Session = Depends(get_db)) -> dict:
    """按业务槽位解析工作流（G1，v2 计划 §11.1/§11.2）。

    选择优先级：显式 workflow_id → 项目默认 → 系统默认 → 内置 → 能力匹配。
    显式指定或项目默认不满足能力时**返回 422 + 缺失能力列表，不回退**（§24 第 16 条）。
    """
    from app.services.workflow import matcher

    slot = payload.get("slot")
    if not isinstance(slot, str) or not slot:
        raise errors.AppError("invalid_request", "slot 必填（image/storyboard_image/video/tts/bgm/sfx）", 422)
    required = payload.get("required")
    if required is not None and not isinstance(required, dict):
        raise errors.AppError("invalid_request", "required 必须是对象", 422)
    res = matcher.resolve(
        db,
        slot=slot,
        project_id=payload.get("project_id"),
        workflow_id=payload.get("workflow_id"),
        extra_required=required,
    )
    if res.workflow is None:
        res.require(slot=slot)  # 抛 422，带 missing_capabilities
    wf = res.workflow
    assert wf is not None
    caps = matcher._caps_of(wf)
    return {
        "workflow": {
            "id": wf.id,
            "name": wf.name,
            "alias": wf.alias,
            "workflow_type": wf.workflow_type,
            "version": wf.version,
            "is_default": wf.is_default,
            "runtime_status": wf.runtime_status,
            "capabilities": caps,
        },
        "source": res.source,
        "required": required or {},
        "considered_workflows": res.considered,
    }


@router.get("/{workflow_id}")
def get_workflow(workflow_id: str, db: Session = Depends(get_db)) -> dict:
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    return _to_dict(wf, db)


@router.get("/{workflow_id}/schema")
def get_workflow_schema(
    workflow_id: str,
    version: int | None = Query(None, description="指定历史版本号；缺省取当前生效版本"),
    db: Session = Depends(get_db),
) -> dict:
    """前端渲染用 Schema：参数按 order 排序、不含 node 绑定、按模态分组。"""
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    if version is not None:
        row = versions_svc.get_version(db, workflow_id, version)
        schema = row.parsed_schema
        resolved_version = row.version
    else:
        schema = wf.parsed_schema
        resolved_version = wf.version
    if not schema or not (schema.get("params") or schema.get("outputs")):
        # 历史脏数据：按当前定义现算一次，不写库
        schema = build_schema(wf.definition, capabilities=wf.capabilities, trigger=wf.trigger)
    return {
        "workflow_id": wf.id,
        "workflow_name": wf.name,
        "workflow_type": wf.workflow_type,
        "version": resolved_version,
        **public_schema(schema),
    }


@router.get("/{workflow_id}/capabilities")
def get_workflow_capabilities(workflow_id: str, db: Session = Depends(get_db)) -> dict:
    """归一化能力（modalities / audio / video / reference / generation_modes）。"""
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    return {
        "workflow_id": wf.id,
        "workflow_name": wf.name,
        "version": wf.version,
        **normalize_capabilities(wf.workflow_type, wf.definition,
                                 capabilities=wf.capabilities, trigger=wf.trigger),
    }


@router.get("/{workflow_id}/versions")
def list_versions(workflow_id: str, db: Session = Depends(get_db)) -> list[dict]:
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    rows = (
        db.query(WorkflowVersion)
        .filter(WorkflowVersion.workflow_id == workflow_id)
        .order_by(WorkflowVersion.version.desc())
        .all()
    )
    return [versions_svc.version_to_dict(r) for r in rows]


@router.post("/{workflow_id}/versions", status_code=201)
def create_version(workflow_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """创建草稿版本（默认不发布）。payload: {definition, changelog?, publish?}"""
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    definition = payload.get("definition")
    if not isinstance(definition, dict):
        raise errors.AppError("invalid_request", "definition 必须是 JSON 对象", 422)
    row = versions_svc.create_version(
        db, wf, definition,
        changelog=payload.get("changelog"),
        publish=bool(payload.get("publish")),
    )
    db.commit()
    db.refresh(row)
    return versions_svc.version_to_dict(row, with_definition=True)


@router.post("/{workflow_id}/versions/{version_id}/publish")
def publish_version(workflow_id: str, version_id: str, db: Session = Depends(get_db)) -> dict:
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    row = versions_svc.get_version_by_id(db, version_id)
    if row.workflow_id != workflow_id:
        raise errors.AppError("invalid_request", "版本不属于该工作流", 422)
    if row.validation_status == "invalid":
        raise errors.AppError(
            "workflow_format_invalid", "该版本静态校验未通过，禁止发布", 422,
        )
    versions_svc.publish_version(db, wf, row)
    db.commit()
    db.refresh(wf)
    return {"workflow": _to_dict(wf, db), "published_version": versions_svc.version_to_dict(row)}


@router.post("/{workflow_id}/versions/{version_id}/rollback")
def rollback_version(workflow_id: str, version_id: str, db: Session = Depends(get_db)) -> dict:
    """回滚＝把指针指回历史版本，不复制、不覆盖历史内容。"""
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    row = versions_svc.get_version_by_id(db, version_id)
    if row.workflow_id != workflow_id:
        raise errors.AppError("invalid_request", "版本不属于该工作流", 422)
    versions_svc.publish_version(db, wf, row)
    db.commit()
    db.refresh(wf)
    return {"workflow": _to_dict(wf, db), "current_version": versions_svc.version_to_dict(row)}


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
            f"workflow_type 必须是 {', '.join(_WORKFLOW_TYPES)} 之一（枚举隔离见 WORKFLOW.md §7.3、AGENTS.md #6）",
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

    from app.models.ids import workflow_id as new_workflow_id
    wf = Workflow(
        id=new_workflow_id(),
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
        version=0,
        status="draft",
        validation_status="static_validated",
        is_default=False,
        description_source="user",
    )
    if is_default:
        db.query(Workflow).filter(
            Workflow.workflow_type == workflow_type,
            Workflow.is_default == True,  # noqa: E712
        ).update({"is_default": False})
        wf.is_default = True
    db.add(wf)
    db.flush()
    # 上传即产出 v1 并发布（工作流中心的「上传 → 校验 → 可用」最短路径）
    versions_svc.create_version(db, wf, definition, changelog="上传导入", publish=True)
    db.commit()
    db.refresh(wf)
    out = _to_dict(wf, db)
    out["warnings"] = (wf.parsed_schema or {}).get("warnings") or []
    return out


@router.post("/{workflow_id}/validate")
def validate_workflow(workflow_id: str, db: Session = Depends(get_db)) -> dict:
    """仅静态校验，不访问 ComfyUI。"""
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    result = validate_definition(wf.definition, trigger=wf.trigger)
    wf.validation_status = "static_validated" if result["valid"] else "invalid"
    if wf.current_version_id:
        row = db.get(WorkflowVersion, wf.current_version_id)
        if row is not None:
            row.validation_status = wf.validation_status
    db.commit()
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


@router.post("/{workflow_id}/check-runtime")
def check_runtime(
    workflow_id: str,
    instance: str | None = Query(None, description="指定 ComfyUI 实例 base_url；缺省用默认实例"),
    db: Session = Depends(get_db),
) -> dict:
    """依赖检查：节点 class_type 与模型文件是否在目标实例就绪（不提交任务）。"""
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    result = runtime_checker.check_definition(
        wf.definition, wf.capabilities, base_url=instance,
    )
    wf.runtime_status = result["runtime_status"]
    if wf.current_version_id:
        row = db.get(WorkflowVersion, wf.current_version_id)
        if row is not None:
            row.runtime_status = wf.runtime_status
            row.dependency_snapshot = row.dependency_snapshot or {}
    db.commit()
    return {"workflow_id": wf.id, "version": wf.version, **result}


@router.post("/{workflow_id}/test", status_code=202)
def test_workflow(
    workflow_id: str,
    instance: str | None = Query(None, description="指定 ComfyUI 实例 base_url"),
    db: Session = Depends(get_db),
) -> dict:
    """真实测试生成：用 Schema 默认值编译最小 prompt 并提交（不落资产、不占任务表）。

    必需媒体槽位无法自造文件时返回 422，并列出缺哪些槽位——不伪造「测试通过」。
    """
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)
    if wf.validation_status == "invalid":
        raise errors.AppError("workflow_format_invalid", "工作流静态校验未通过，禁止测试", 422)
    result = runtime_checker.submit_test(
        wf.definition, wf.capabilities, trigger=wf.trigger, base_url=instance,
    )
    if not result.get("submitted"):
        raise errors.AppError(
            result.get("reason") or "workflow_test_failed",
            result.get("message") or "测试提交失败",
            422,
            blocking_slots=result.get("blocking_slots") or [],
        )
    return {"workflow_id": wf.id, "version": wf.version, **result}


@router.get("/test/{prompt_id}")
def get_test_status(
    prompt_id: str,
    instance: str | None = Query(None, description="指定 ComfyUI 实例 base_url"),
) -> dict:
    """查询测试任务状态与产物清单（只读，不下载文件）。"""
    return runtime_checker.test_status(prompt_id, base_url=instance)


@router.patch("/{workflow_id}")
def update_workflow(
    workflow_id: str,
    payload: dict,
    db: Session = Depends(get_db),
) -> dict:
    """元信息就地更新；定义变化生成**新不可变版本**并发布（不再原地覆盖）。"""
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        raise errors.AppError("workflow_not_found", "工作流不存在", 404)

    if "name" in payload:
        wf.name = payload["name"]
    if "alias" in payload:
        wf.alias = payload["alias"]
    if "description" in payload:
        desc = payload["description"]
        if desc is not None and not isinstance(desc, str):
            raise errors.AppError("invalid_request", "description 必须是字符串", 422)
        if desc is not None:
            desc_stripped = desc.strip()
            if not desc_stripped:
                raise errors.AppError("invalid_request", "description 不能为空", 422)
            # 长度校验：≤20 汉字 或 ≤40 ASCII 字符
            char_count = len(desc_stripped)
            ascii_count = sum(1 for c in desc_stripped if ord(c) < 128)
            non_ascii_count = char_count - ascii_count
            if non_ascii_count > 20 or ascii_count > 40:
                raise errors.AppError(
                    "invalid_request",
                    "description 不超过 20 个汉字或 40 个英文字符",
                    422,
                )
            # 唯一性校验：排除自身
            existing = (
                db.query(Workflow)
                .filter(Workflow.description == desc_stripped, Workflow.id != wf.id)
                .first()
            )
            if existing:
                raise errors.AppError(
                    "invalid_request",
                    f"description 已被工作流「{existing.name}」使用，请使用不同的描述",
                    422,
                )
            wf.description = desc_stripped
            wf.description_source = "user"
        else:
            wf.description = None
            wf.description_source = None
    if "is_default" in payload:
        if payload["is_default"]:
            db.query(Workflow).filter(
                Workflow.workflow_type == wf.workflow_type,
                Workflow.is_default == True,  # noqa: E712
                Workflow.id != wf.id,
            ).update({"is_default": False})
        wf.is_default = bool(payload["is_default"])
    if "definition" in payload:
        definition = payload["definition"]
        if not isinstance(definition, dict):
            raise errors.AppError("invalid_request", "definition 必须是 JSON 对象", 422)
        versions_svc.create_version(
            db, wf, definition,
            changelog=payload.get("changelog") or "PATCH 更新定义",
            publish=True,
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
    db.query(WorkflowVersion).filter(WorkflowVersion.workflow_id == workflow_id).delete(
        synchronize_session=False
    )
    db.delete(wf)
    db.commit()
    return Response(status_code=204)
