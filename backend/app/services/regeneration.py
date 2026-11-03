"""多图参考重新生成工作台 V1：配置继承、快照冻结、参考图校验与结构化落库。

设计约定：
- 默认模式『沿用原配置，只修改提示词』从用户指定的基础版本加载原始生成配置，
  不从当前资产最新版本重新拼装。
- 任务创建即冻结输入快照；再次修改必须创建新任务，不复用旧快照。
- 参考图能力以工作流 capabilities.reference_slots 为唯一事实来源，
  不臆造槽位、不假设所有模型共用同一参考图格式。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Asset, Job, JobInputRef, Workflow, ids
from app.services import errors

MODE_INHERIT_PROMPT_ONLY = "inherit_prompt_only"
MODE_MODIFY_REFS = "modify_refs"
MODE_FULL_RECONFIGURE = "full_reconfigure"

VALID_MODES = (MODE_INHERIT_PROMPT_ONLY, MODE_MODIFY_REFS, MODE_FULL_RECONFIGURE)

# 系统保留槽位：由生成流程按镜头/连续性规则写入
SYSTEM_SLOTS = ("ref_image", "ref_prev_frame", "first_frame", "end_frame")

# 帧约束语义槽位：与普通参考图必须区分，避免把尾帧参考说成首帧约束
FRAME_CONSTRAINT_SLOTS = ("first_frame", "end_frame", "ref_prev_frame")

ROLE_BY_SLOT = {
    "ref_identity": "character",
    "ref_character_2": "character",
    "ref_scene": "scene",
    "ref_prop": "prop",
    "ref_image": "keyframe",
    "ref_prev_frame": "prev_frame",
    "first_frame": "first_frame",
    "end_frame": "end_frame",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _slot_semantic(param: str) -> str:
    return "frame_constraint" if param in FRAME_CONSTRAINT_SLOTS else "reference"


def _slot_role(param: str) -> str | None:
    return ROLE_BY_SLOT.get(param)


def normalize_reference_slots(capabilities: dict | None) -> list[dict]:
    """归一化工作流 capabilities.reference_slots。

    兼容两种真实声明格式：
    - 字符串数组：["ref_identity", "ref_character_2"]
    - 对象数组：[{"param": "ref_identity", "input_key": "...", "optional": true}]
    仅以工作流真实声明为准，不补造槽位。
    """
    raw = (capabilities or {}).get("reference_slots") or []
    slots: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            param = item.get("param")
            input_key = item.get("input_key")
            optional = bool(item.get("optional", True))
        else:
            param = item
            input_key = None
            optional = True
        if not param:
            continue
        slots.append({
            "param": param,
            "input_key": input_key,
            "required": not optional,
            "semantic": _slot_semantic(param),
            "role": _slot_role(param),
            "system": param in SYSTEM_SLOTS,
        })
    return slots


def workflow_binds_param(wf: Workflow | None, param: str) -> bool:
    """判断工作流定义是否真实绑定了某参数标记，用于决定是否提供该编辑项。"""
    if wf is None or not wf.definition:
        return False
    from app.services.workflow_inject import parse_workflow
    try:
        parsed = parse_workflow(wf.definition)
    except Exception:  # noqa: BLE001
        return False
    return param in parsed.params and not parsed.errors


def _asset_brief(asset: Asset | None) -> dict | None:
    if asset is None:
        return None
    return {
        "asset_id": asset.id,
        "asset_type": asset.asset_type,
        "name": asset.name,
        "version": asset.version,
        "file_path": asset.file_path,
        "source_job_id": asset.source_job_id,
    }


def _refs_from_records(db: Session, job_id: str) -> list[dict]:
    rows = (
        db.query(JobInputRef)
        .filter(JobInputRef.job_id == job_id)
        .order_by(JobInputRef.seq)
        .all()
    )
    return [
        {
            "slot_param": r.slot_param,
            "input_key": r.input_key,
            "asset_id": r.asset_id,
            "asset_version": r.asset_version,
            "asset_type": r.asset_type,
            "role": r.role,
            "required": bool(r.required),
            "file_path": r.file_path,
            "semantic": r.semantic,
            "enabled": bool(r.enabled),
            "source": "job_input_ref",
        }
        for r in rows
    ]


def _refs_from_snapshot(db: Session, snapshot: dict, media_params: dict | None = None) -> list[dict]:
    """历史任务兼容：从 {槽位: 文件路径} 反查资产。

    参考图在任务里的存放位置有两种，都要识别，否则真实参考图会整体丢失：
    - snapshot['injected_media']：视频任务分支写入
    - params['__media__']：生成提交时的媒体参数，分镜图任务走这里
    """
    injected = snapshot.get("injected_media") or media_params or {}
    if not isinstance(injected, dict):
        injected = {}
    refs: list[dict] = []
    for slot, path in injected.items():
        asset = None
        if path:
            asset = (
                db.query(Asset)
                .filter(Asset.file_path == path)
                .order_by(Asset.version.desc())
                .first()
            )
        refs.append({
            "slot_param": slot,
            "input_key": None,
            "asset_id": asset.id if asset else None,
            "asset_version": asset.version if asset else None,
            "asset_type": asset.asset_type if asset else None,
            "role": _slot_role(slot),
            "required": False,
            "file_path": path,
            "semantic": _slot_semantic(slot),
            "enabled": bool(path),
            "source": "legacy_snapshot",
        })
    return refs


def build_base_config(db: Session, asset: Asset) -> dict:
    """从指定基础版本反查原始生成配置。

    配置不完整时明确标记 snapshot_complete=False 并给出原因，
    调用方必须据此提示『历史配置不完整』，禁止用默认值或最新资产替代。
    """
    job = db.get(Job, asset.source_job_id) if asset.source_job_id else None
    snapshot = dict(job.input_snapshot or {}) if job else {}
    params = dict(snapshot.get("params") or {})
    if job is not None and job.params:
        params = {**params, **dict(job.params)}

    wf = db.get(Workflow, job.workflow_id) if job is not None and job.workflow_id else None
    slots = normalize_reference_slots(wf.capabilities if wf else None)

    missing: list[str] = []
    if job is None:
        missing.append("source_job_missing")
    if not snapshot:
        missing.append("input_snapshot_missing")
    if wf is None:
        missing.append("workflow_missing")
    if not params:
        missing.append("params_missing")

    refs = _refs_from_records(db, job.id) if job is not None else []
    if not refs:
        refs = _refs_from_snapshot(db, snapshot, params.get("__media__"))

    slot_index = {s["param"]: s for s in slots}
    for ref in refs:
        slot = slot_index.get(ref["slot_param"])
        if slot is not None:
            if not ref.get("input_key"):
                ref["input_key"] = slot["input_key"]
            ref["required"] = bool(ref.get("required")) or slot["required"]
            ref["semantic"] = slot["semantic"]
            ref["role"] = ref.get("role") or slot["role"]

    prompt = params.get("prompt") or ""
    negative = params.get("negative") or ""
    model_params = {
        k: v
        for k, v in params.items()
        if not str(k).startswith("__") and k not in ("prompt", "negative")
    }

    return {
        "base": _asset_brief(asset),
        "snapshot_complete": not missing,
        "incomplete_reasons": missing,
        "mode": MODE_INHERIT_PROMPT_ONLY,
        "prompt": {"rendered": prompt, "editable": prompt},
        "negative_prompt": {
            "rendered": negative,
            "supported": bool(negative) or workflow_binds_param(wf, "negative"),
        },
        "workflow": {
            "id": wf.id if wf else None,
            "name": wf.name if wf else None,
            "type": wf.workflow_type if wf else None,
            "version": wf.version if wf else None,
        },
        "available_slots": slots,
        "refs": refs,
        "model_params": model_params,
        "locked": {"refs": True, "params": True, "workflow": True},
    }


def _type_matches(role: str, asset_type: str | None) -> bool:
    """参考图资产类型与槽位用途的匹配判定，避免类型错配静默通过。"""
    if not asset_type:
        return True
    if role in ("character", "scene", "prop"):
        return asset_type in (role, "keyframe", "asset")
    if role == "keyframe":
        return asset_type in ("keyframe", "asset")
    if role == "prev_frame":
        return asset_type in ("clip", "keyframe", "frame", "asset")
    if role in ("first_frame", "end_frame"):
        return asset_type in ("clip", "keyframe", "frame", "asset")
    return True


def _resolve_asset_ref(db: Session, ref: dict, slot: dict) -> Asset:
    asset_id = ref.get("asset_id")
    if not asset_id:
        raise errors.AppError(
            "invalid_request",
            f"槽位 {slot['param']} 缺少 asset_id，无法确定参考图",
            422,
        )
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise errors.AppError(
            "reference_asset_missing",
            f"槽位 {slot['param']} 引用的资产 {asset_id} 不存在",
            409,
        )
    if not asset.file_path or not os.path.exists(asset.file_path):
        raise errors.AppError(
            "reference_file_missing",
            f"槽位 {slot['param']} 引用的资产文件不存在，可能已被移除",
            409,
        )
    expected = slot.get("role")
    if expected and not _type_matches(expected, asset.asset_type):
        raise errors.AppError(
            "ref_type_mismatch",
            f"槽位 {slot['param']} 需要 {expected} 用途资产，实际为 {asset.asset_type}",
            422,
        )
    return asset


def validate_refs(db: Session, slots: list[dict], refs: list[dict]) -> list[dict]:
    """按真实工作流槽位校验参考图集合，返回归一化后的启用参考图列表。

    校验项：槽位是否被工作流支持、必需槽位是否保留、资产与版本是否存在、
    文件是否可读、资产类型是否与槽位用途匹配。全部失败均返回明确错误码。
    """
    slot_index = {s["param"]: s for s in slots}
    by_slot: dict[str, dict] = {}
    for ref in refs:
        param = ref.get("slot_param")
        if not param:
            raise errors.AppError("invalid_request", "参考图缺少槽位参数 slot_param", 422)
        slot = slot_index.get(param)
        if slot is None:
            available = [s["param"] for s in slots] or ["无"]
            raise errors.AppError(
                "slot_not_supported",
                f"当前工作流不支持槽位 {param}，可用槽位：{available}",
                422,
            )
        by_slot[param] = {**ref, "slot": slot}

    for slot in slots:
        if not slot["required"]:
            continue
        ref = by_slot.get(slot["param"])
        if ref is None or not ref.get("enabled", True):
            raise errors.AppError(
                "required_slot_missing",
                f"槽位 {slot['param']} 为当前工作流必需输入，不可移除或禁用",
                422,
            )

    result: list[dict] = []
    seq = 0
    for ref in by_slot.values():
        if not ref.get("enabled", True):
            continue
        slot = ref["slot"]
        asset = _resolve_asset_ref(db, ref, slot)
        result.append({
            "slot_param": slot["param"],
            "input_key": slot["input_key"],
            "asset_id": asset.id,
            "asset_version": asset.version,
            "asset_type": asset.asset_type,
            "role": slot["role"],
            "required": slot["required"],
            "file_path": asset.file_path,
            "semantic": slot["semantic"],
            "enabled": True,
            "seq": seq,
        })
        seq += 1
    return result


def assemble_snapshot(
    db: Session,
    *,
    mode: str,
    base_config: dict,
    prompt: str,
    negative_prompt: str,
    refs: list[dict] | None = None,
    params: dict | None = None,
    workflow: Workflow | None = None,
) -> dict:
    """按模式合并继承项与用户改动，输出冻结快照。"""
    if mode not in VALID_MODES:
        raise errors.AppError("invalid_request", f"不支持的重新生成模式：{mode}", 400)

    complete = bool(base_config.get("snapshot_complete"))
    if not complete and mode == MODE_INHERIT_PROMPT_ONLY:
        raise errors.AppError(
            "snapshot_incomplete",
            "历史配置不完整，无法使用『沿用原配置，只修改提示词』模式，请改用完整重新配置",
            409,
            reasons=base_config.get("incomplete_reasons") or [],
        )

    base_refs = list(base_config.get("refs") or [])
    slots = list(base_config.get("available_slots") or [])
    base_params = dict(base_config.get("model_params") or {})
    base_workflow = dict(base_config.get("workflow") or {})

    if mode == MODE_INHERIT_PROMPT_ONLY:
        final_refs = base_refs
        final_params = base_params
        final_workflow = base_workflow
    elif mode == MODE_MODIFY_REFS:
        final_refs = base_refs if refs is None else refs
        final_params = base_params
        final_workflow = base_workflow
    else:
        final_refs = base_refs if refs is None else refs
        final_params = {**base_params, **(params or {})}
        if workflow is not None:
            slots = normalize_reference_slots(workflow.capabilities)
            final_workflow = {
                "id": workflow.id,
                "name": workflow.name,
                "type": workflow.workflow_type,
                "version": workflow.version,
            }
        else:
            final_workflow = base_workflow

    validated = validate_refs(db, slots, final_refs)

    snapshot = {
        "snapshot_version": 1,
        "mode": mode,
        "base": base_config.get("base"),
        "prompt": {"rendered": prompt},
        "negative_prompt": {"rendered": negative_prompt},
        "workflow": final_workflow,
        "model_params": final_params,
        "refs": validated,
        "inherited": {
            "refs_locked": mode == MODE_INHERIT_PROMPT_ONLY,
            "params_locked": mode != MODE_FULL_RECONFIGURE,
            "workflow_locked": mode != MODE_FULL_RECONFIGURE,
        },
        "created_at": _now().isoformat(),
    }
    # 哈希只反映输入内容，排除生成时间与哈希自身，保证相同输入得到相同哈希
    hashable = {
        k: v for k, v in snapshot.items()
        if k not in ("created_at", "snapshot_hash")
    }
    digest = hashlib.sha256(
        json.dumps(hashable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    snapshot["snapshot_hash"] = digest
    return snapshot


def write_input_refs(db: Session, job: Job, snapshot: dict) -> int:
    """把快照中的参考图结构化为 job_input_ref 记录，作为可查询的任务输入索引。"""
    refs = snapshot.get("refs") or []
    for seq, ref in enumerate(refs):
        db.add(JobInputRef(
            id=ids.job_input_ref_id(),
            job_id=job.id,
            slot_param=ref["slot_param"],
            input_key=ref.get("input_key"),
            asset_id=ref.get("asset_id"),
            asset_version=ref.get("asset_version"),
            asset_type=ref.get("asset_type"),
            role=ref.get("role"),
            required=bool(ref.get("required")),
            file_path=ref.get("file_path"),
            semantic=ref.get("semantic") or "reference",
            enabled=bool(ref.get("enabled", True)),
            seq=seq,
            created_at=_now(),
        ))
    return len(refs)


def snapshot_to_media(snapshot: dict) -> dict[str, str]:
    """把快照中启用的参考图转成工作流媒体参数 {槽位: 本地文件路径}。"""
    media: dict[str, str] = {}
    for ref in snapshot.get("refs") or []:
        if not ref.get("enabled", True):
            continue
        path = ref.get("file_path")
        if path:
            media[ref["slot_param"]] = path
    return media
