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


def _workflow_model_type(wf: Workflow, caps: dict | None = None) -> str | None:
    """返回工作流的模型类型（细分大类）。

    读取顺序：库值 model_type 优先（上传/播种时按 normalize 结果写入）；
    历史记录该列为空时，用 normalize() 从节点重新推导，
    绝不对名称、描述或文件名做匹配。
    """
    if getattr(wf, "model_type", None):
        return wf.model_type
    resolved = caps if caps is not None else _slot_selection_caps(wf)
    return _classify_by_capabilities(wf.workflow_type, resolved)


def normalize_description(
    db: Session,
    raw: str | None,
    *,
    fallback: str,
    exclude_id: str | None = None,
) -> str:
    """工作流展示描述统一收敛：非空 → 长度 → 全局唯一。

    上传与 PATCH 共用同一套规则，避免两处校验漂移：
    - 未填写（None / 空白）时回落到 `fallback`（工作流名称），保证落库描述非空；
    - 用户填写时限制 20 个汉字或 40 个 ASCII 字符；
    - 唯一性校验排除自身，重复时给出可操作提示而不是 500。
    """
    user_provided = bool((raw or "").strip())
    desc = (raw or "").strip() or (fallback or "").strip()
    if not desc:
        raise errors.AppError("invalid_request", "description 不能为空", 422)
    if user_provided:
        ascii_count = sum(1 for c in desc if ord(c) < 128)
        if len(desc) - ascii_count > 20 or ascii_count > 40:
            raise errors.AppError(
                "invalid_request",
                "description 不超过 20 个汉字或 40 个英文字符",
                422,
            )
    query = db.query(Workflow).filter(Workflow.description == desc)
    if exclude_id:
        query = query.filter(Workflow.id != exclude_id)
    existing = query.first()
    if existing is not None:
        raise errors.AppError(
            "invalid_request",
            f"description 已被工作流「{existing.name}」使用，请使用不同的描述",
            422,
        )
    return desc


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
        # 模型类型：前端按它选择具体工作流（文生图 / 图生图 / 参考图生视频 / …）
        "model_type": _workflow_model_type(wf),
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


# 业务槽位 → 候选工作流的真实能力规则（选择器 / 项目默认 / 设置页共用）。
# 规则只读 capabilities.normalize() 的解析结果，不看名称、描述或文件名。
_SLOT_SELECTION_RULES: dict[str, dict] = {
    # 资产库：只允许无图片输入的纯文生图（带参考槽位会让模型自行补全主体）
    'asset_image': {'workflow_type': 'image', 'no_image_input': True},
    # 分镜图：必须能接多张参考图（角色 / 场景 / 道具）
    'storyboard_image': {
        'workflow_type': 'image',
        'required': {
            'modalities.image_input': True,
            'reference.supports_multi_reference': True,
        },
    },
    # 分镜看板：必须是 H3 多参考图视频协议（计算图含 MiniMaxH3ReferenceToVideo）
    'video': {
        'workflow_type': 'video',
        'required': {'modalities.image_input': True, 'modalities.video_output': True},
        'modes': ['multi_reference_to_video'],
    },
}

# 业务槽位 → 推荐默认工作流（按内置工作流名声明，不依赖列表排序巧合）。
# 同一 workflow_type 下 is_default 只能有一个（图片类既要有纯文生图又要有图生图），
# 因此「各页面默认选哪个」必须按槽位声明，而不是复用 is_default。
_SLOT_DEFAULT_WORKFLOWS: dict[str, str] = {
    'asset_image': 'wf_image_t2i_qwen21_turbo',
    'storyboard_image': 'wf_image_ref_qwen21_multi',
    # 「极速5秒图生视频」已按用户要求从库中移除，默认项改指标准 5 秒版
    'video': 'wf_video_h3_ref2va_std_5s',
}


def _slot_selection_caps(wf: Workflow) -> dict:
    """归一化能力；definition 缺失时退回原始 capabilities（不虚构能力）。"""
    if wf.definition:
        return normalize_capabilities(
            wf.workflow_type, wf.definition,
            capabilities=wf.capabilities or {}, trigger=wf.trigger or {},
        )
    return dict(wf.capabilities or {})


