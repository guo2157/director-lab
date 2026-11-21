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

from app.models import Asset, Job, JobInputRef, Shot, Workflow, ids
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
    "ref_character_3": "character",
    "ref_character_4": "character",
    "ref_character_5": "character",
    "ref_character_6": "character",
    "ref_scene": "scene",
    "ref_prop": "prop",
    "ref_image": "keyframe",
    "ref_prev_frame": "prev_frame",
    "first_frame": "first_frame",
    "end_frame": "end_frame",
}

# 槽位展示顺序：与详情页参考图列表一致（主角色 → 次角色 → 场景 → 道具 → 帧约束）
_SLOT_ORDER = {
    "ref_identity": 0,
    "ref_character_2": 1,
    "ref_character_3": 2,
    "ref_character_4": 3,
    "ref_character_5": 4,
    "ref_character_6": 5,
    "ref_scene": 6,
    "ref_prop": 7,
    "ref_image": 8,
    "ref_prev_frame": 9,
    "first_frame": 10,
    "end_frame": 11,
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _slot_semantic(param: str) -> str:
    return "frame_constraint" if param in FRAME_CONSTRAINT_SLOTS else "reference"


def _slot_role(param: str) -> str | None:
    return ROLE_BY_SLOT.get(param)


def normalize_reference_slots(capabilities: dict | None) -> list[dict]:
    """归一化工作流 capabilities.reference_slots。

    兼容三种真实声明格式：
    - 字符串数组：["ref_identity", "ref_character_2"]
    - 对象数组（旧扁平结构）：[{"param": "ref_identity", "input_key": "...", "optional": true}]
    - 归一化后的嵌套结构：capabilities["reference"]["slots"]
      （`capabilities.normalize()` 的输出，`.meta.json` 移除后生产库存的就是它）
    仅以工作流真实声明为准，不补造槽位。
    """
    caps = capabilities or {}
    raw = caps.get("reference_slots")
    if raw is None:
        # 新结构：reference.slots，与 reference_slots 语义相同但字段更全
        raw = (caps.get("reference") or {}).get("slots") or []
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


def tier_generation_params(wf: Workflow | None) -> dict:
    """取档位工作流自带的画幅与帧数（极速/标准 5s、质量 10s）。

    换档重新生成时必须用新档位的参数，不能沿用上一版任务快照里的
    width/height/length，否则选了 10s 档位仍按 5s 出片。
    """
    if wf is None or not (wf.capabilities or {}).get("tier"):
        return {}
    for node in (wf.definition or {}).values():
        if not isinstance(node, dict) or node.get("class_type") != "MiniMaxH3ReferenceToVideo":
            continue
        inputs = node.get("inputs") or {}
        return {
            key: inputs[key]
            for key in ("width", "height", "length")
            if isinstance(inputs.get(key), int)
        }
    return {}


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


def _refs_from_current_plan(db: Session, shot_id: str, plan_type: str) -> list[dict]:
    """从**当前**资产定义（AssetPlanDependency）取参考图槽位。

    为什么需要这个（2026-11-21 修复）：

    详情页的参考图列表原本只从「原始生成任务快照」反查（`_refs_from_records` /
    `_refs_from_snapshot`）。但快照是**历史快照**——图片是用旧代码生成的时候，
    快照里只有当时注入的槽位。实测事故：

        sht_d766f85afbc6 的分镜图由 job_0a97b6dc1dae 生成
        该任务快照 __media__ = {ref_identity: 江屿, ref_scene: 海岸沙滩·夕阳}
        → 详情页只显示 2 个槽位，ref_character_2 显示「未绑定」

    而此时资产定义（重建后）已经正确算出 3 个槽位（含 ref_character_2=林晚）。
    用户看到「提示词里两个角色、参考图只有一个槽位」，指的就是这个现象：
    **不是槽位算错了，是详情页读的是过期快照。**

    这里把「当前定义」作为权威来源，快照仅作为兜底（定义缺失的历史数据）。
    """
    if not shot_id:
        return []
    from app.models import AssetPlan, AssetPlanDependency

    plan = (
        db.query(AssetPlan)
        .filter(
            AssetPlan.owner_id == shot_id,
            AssetPlan.plan_type == plan_type,
        )
        .first()
    )
    if plan is None:
        return []
    deps = (
        db.query(AssetPlanDependency)
        .filter(AssetPlanDependency.plan_id == plan.id)
        .all()
    )
    if not deps:
        return []
    refs: list[dict] = []
    for dep in deps:
        # 只取参考图类槽位，跳过 ref_prop 等非角色/场景槽位由调用方按需保留
        if not dep.slot or not dep.slot.startswith("ref_"):
            continue
        path, asset_id, version = None, None, None
        try:
            from app.api.v1.asset_plans import _resolve_dep_path
            path, asset_id, version = _resolve_dep_path(db, dep)
        except Exception:  # 解析失败时退回定义里的占位路径
            path = dep.ref_path
        asset = db.get(Asset, asset_id) if asset_id else None
        refs.append({
            "slot_param": dep.slot,
            "input_key": None,
            "asset_id": asset_id,
            "asset_version": version,
            "asset_type": asset.asset_type if asset is not None else None,
            "role": _slot_role(dep.slot),
            "required": dep.slot in ("ref_identity", "ref_scene"),
            "file_path": path,
            "semantic": _slot_semantic(dep.slot),
            "enabled": bool(path),
            "source": "current_plan",
        })
    refs.sort(key=lambda r: _SLOT_ORDER.get(r["slot_param"], 99))
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

    # 当前定义优先（2026-11-21）：历史任务快照只反映「当时」注入了什么槽位，
    # 代码升级后主体角色算法变了，快照就会与当前定义不一致——详情页因此显示
    # 「提示词两个角色、参考图只有一个槽位」。以定义为准合并：定义里有的槽位
    # 覆盖快照，定义里没有的槽位（历史遗留）保留，避免丢失旧数据。
    _plan_type = None
    _shot_id = job.shot_id if job is not None else None
    _job_type = job.job_type if job is not None else None
    if _shot_id:
        if _job_type == "generate_keyframe":
            _plan_type = "keyframe"
        elif _job_type == "generate_clip":
            _plan_type = "clip"
    if _plan_type and _shot_id:
        _current = _refs_from_current_plan(db, _shot_id, _plan_type)
        if _current:
            _cur_slots = {r["slot_param"] for r in _current}
            refs = _current + [r for r in refs if r["slot_param"] not in _cur_slots]
            refs.sort(key=lambda r: _SLOT_ORDER.get(r["slot_param"], 99))

    slot_index = {s["param"]: s for s in slots}
    for ref in refs:
        slot = slot_index.get(ref["slot_param"])
        if slot is not None:
            if not ref.get("input_key"):
                ref["input_key"] = slot["input_key"]
            ref["required"] = bool(ref.get("required")) or slot["required"]
            ref["semantic"] = slot["semantic"]
            ref["role"] = ref.get("role") or slot["role"]

    # 补充资产名称：供前端展示参考图与资产的绑定关系
    _name_ids = {r.get("asset_id") for r in refs if r.get("asset_id")}
    if _name_ids:
        _name_map = {a.id: a.name for a in db.query(Asset).filter(Asset.id.in_(_name_ids)).all()}
        for r in refs:
            aid = r.get("asset_id")
            if aid and aid in _name_map:
                r["asset_name"] = _name_map[aid]
    prompt = params.get("prompt") or ""
    negative = params.get("negative") or ""

    # 提示词头部的「参考资产」按当前口径重算（2026-11-21 修复）。
    #
    # 快照里的 prompt 是**生成时**的文本。当时的 collect_shot_characters 只认
    # character_id + 对白说话人，双人镜里第二个人物（只出现在 beats 动作描述里）
    # 根本没被算出来，于是快照 prompt 头部写的是：
    #     参考资产：@江屿 @海岸沙滩·夕阳      ← 缺林晚
    # 而槽位绑定用的是修复后的 shot_subject_characters，能算出两个角色。
    # 两者不一致就出现用户看到的「提示词里两个角色、参考图只有一个槽位」：
    # 模型只拿到一张定妆图去画两个人，画面里于是出现两个江屿。
    #
    # 这里用当前 inject_asset_refs 重算头部（幂等：已有 @ 引用不重复注入），
    # 让详情页展示的提示词与真正提交生成时用的文本保持同一口径。
    if prompt and _shot_id and _plan_type == "keyframe":
        try:
            from app.services.shot_refs import inject_asset_refs
            _shot_obj = db.get(Shot, _shot_id)
            if _shot_obj is not None:
                # 快照 prompt 可能已带旧的「参考资产」行，先剥离再按当前口径重建，
                # 否则旧行残留会导致新角色加不进去。
                _body = prompt
                _head, _sep, _rest = _body.partition("\n")
                if _head.startswith("参考资产："):
                    _body = _rest
                prompt = inject_asset_refs(db, _shot_obj, _body)
        except Exception:  # 注入失败不阻断配置读取，保留原快照文本
            pass

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


def build_clip_refs_from_keyframe(db: Session, shot, keyframe: Asset) -> list[dict]:
    """尚未生成分镜视频时，按视频生成的真实槽位口径构造参考图绑定。

    视频任务实际注入的是：上一镜尾帧（ref_prev_frame）+ 分镜图（ref_image）
    + 角色定妆图（ref_identity/ref_character_N）+ 道具锚定图（ref_prop），
    不包含场景基准图。因此这里不能直接复用分镜图任务的参考清单
    （那是 image_1..7 口径，含场景且没有分镜图本身）。
    """
    from app.models import Character, Prop, Scene
    from app.services.shot_chain import resolve_prev_frame
    from app.services.shot_refs import collect_shot_characters

    scene = db.get(Scene, shot.scene_id)
    refs: list[dict] = []
    # 串联生成时本镜首帧取自上一镜实际尾帧（ref_images.ref_image_0），需在参考图中可见可改
    frame = resolve_prev_frame(db, shot)
    if frame is not None:
        refs.append({
            "slot_param": "ref_prev_frame",
            "asset_id": frame["asset_id"],
            "asset_version": frame["version"],
            "asset_type": "last_frame",
            "role": "prev_frame",
            "required": bool(shot.requires_prev_frame),
            "file_path": frame["file_path"],
            "semantic": "frame_constraint",
            "enabled": True,
            "asset_name": frame.get("asset_name") or "上一镜尾帧（串联首帧）",
            "source": "keyframe_fallback",
        })
    refs.append({
        "slot_param": "ref_image",
        "asset_id": keyframe.id,
        "asset_version": keyframe.version,
        "asset_type": keyframe.asset_type,
        "role": "keyframe",
        "required": True,
        "file_path": keyframe.file_path,
        "semantic": "reference",
        "enabled": True,
        "asset_name": keyframe.name or "本镜分镜图",
        "source": "keyframe_fallback",
    })
    if scene is None:
        return refs
    char_slots = ["ref_identity"] + [f"ref_character_{i}" for i in range(2, 7)]
    for index, name in enumerate(collect_shot_characters(db, shot)):
        if index >= len(char_slots):
            break
        char = (
            db.query(Character)
            .filter(Character.project_id == scene.project_id, Character.name == name)
            .first()
        )
        if char is None or not char.portrait_asset_id:
            continue
        asset = db.get(Asset, char.portrait_asset_id)
        if asset is None:
            continue
        refs.append({
            "slot_param": char_slots[index],
            "asset_id": asset.id,
            "asset_version": asset.version,
            "asset_type": asset.asset_type,
            "role": "character",
            "required": False,
            "file_path": asset.file_path,
            "semantic": "reference",
            "enabled": True,
            "asset_name": char.name,
            "source": "keyframe_fallback",
        })
    prop_ids = [pid for pid in (shot.props or []) if isinstance(pid, str)]
    prop_query = db.query(Prop).filter(Prop.project_id == scene.project_id,
                                       Prop.anchor_asset_id.isnot(None))
    prop = prop_query.filter(Prop.id.in_(prop_ids)).first() if prop_ids else None
    if prop is None:
        # shot.props 可能为空（旧数据未持久化），按正文提及回退，避免道具参考图整体缺失
        text = f"{shot.video_prompt or ''}\n{shot.image_prompt or ''}"
        for candidate in prop_query.all():
            if candidate.name and candidate.name in text:
                prop = candidate
                break
    if prop is not None:
        asset = db.get(Asset, prop.anchor_asset_id)
        if asset is not None:
            refs.append({
                "slot_param": "ref_prop",
                "asset_id": asset.id,
                "asset_version": asset.version,
                "asset_type": asset.asset_type,
                "role": "prop",
                "required": False,
                "file_path": asset.file_path,
                "semantic": "reference",
                "enabled": True,
                "asset_name": prop.name,
                "source": "keyframe_fallback",
            })
    return refs


def _type_matches(role: str, asset_type: str | None) -> bool:
    """参考图资产类型与槽位用途的匹配判定，避免类型错配静默通过。"""
    if not asset_type:
        return True
    if role in ("character", "scene", "prop"):
        # reference：本地上传的参考图（无实体归属）也允许绑定参考槽位
        return asset_type in (role, "keyframe", "asset", "reference")
    if role == "keyframe":
        return asset_type in ("keyframe", "asset")
    # 尾帧/首帧资产由 shot_chain 从视频抽取并落为 asset_type=last_frame，
    # 绑定到 ref_prev_frame 时不能按图片类型误判为错配
    if role == "prev_frame":
        return asset_type in ("clip", "keyframe", "frame", "last_frame", "asset")
    if role in ("first_frame", "end_frame"):
        return asset_type in ("clip", "keyframe", "frame", "last_frame", "asset")
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
            "asset_name": asset.name,
            "role": slot["role"],
            "required": slot["required"],
            "file_path": asset.file_path,
            "semantic": slot["semantic"],
            "enabled": True,
            "seq": seq,
        })
        seq += 1
    return result