def _slot_selection_check(caps: dict, rule: dict) -> tuple[bool, list[str]]:
    """按槽位规则判定可用性，返回 (可用, 缺失说明)。"""
    from app.services.workflow.capabilities import capability_matches

    ok, missing = capability_matches(caps, rule.get('required') or {})
    reasons: list[str] = list(missing)
    if rule.get('no_image_input') and (caps.get('modalities') or {}).get('image_input'):
        ok = False
        reasons.append('该工作流声明了图片输入，不是纯文生图')
    for mode in rule.get('modes') or []:
        if mode not in (caps.get('generation_modes') or []):
            ok = False
            reasons.append(f'缺少能力：{mode}')
    return ok, reasons


@router.get('/candidates')
def list_workflow_candidates(
    slot: str = Query(..., description='业务槽位：asset_image / storyboard_image / video'),
    model_type: str | None = Query(
        None,
        description='模型类型（细分键）：image_text_to_image / image_image_to_image / '
                    'video_reference_to_video；缺省取该槽位的默认模型类型',
    ),
    db: Session = Depends(get_db),
) -> dict:
    """按业务槽位返回候选工作流（含展示描述与可用性）。

    前端选择器只用 description 作主展示文本，能否入选完全取决于后端解析出的
    真实能力，避免各页面各写一套规则、也避免按名称猜类型。
    """
    rule = _SLOT_SELECTION_RULES.get(slot)
    if rule is None:
        raise errors.AppError(
            'invalid_request',
            f'未知的业务槽位：{slot}',
            422,
            allowed_slots=sorted(_SLOT_SELECTION_RULES),
        )
    rows = (
        db.query(Workflow)
        .filter(Workflow.workflow_type == rule['workflow_type'])
        .order_by(Workflow.is_default.desc(), Workflow.is_builtin.desc(), Workflow.name)
        .all()
    )
    slot_default = _SLOT_DEFAULT_WORKFLOWS.get(slot)
    # 页面按「模型类型」选具体工作流：只在显式传值时过滤。
    # 未传时保持原有全量返回（兼容既有调用与能力检查场景），
    # 默认值由前端从 /model-types 的 slot_defaults 取，不在这里隐式兜底。
    wanted_model_type = model_type or None
    if model_type:
        allowed = _MODEL_TYPES_BY_WORKFLOW_TYPE.get(rule['workflow_type']) or ()
        if model_type not in allowed:
            raise errors.AppError(
                'invalid_request',
                f'模型类型 {model_type} 不属于 {rule["workflow_type"]}，可选：{", ".join(allowed)}',
                422,
            )
    # 模型类型切换后能力要求随之变化：资产库默认要求纯文生图，
    # 一旦显式切到「图生图」，就必须能接图片输入，否则永远选不出候选。
    if wanted_model_type == 'image_image_to_image':
        rule = {
            **rule,
            'no_image_input': False,
            'required': {**(rule.get('required') or {}), 'modalities.image_input': True},
        }
    elif wanted_model_type == 'image_text_to_image':
        rule = {**rule, 'no_image_input': True, 'required': {}}
    items: list[dict] = []
    for wf in rows:
        caps = _slot_selection_caps(wf)
        wf_model_type = _workflow_model_type(wf, caps)
        # 只列该模型类型的候选，避免下拉里混进别的细分类型
        if wanted_model_type and wf_model_type != wanted_model_type:
            continue
        usable, reasons = _slot_selection_check(caps, rule)
        items.append({
            # 推荐默认项：只有真实可用的工作流才会被推荐，避免默认值指向不可用模型
            'is_recommended': usable and wf.name == slot_default,
            'id': wf.id,
            'name': wf.name,
            'description': wf.description or wf.name,
            'workflow_type': wf.workflow_type,
            'model_type': wf_model_type,
            'version': wf.version,
            'is_default': wf.is_default,
            'is_builtin': wf.is_builtin,
            'runtime_status': wf.runtime_status,
            'generation_modes': caps.get('generation_modes') or [],
            'usable': usable,
            'missing': reasons,
        })
    return {
        'slot': slot,
        'workflow_type': rule['workflow_type'],
        'model_type': wanted_model_type,
        # 该槽位可切换的模型类型（前端渲染「模型类型」下拉用）
        'model_type_options': model_type_options(rule['workflow_type']),
        'default_workflow_name': slot_default,
        'items': items,
        'usable_count': sum(1 for i in items if i['usable']),
    }


# 设置页工作流分类：细分类型只从 capabilities.normalize() 的解析结果推导，
# 不按工作流名称、描述或文件名判断（AGENTS.md #6 / 审计 D5）。
_CLASSIFICATION_GROUPS: list[tuple[str, str, str]] = [
    ("image_text_to_image", "文生图模型", "image"),
    ("image_image_to_image", "图生图模型", "image"),
    ("video_first_last_frame", "首尾帧生视频", "video"),
    ("video_text_to_video", "文生视频", "video"),
    ("video_reference_to_video", "参考图生视频", "video"),
    ("tts", "TTS", "tts"),
    ("bgm", "BGM", "bgm"),
    ("sfx", "SFX", "sfx"),
]


def _classify_by_capabilities(workflow_type: str, caps: dict) -> str | None:
    """按归一化能力给出细分分类键；无法判定时返回 None（进「待检查」区）。"""
    modes = set(caps.get("generation_modes") or [])
    modalities = caps.get("modalities") or {}
    video = caps.get("video") or {}
    if workflow_type == "image":
        if "image_to_image" in modes or modalities.get("image_input"):
            return "image_image_to_image"
        if "text_to_image" in modes:
            return "image_text_to_image"
        return None
    if workflow_type == "video":
        # 顺序有意义：H3 多参考图协议最具体，必须先判，否则会被下面的
        # 「有图片输入 → 参考图生视频」吞掉；首尾帧再判，避免误归单参考图。
        if "multi_reference_to_video" in modes:
            return "video_reference_to_video"
        if video.get("supports_first_frame") and video.get("supports_last_frame"):
            return "video_first_last_frame"
        if modalities.get("image_input"):
            return "video_reference_to_video"
        if "text_to_video" in modes:
            return "video_text_to_video"
        return None
    if workflow_type in ("tts", "bgm", "sfx"):
        return workflow_type
    return None


# 模型类型（细分大类）定义：前端下拉与后端校验共用同一份。
# 视频目前只开放「参考图生视频」，首尾帧 / 文生视频作为预留项，前端置灰不可选。
_MODEL_TYPES_BY_WORKFLOW_TYPE: dict[str, tuple[str, ...]] = {
    'image': ('image_text_to_image', 'image_image_to_image'),
    'video': ('video_reference_to_video', 'video_first_last_frame', 'video_text_to_video'),
    'tts': ('tts',),
}

MODEL_TYPE_LABELS: dict[str, str] = {
    'image_text_to_image': '文生图模型',
    'image_image_to_image': '图生图模型',
    'video_reference_to_video': '参考图生视频',
    'video_first_last_frame': '首尾帧生视频',
    'video_text_to_video': '文生视频',
    'tts': 'TTS',
}

# 当前真正开放的模型类型（视频只支持参考图生视频，其余为预留）
_ENABLED_MODEL_TYPES: dict[str, tuple[str, ...]] = {
    'image': ('image_text_to_image', 'image_image_to_image'),
    'video': ('video_reference_to_video',),
    'tts': ('tts',),
}

# 业务槽位 -> 页面默认模型类型：资产库文生图 / 分镜图图生图 / 分镜视频参考图生视频
_SLOT_DEFAULT_MODEL_TYPES: dict[str, str] = {
    'asset_image': 'image_text_to_image',
    'storyboard_image': 'image_image_to_image',
    'video': 'video_reference_to_video',
}


def model_type_options(workflow_type: str) -> list[dict]:
    keys = _MODEL_TYPES_BY_WORKFLOW_TYPE.get(workflow_type) or ()
    enabled = _ENABLED_MODEL_TYPES.get(workflow_type) or ()
    return [
        {'value': key, 'label': MODEL_TYPE_LABELS[key], 'enabled': key in enabled}
        for key in keys
    ]