def validate_prompt_consistency(prompt: str, refs: list[dict], slots: list[dict]) -> dict:
    """校验提示词图片引用与参考图槽位的一致性（确定性代码校验，不依赖 LLM）。

    校验等级：
    - 错误（阻断生成）：提示词引用的图片编号没有对应启用的参考图
    - 警告（允许确认后生成）：已注入的参考图未被提示词引用
    图片编号严格按工作流 input_key 换算（images.image_N → N），
    与 worker 实际注入 ComfyUI 的槽位一一对应，不按数组下标猜测。
    """
    import re

    from app.services.ref_inject import slot_image_index, workflow_ref_syntax

    errors: list[dict] = []
    warnings: list[dict] = []
    checks: list[dict] = []
    # 归一化槽位转回 ref_inject 兼容格式（param + input_key），用于图序换算与语法判定
    raw_slots = [
        {"param": s["param"], "input_key": s.get("input_key")}
        for s in slots
        if s.get("input_key")
    ]
    syntax = workflow_ref_syntax(raw_slots)
    if syntax is None:
        # 工作流未声明图片引用语法（纯文生图），无编号可校验
        return {"valid": True, "errors": errors, "warnings": warnings, "checks": checks}
    # 启用参考图：槽位 → 图片编号（1-based，与工作流 input_key 严格对应）
    enabled_refs = [r for r in refs if r.get("enabled", True)]
    slot_to_index: dict[str, int] = {}
    for ref in enabled_refs:
        idx = slot_image_index(ref.get("slot_param") or "", raw_slots)
        if idx is not None:
            slot_to_index[ref["slot_param"]] = idx
    # 提取提示词中的图片引用编号（按工作流语法）
    if syntax == "image":
        pattern = re.compile(r"<image(\d+)>")
    else:
        pattern = re.compile(r"<Picture\s+(\d+)>")
    referenced = {int(m.group(1)) for m in pattern.finditer(prompt or "")}
    # 错误：提示词引用的编号没有对应启用的参考图
    for num in sorted(referenced):
        matched = [s for s, i in slot_to_index.items() if i == num]
        if not matched:
            errors.append({
                "code": "PROMPT_REF_MISSING",
                "image_no": num,
                "message": f"提示词引用了 Image {num}，但没有对应的启用参考图，请补充该槽位图片或修改提示词",
            })
        else:
            checks.append({
                "image_no": num,
                "slot": matched[0],
                "status": "ok",
                "message": f"Image {num} 对应槽位 {matched[0]}，绑定正确",
            })
    # 警告：已注入但未被提示词引用的参考图
    for ref in enabled_refs:
        idx = slot_to_index.get(ref["slot_param"])
        if idx is not None and idx not in referenced:
            warnings.append({
                "code": "REF_NOT_REFERENCED",
                "slot": ref["slot_param"],
                "image_no": idx,
                "message": f"参考图 {ref['slot_param']}（Image {idx}）已注入但未被提示词引用，生成时该图仍会传入模型",
            })
    return {"valid": not errors, "errors": errors, "warnings": warnings, "checks": checks}


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
        # 沿用原配置时参考图仍需以调用方传入的实时系统槽位为准：
        # 历史快照里的文件路径可能已随尾帧/分镜图改名失效，反查资产为空，
        # 直接用 base_refs 会报『槽位缺少 asset_id』
        final_refs = base_refs if refs is None else refs
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
    # 提示词与参考图一致性校验（确定性代码校验）：结果随快照冻结，供前端展示与提交门禁复用
    consistency = validate_prompt_consistency(prompt, validated, slots)

    snapshot = {
        "snapshot_version": 1,
        "mode": mode,
        "base": base_config.get("base"),
        "prompt": {"rendered": prompt},
        "negative_prompt": {"rendered": negative_prompt},
        "workflow": final_workflow,
        "model_params": final_params,
        "refs": validated,
        "consistency": consistency,
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