@router.get('/model-types')
def list_model_types() -> dict:
    return {
        'items': [
            {
                'workflow_type': wf_type,
                'model_types': model_type_options(wf_type),
            }
            for wf_type in _MODEL_TYPES_BY_WORKFLOW_TYPE
        ],
        'slot_defaults': dict(_SLOT_DEFAULT_MODEL_TYPES),
    }


@router.get("/classification")
def classify_workflows(db: Session = Depends(get_db)) -> dict:
    """按真实能力给工作流分组（设置页工作流 Tab 使用）。

    每组只列 workflow_type 匹配且能力落在该细分类型的记录；能力判不出来的
    记录进 uncategorized，绝不为了「看起来完整」硬塞进某类。
    """
    rows = (
        db.query(Workflow)
        .order_by(Workflow.workflow_type, Workflow.is_default.desc(), Workflow.name)
        .all()
    )
    buckets: dict[str, list[dict]] = {key: [] for key, _, _ in _CLASSIFICATION_GROUPS}
    uncategorized: list[dict] = []
    for wf in rows:
        caps = _slot_selection_caps(wf)
        # 模型类型：库值优先（上传时按真实能力写入），历史空值再从能力推导
        key = _workflow_model_type(wf, caps)
        item = {
            "id": wf.id,
            "name": wf.name,
            "description": wf.description or wf.name,
            "workflow_type": wf.workflow_type,
            "model_type": key,
            "version": wf.version,
            "is_default": wf.is_default,
            "is_builtin": wf.is_builtin,
            "runtime_status": wf.runtime_status,
            "validation_status": wf.validation_status,
            "generation_modes": caps.get("generation_modes") or [],
            "supports_multi_reference": bool((caps.get("reference") or {}).get("supports_multi_reference")),
        }
        if key is None:
            uncategorized.append(item)
        else:
            buckets[key].append(item)
    return {
        "groups": [
            {
                "key": key,
                "label": label,
                "workflow_type": wf_type,
                "items": buckets[key],
            }
            for key, label, wf_type in _CLASSIFICATION_GROUPS
        ],
        "uncategorized": uncategorized,
        "total": len(rows),
    }


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
    # 模型类型：前端「新增工作流」按类型联动下拉选择，例如 image 下的
    # image_text_to_image / image_image_to_image，video 下的 video_reference_to_video。
    # 允许省略（由 JSON 推导），一旦传入就必须与 JSON 实际能力一致。
    model_type: str | None = Form(None),
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

    # 能力声明统一由 normalize() 从节点推导后落库，不再依赖调用方是否传 capabilities，
    # 避免出现「库值为空、读取侧另算」的两套形态。
    normalized_caps = normalize_capabilities(
        workflow_type, definition, capabilities=caps_dict or {}, trigger=trigger_dict
    )
    stored_caps = {**(caps_dict or {}), **normalized_caps}

    # 参考图槽位三方一致性（2026-11-21）：capabilities 声明的 input_key 必须真实
    # 存在于 definition 节点上。漂移的后果是静默的——注入落到不存在的节点、
    # 或两个槽位抢同一张图，提示词 <Picture N> 与模型收到的图序必然错位，
    # 任务照样成功，只是画出来的人和帧全错位。上传即拒绝，不等出图才发现。
    from app.services.workflow.slot_alignment import verify_reference_alignment

    _slot_issues = verify_reference_alignment(definition, stored_caps)
    _slot_errors = [i for i in _slot_issues if i["level"] == "error"]
    if _slot_errors:
        raise errors.AppError(
            "workflow_slot_mismatch",
            "参考图槽位声明与工作流节点不一致："
            + "；".join(i["message"] for i in _slot_errors),
            422,
            issues=_slot_issues,
        )

    derived_model_type = _classify_by_capabilities(workflow_type, normalized_caps)
    allowed_model_types = [
        key for key, _label, wf_type in _CLASSIFICATION_GROUPS if wf_type == workflow_type
    ]
    if model_type:
        if model_type not in allowed_model_types:
            raise errors.AppError(
                "invalid_request",
                f"模型类型 {model_type} 不属于 {workflow_type} 类型，"
                f"可选：{', '.join(allowed_model_types) or '无'}",
                422,
            )
        # 预留能力（视频的首尾帧 / 文生视频）当前不可提交，避免建出跑不了的工作流
        enabled_model_types = _ENABLED_MODEL_TYPES.get(workflow_type) or ()
        if model_type not in enabled_model_types:
            raise errors.AppError(
                "invalid_request",
                f"模型类型「{MODEL_TYPE_LABELS.get(model_type, model_type)}」暂未开放，"
                f"当前可选：{'、'.join(MODEL_TYPE_LABELS.get(k, k) for k in enabled_model_types)}",
                422,
            )
        if derived_model_type and model_type != derived_model_type:
            raise errors.AppError(
                "workflow_format_invalid",
                f"所选模型类型（{model_type}）与工作流 JSON 实际能力（{derived_model_type}）不一致，"
                "请按 JSON 的真实节点与输入标记选择",
                422,
            )
    else:
        model_type = derived_model_type

    schema = build_schema(definition, capabilities=stored_caps, trigger=trigger_dict)

    from app.models.ids import workflow_id as new_workflow_id
    resolved_description = normalize_description(db, description, fallback=name)
    wf = Workflow(
        id=new_workflow_id(),
        name=name,
        alias=alias,
        original_filename=file.filename,
        file_hash=_definition_hash(definition),
        workflow_type=workflow_type,
        model_type=model_type,
        description=resolved_description,
        trigger=trigger_dict,
        capabilities=stored_caps,
        definition=definition,
        parsed_schema=schema,
        version=0,
        status="draft",
        validation_status="static_validated",
        is_default=False,
        description_source="user" if description else "seed",
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
    # 参考图槽位：声明 vs 工作流节点键名（与上传同源校验，供设置页就地体检）
    from app.services.workflow.slot_alignment import verify_reference_alignment

    _slot_issues = verify_reference_alignment(wf.definition, wf.capabilities)
    wf.validation_status = "static_validated" if result["valid"] and not any(
        i["level"] == "error" for i in _slot_issues
    ) else "invalid"
    if wf.current_version_id:
        row = db.get(WorkflowVersion, wf.current_version_id)
        if row is not None:
            row.validation_status = wf.validation_status
    db.commit()
    return {
        "valid": result["valid"] and not any(i["level"] == "error" for i in _slot_issues),
        "issues": issue_messages(result) + [
            f"[{i['code']}] {i['message']}" for i in _slot_issues
        ],
        "errors": result["errors"] + [i for i in _slot_issues if i["level"] == "error"],
        "warnings": result["warnings"] + [i for i in _slot_issues if i["level"] == "warning"],
        "slot_alignment": _slot_issues,
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
        # 描述不允许为空：未填写时回落到工作流名称（§4.2 第 4 条）
        wf.description = normalize_description(
            db, desc, fallback=wf.name, exclude_id=wf.id,
        )
        if desc is not None:
            wf.description_source = "user"
    if "is_default" in payload:
        if payload["is_default"]:
            db.query(Workflow).filter(
                Workflow.workflow_type == wf.workflow_type,
                Workflow.is_default == True,  # noqa: E712
                Workflow.id != wf.id,
            ).update({"is_default": False})
        wf.is_default = bool(payload["is_default"])
    if "model_type" in payload:
        caps = _slot_selection_caps(wf)
        allowed = _MODEL_TYPES_BY_WORKFLOW_TYPE.get(wf.workflow_type) or ()
        enabled = _ENABLED_MODEL_TYPES.get(wf.workflow_type) or ()
        value = payload["model_type"] or None
        if value is not None and value not in allowed:
            raise errors.AppError(
                "invalid_request",
                f"模型类型必须是 {', '.join(allowed)} 之一",
                422,
            )
        if value is not None and value not in enabled:
            raise errors.AppError(
                "invalid_request",
                f"模型类型「{MODEL_TYPE_LABELS.get(value, value)}」暂未开放",
                422,
            )
        derived = _classify_by_capabilities(wf.workflow_type, caps)
        if value and derived and derived != value:
            raise errors.AppError(
                "invalid_request",
                f"所选模型类型（{MODEL_TYPE_LABELS.get(value, value)}）与工作流 JSON "
                f"实际能力（{MODEL_TYPE_LABELS.get(derived, derived)}）不一致",
                422,
            )
        wf.model_type = value or derived
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
