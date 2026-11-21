"""分镜视频重新生成工作台：结构化绑定、分层提示词与一致性校验。

核心原则（对应整改方案第二十四节）：
- 结构化参考图绑定是唯一事实来源，提示词只是绑定关系的自然语言表达
- 槽位 → node_id 映射一律由工作流 definition 中的标记解析得到，禁止数组下标猜测
- 身份绑定解决『谁是谁』，空间布局解决『谁在哪里』，动作绑定解决『谁做什么』，
  三者独立存储，不互相推导
- 工作流不支持精确坐标/时间控制时，结构化数据只转成自然语言约束，
  绝不伪造 ComfyUI 输入参数
- 历史数据缺失一律标记 pending_confirmation，不静默补默认值
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Asset, Character, Job, Prop, Shot, Workflow
from app.services import errors
from app.services.workflow_inject import parse_workflow

# 参考图业务语义分类（不等于独立 ComfyUI 节点，仅用于界面分组与提示词措辞）
REFERENCE_TYPES = (
    "first_frame",
    "last_frame",
    "character",
    "character_with_prop",
    "scene",
    "prop",
    "composition",
    "other",
)

HORIZONTAL_OPTIONS = ("left", "center", "right")
VERTICAL_OPTIONS = ("top", "center", "bottom")
DEPTH_OPTIONS = ("foreground", "middle_ground", "background")
FACING_OPTIONS = ("left", "right", "camera", "away", "custom")

REF_TYPE_LABEL = {
    "first_frame": "视频首帧",
    "last_frame": "视频尾帧",
    "character": "角色身份参考",
    "character_with_prop": "角色持有道具组合参考",
    "scene": "场景参考",
    "prop": "道具参考",
    "composition": "构图参考",
    "other": "其他参考",
}

HORIZONTAL_LABEL = {"left": "画面左侧", "center": "画面中央", "right": "画面右侧"}
VERTICAL_LABEL = {"top": "画面上部", "center": "画面中部", "bottom": "画面下部"}
DEPTH_LABEL = {"foreground": "前景", "middle_ground": "中景", "background": "背景"}
FACING_LABEL = {
    "left": "面向画面左侧",
    "right": "面向画面右侧",
    "camera": "面向镜头",
    "away": "背对镜头",
    "custom": "自定义朝向",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


AUDIO_INPUT_PREFIXES = ("ref_audios.", "ref_audio_", "audio")


def _image_index_of(input_key: str | None) -> int | None:
    """按工作流声明的 input_key 换算图片序号（1-based）。

    - ref_images.ref_image_N（H3 ref2va，0-based）→ N+1
    - images.image_N（Qwen-Image-2.1）→ N
    其他形态（first_frame / last_frame 等具名槽）不参与图片编号，返回 None。
    """
    key = str(input_key or "")
    m = re.match(r"^ref_images\.ref_image_(\d+)$", key)
    if m:
        return int(m.group(1)) + 1
    m = re.match(r"^images\.image_(\d+)$", key)
    if m:
        return int(m.group(1))
    return None


def _is_audio_key(input_key: str | None) -> bool:
    key = str(input_key or "")
    return any(key.startswith(p) for p in AUDIO_INPUT_PREFIXES)


def slot_node_map(wf: Workflow | None) -> dict[str, dict]:
    """解析槽位 → {node_id, input_key, image_index, required}。

    两处权威来源各取其一，都不猜：
    - node_id 来自工作流 definition 中节点 _meta.title 的真实注入标记（$~slot.field）
    - input_key 与图片序号来自 capabilities.reference_slots 的显式声明
    音频槽位（ref_audios.*）不进入参考图映射：H3 的 TTS 音频不得占用图片槽位。
    """
    if wf is None or not isinstance(wf.definition, dict):
        return {}
    parsed = parse_workflow(wf.definition)
    declared: dict[str, dict] = {}
    caps = wf.capabilities if isinstance(wf.capabilities, dict) else {}
    # 统一入口：数据库保存的是 reference.slots，仅读顶层 reference_slots 会得到空声明，
    # 导致图片序号与 input_key 全部回退到节点标记，H3 槽位序号错位
    from app.services.workflow.capabilities import declared_reference_slots
    for item in declared_reference_slots(caps):
        if isinstance(item, dict) and item.get("param"):
            declared[item["param"]] = item
    mapping: dict[str, dict] = {}
    for name, binding in (parsed.params or {}).items():
        if not binding.media:
            continue
        decl = declared.get(name) or {}
        input_key = decl.get("input_key") or binding.field
        if _is_audio_key(input_key):
            continue
        mapping[name] = {
            "node_id": binding.node_id,
            "input_key": input_key,
            "image_index": _image_index_of(input_key),
            "required": bool(binding.required) or bool(decl.get("required")),
            "optional_declared": bool(decl.get("optional", True)),
        }
    return mapping


def _image_no_label(image_index: int | None, slot: str, syntax: str | None = None) -> str:
    """生成提示词中的图片引用标记。

    必须与工作流真实语法一致：ref_images.* → <Picture N>，images.image_N → <imageN>。
    用自然语言『Image 2』会让模型对不上槽位，也会让确定性校验失效。
    """
    if image_index is None:
        return slot
    if syntax == "picture":
        return f"<Picture {image_index}>"
    if syntax == "image":
        return f"<image{image_index}>"
    return f"Image {image_index}"


def _reference_syntax(nodes: dict[str, dict]) -> str | None:
    """判定工作流图片引用语法：ref_images.* → picture，images.image_N → image。

    复用 ref_inject 的判定口径，确保提示词标记与 worker 实际注入一致。
    """
    from app.services.ref_inject import workflow_ref_syntax

    raw = [
        {"param": name, "input_key": node.get("input_key")}
        for name, node in nodes.items()
    ]
    return workflow_ref_syntax(raw)


def _ref_type_for_slot(slot: str, role: str | None) -> str:
    if slot in ("first_frame",):
        return "first_frame"
    if slot in ("last_frame", "ref_prev_frame"):
        return "last_frame"
    if role == "character":
        return "character"
    if role == "scene":
        return "scene"
    if role == "prop":
        return "prop"
    if role == "keyframe":
        return "composition"
    return "other"


def _asset_kind(asset_type: str | None) -> str:
    return asset_type or "reference"


def build_references(
    db: Session,
    base_config: dict,
    nodes: dict[str, dict],
    shot: Shot,
) -> list[dict]:
    """把基础配置中的参考图转成结构化绑定，槽位缺失即标为待确认。"""
    refs: list[dict] = []
    for seq, ref in enumerate(base_config.get("refs") or []):
        slot = ref.get("slot_param") or ""
        node = nodes.get(slot) or {}
        role = ref.get("role")
        char_id = None
        if role == "character" and ref.get("asset_id"):
            asset = db.get(Asset, ref["asset_id"])
            if asset is not None:
                char = (
                    db.query(Character)
                    .filter(Character.project_id == asset.project_id)
                    .filter(Character.portrait_asset_id == asset.id)
                    .first()
                )
                char_id = char.id if char is not None else None
        image_index = node.get("image_index")
        # node 映射缺失时（当前定义提供的槽位尚未与工作流节点对齐）回退到
        # ref 自带的 input_key，否则详情页的 image_url/input_key 会全空。
        input_key = node.get("input_key") or ref.get("input_key")
        refs.append({
            "reference_id": f"ref_{slot or seq}",
            "slot_id": slot,
            "node_id": node.get("node_id"),
            "input_key": input_key,
            "image_no": image_index,
            "reference_type": _ref_type_for_slot(slot, role),
            "asset_type": _asset_kind(ref.get("asset_type")),
            "asset_id": ref.get("asset_id"),
            "asset_name": ref.get("asset_name"),
            "character_id": char_id,
            "image_url": ref.get("file_path"),
            "binding_description": (
                f"{ref.get('asset_name') or slot} 的{REF_TYPE_LABEL.get(_ref_type_for_slot(slot, role), '参考')}"
            ),
            "identity_constraints": (
                ["保持角色原有外观", "保持体型与颜色", "保持五官特征", "不能与其他角色混淆"]
                if role == "character"
                else []
            ),
            "required": bool(node.get("required") or ref.get("required")),
            "enabled": bool(ref.get("enabled", True)),
            "sort_order": seq,
            "semantic": ref.get("semantic") or "reference",
            "status": "confirmed" if node.get("node_id") else "slot_unmapped",
        })
    return refs


def system_clip_refs(db: Session, shot: Shot) -> list[dict]:
    """clip 目标的系统参考（快照口径）：本镜分镜图与上一镜实际尾帧。

    两类语义必须区分：ref_image 是本镜构图参考，ref_prev_frame 是上一镜尾帧约束。
    """
    from app.services.shot_chain import resolve_prev_frame

    def _current(asset_type: str) -> Asset | None:
        return (
            db.query(Asset)
            .join(Job, Job.id == Asset.source_job_id)
            .filter(
                Job.shot_id == shot.id,
                Job.status == "succeeded",
                Asset.asset_type == asset_type,
                Asset.validity == "current",
            )
            .order_by(Asset.version.desc())
            .first()
        )

    refs: list[dict] = []
    keyframe = _current("keyframe")
    if keyframe is not None and keyframe.file_path:
        refs.append({
            "slot_param": "ref_image",
            "input_key": "ref_images.ref_image_1",
            "asset_id": keyframe.id,
            "asset_version": keyframe.version,
            "asset_type": keyframe.asset_type,
            "role": "keyframe",
            "required": False,
            "file_path": keyframe.file_path,
            "semantic": "reference",
            "enabled": True,
            "asset_name": keyframe.name or "本镜分镜图",
        })
    frame = resolve_prev_frame(db, shot)
    if frame is not None:
        refs.append({
            "slot_param": "ref_prev_frame",
            "input_key": "ref_images.ref_image_0",
            "asset_id": frame["asset_id"],
            "asset_version": frame["version"],
            "asset_type": "last_frame",
            "role": "prev_frame",
            "required": bool(shot.requires_prev_frame),
            "file_path": frame["file_path"],
            "semantic": "frame_constraint",
            "enabled": True,
            "asset_name": frame.get("asset_name") or "上一镜尾帧（串联首帧）",
        })
    return refs


# 系统槽位：必须实时解析当前生效产物，不能沿用基础版本任务快照里的旧绑定
SYSTEM_SLOT_PARAMS = ("ref_prev_frame", "ref_image")


def merge_system_refs(existing: list[dict], system_refs: list[dict]) -> list[dict]:
    """用实时系统参考覆盖同槽位快照绑定，其余槽位保持原顺序。

    ref_prev_frame / ref_image 若实时解析不到（上一镜无尾帧、本镜无分镜图），
    必须移除旧快照绑定，不能用过期资产顶替。
    """
    by_slot = {ref["slot_param"]: ref for ref in system_refs}
    merged: list[dict] = []
    for ref in existing:
        slot = ref.get("slot_param")
        if slot in by_slot:
            merged.append(by_slot[slot])
        elif slot in SYSTEM_SLOT_PARAMS:
            continue
        else:
            merged.append(ref)
    present = {ref.get("slot_param") for ref in merged}
    merged.extend(ref for ref in system_refs if ref["slot_param"] not in present)
    return merged


def refresh_system_references(
    db: Session, shot: Shot, references: list[dict], nodes: dict[str, dict]
) -> list[dict]:
    """把工作区参考里的系统槽位刷新为当前生效资产。

    已保存配置（saved）和任务快照都可能指向旧尾帧/旧分镜图，
    刷新后工作台与镜头、视频详情显示的是同一张图。
    """
    by_slot = {ref["slot_param"]: ref for ref in system_clip_refs(db, shot)}
    refreshed: list[dict] = []
    for ref in references:
        slot = ref.get("slot_id")
        if slot in by_slot:
            live = by_slot.pop(slot)
            node = nodes.get(slot) or {}
            item = dict(ref)
            item.update({
                "asset_id": live["asset_id"],
                "asset_name": live["asset_name"],
                "asset_type": _asset_kind(live["asset_type"]),
                "image_url": live["file_path"],
                "semantic": live["semantic"],
                "required": live["required"],
                "enabled": True,
                "status": "confirmed" if node.get("node_id") else item.get("status", "slot_unmapped"),
            })
            refreshed.append(item)
        elif slot in SYSTEM_SLOT_PARAMS:
            continue
        else:
            refreshed.append(ref)
    for slot, live in by_slot.items():
        node = nodes.get(slot) or {}
        ref_type = _ref_type_for_slot(slot, live.get("role"))
        refreshed.append({
            "reference_id": f"ref_{slot}",
            "slot_id": slot,
            "node_id": node.get("node_id"),
            "input_key": node.get("input_key"),
            "image_no": node.get("image_index"),
            "reference_type": ref_type,
            "asset_type": _asset_kind(live["asset_type"]),
            "asset_id": live["asset_id"],
            "asset_name": live["asset_name"],
            "character_id": None,
            "image_url": live["file_path"],
            "binding_description": f"{live['asset_name']} 的{REF_TYPE_LABEL.get(ref_type, '参考')}",
            "identity_constraints": [],
            "required": live["required"],
            "enabled": True,
            "sort_order": -1 if slot == "ref_prev_frame" else 0,
            "semantic": live["semantic"],
            "status": "confirmed" if node.get("node_id") else "slot_unmapped",
        })
    refreshed.sort(key=lambda r: 0 if r.get("slot_id") == "ref_prev_frame" else 1)
    return refreshed


def build_character_bindings(
    db: Session,
    references: list[dict],
    shot: Shot,
) -> list[dict]:
    """按结构化参考图生成角色身份绑定，缺图角色标为待补充。"""
    from app.services.shot_chain import shot_project_id
    from app.services.shot_refs import collect_shot_characters

    project_id = shot_project_id(db, shot)
    bindings: list[dict] = []
    by_char = {r["character_id"]: r for r in references if r.get("character_id")}
    for name in collect_shot_characters(db, shot):
        query = db.query(Character).filter(Character.name == name)
        if project_id:
            query = query.filter(Character.project_id == project_id)
        char = query.first()
        ref = by_char.get(char.id) if char is not None else None
        bindings.append({
            "character_id": char.id if char is not None else None,
            "character_name": name,
            "identity_reference_id": ref.get("reference_id") if ref else None,
            "identity_slot_id": ref.get("slot_id") if ref else None,
            "identity_image_no": ref.get("image_no") if ref else None,
            "identity_constraints": (
                ref.get("identity_constraints")
                if ref and ref.get("identity_constraints")
                else ["保持角色外观一致", "保持原有体型", "保持五官特征"]
            ),
            "status": "bound" if ref else "identity_reference_missing",
        })
    return bindings


def _position_of(state: dict | None) -> dict:
    """从镜头起止状态中读取空间位置；读不到就是待确认，不做推断。"""
    data = state or {}
    pos = data.get("position") if isinstance(data.get("position"), dict) else {}
    horizontal = pos.get("horizontal") if pos.get("horizontal") in HORIZONTAL_OPTIONS else None
    vertical = pos.get("vertical") if pos.get("vertical") in VERTICAL_OPTIONS else None
    depth = pos.get("depth") if pos.get("depth") in DEPTH_OPTIONS else None
    facing = data.get("facing") if data.get("facing") in FACING_OPTIONS else None
    x = pos.get("x") if isinstance(pos.get("x"), (int, float)) else None
    y = pos.get("y") if isinstance(pos.get("y"), (int, float)) else None
    known = any(v is not None for v in (horizontal, vertical, depth, x, y))
    return {
        "horizontal": horizontal,
        "vertical": vertical,
        "depth": depth,
        "x": x,
        "y": y,
        "facing_direction": facing,
        "status": "confirmed" if known else "pending_confirmation",
    }


def build_spatial_layout(db: Session, shot: Shot, bindings: list[dict]) -> dict:
    """构建空间布局：初始位置来自 start_state，最终位置来自 end_state。

    两者都由分镜结构化字段提供；字段缺失就标 pending_confirmation，
    由用户在画布确认，禁止根据首帧自动猜测。
    """
    characters: list[dict] = []
    for binding in bindings:
        if not binding.get("character_id"):
            continue
        chars = shot.dialogue_segments or []
        speaker_state = None
        for seg in chars if isinstance(chars, list) else []:
            if isinstance(seg, dict) and seg.get("speaker") == binding["character_name"]:
                speaker_state = seg.get("state") if isinstance(seg.get("state"), dict) else None
                break
        initial = _position_of(shot.start_state if isinstance(shot.start_state, dict) else None)
        final = _position_of(shot.end_state if isinstance(shot.end_state, dict) else None)
        if speaker_state:
            initial = _position_of(speaker_state) if initial["status"] != "confirmed" else initial
        characters.append({
            "character_id": binding["character_id"],
            "character_name": binding["character_name"],
            "initial_position": initial,
            "final_position": final,
            "facing_direction": initial.get("facing_direction"),
            "movement_enabled": bool(
                initial.get("status") == "confirmed"
                and final.get("status") == "confirmed"
                and (initial.get("horizontal") != final.get("horizontal") or initial.get("x") != final.get("x"))
            ),
            "movement_direction": None,
            "movement_path": [],
            "relative_relations": [],
        })
    first = db.get(Asset, shot.last_frame_asset_id) if shot.last_frame_asset_id else None
    return {
        "coordinate_system": "screen_normalized",
        "aspect_ratio": _aspect_ratio(db, shot),
        "camera_view": "single_camera",
        "characters": characters,
        "prop_positions": [],
        "status": (
            "confirmed"
            if characters and all(c["initial_position"]["status"] == "confirmed" for c in characters)
            else "pending_confirmation"
        ),
        "source": "shot.start_state/end_state",
        "has_last_frame": first is not None,
    }


def _aspect_ratio(db: Session, shot: Shot) -> str:
    """按本镜最近一次视频任务的宽高推断画面比例，读不到时按 16:9。"""
    params = {}
    asset = (
        db.query(Asset)
        .join(Job, Asset.source_job_id == Job.id)
        .filter(Job.shot_id == shot.id, Asset.asset_type == "clip")
        .order_by(Asset.version.desc())
        .first()
    )
    if asset is not None and asset.source_job_id:
        job = db.get(Job, asset.source_job_id)
        if job is not None and isinstance(job.params, dict):
            params = job.params
    width = params.get("width")
    height = params.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        if height > width:
            return "9:16"
        if abs(width - height) < max(1, int(width * 0.02)):
            return "1:1"
    return "16:9"


def build_action_timeline(db: Session, shot: Shot, bindings: list[dict], spatial: dict) -> dict:
    """把分镜 beats 转成动作时间轴，动作主体缺失标 pending_confirmation。"""
    name_to_id = {b["character_name"]: b.get("character_id") for b in bindings}
    duration = float(shot.duration or 0) or None
    beats = shot.beats if isinstance(shot.beats, list) else []
    actions: list[dict] = []
    for index, beat in enumerate(beats):
        if isinstance(beat, str):
            text = beat
            subject = None
        elif isinstance(beat, dict):
            text = beat.get("action") or beat.get("description") or ""
            subject = beat.get("character") or beat.get("subject")
        else:
            continue
        matched = None
        for name, cid in name_to_id.items():
            if name and name in text:
                matched = name
                break
        actions.append({
            "action_id": f"action_{index + 1:03d}",
            "character_id": name_to_id.get(subject) if subject else name_to_id.get(matched) if matched else None,
            "character_name": subject or matched,
            "start_progress": round(index / max(1, len(beats)), 3),
            "end_progress": round((index + 1) / max(1, len(beats)), 3),
            "action_description": text,
            "status": "confirmed" if (subject or matched) else "subject_pending",
        })
    return {
        "duration_seconds": duration,
        "actions": actions,
        "status": (
            "confirmed"
            if actions and all(a["status"] == "confirmed" for a in actions)
            else ("pending_confirmation" if actions else "empty")
        ),
        "source": "shot.beats",
    }


def build_prop_bindings(db: Session, shot: Shot, references: list[dict]) -> list[dict]:
    """道具绑定：道具资产身份 + 持有者，持有者必须有剧情依据。"""
    from app.models import CharacterPropBinding

    from app.services.shot_chain import shot_project_id

    project_id = shot_project_id(db, shot)
    result: list[dict] = []
    names = [n for n in (shot.props or []) if isinstance(n, str)] if isinstance(shot.props, list) else []
    ref_by_prop = {
        r.get("asset_name"): r for r in references if r.get("reference_type") == "prop"
    }
    for name in names:
        query = db.query(Prop).filter(Prop.name == name)
        if project_id:
            query = query.filter(Prop.project_id == project_id)
        prop = query.first()
        holder = None
        holder_name = None
        if prop is not None:
            binding = (
                db.query(CharacterPropBinding)
                .filter(CharacterPropBinding.prop_id == prop.id)
                .filter(CharacterPropBinding.valid_until_event.is_(None))
                .filter(CharacterPropBinding.review_status != "rejected")
                .first()
            )
            if binding is not None:
                holder = binding.character_id
                char = db.get(Character, binding.character_id)
                holder_name = char.name if char is not None else None
        ref = ref_by_prop.get(name)
        result.append({
            "prop_id": prop.id if prop is not None else None,
            "prop_name": name,
            "reference_id": ref.get("reference_id") if ref else None,
            "reference_slot_id": ref.get("slot_id") if ref else None,
            "holder_character_id": holder,
            "holder_character_name": holder_name,
            "visibility": "visible",
            "attachment_position": None,
            "follows_character": True,
            "handover": None,
            "status": "confirmed" if holder or ref else "holder_pending",
        })
    return result


def render_prompt_sections(
    references: list[dict],
    bindings: list[dict],
    spatial: dict,
    timeline: dict,
    props: list[dict],
    *,
    scene_title: str | None = None,
    camera_movement: str | None = None,
    user_text: str = "",
    syntax: str | None = None,
) -> dict:
    """按结构化数据分层渲染提示词，每段独立可重生成。"""
    ref_lines: list[str] = ["【参考图绑定】"]
    for ref in references:
        if not ref.get("enabled", True):
            continue
        label = _image_no_label(ref.get("image_no"), ref.get("slot_id") or "", syntax)
        desc = ref.get("binding_description") or REF_TYPE_LABEL.get(ref.get("reference_type"), "参考")
        if ref.get("reference_type") == "character":
            ref_lines.append(f"{label}：{desc}。严格保持其外观、体型、颜色和五官。")
        elif ref.get("reference_type") == "prop":
            ref_lines.append(f"{label}：{desc}。保持其形状、颜色和材质。")
        else:
            ref_lines.append(f"{label}：{desc}。")

    identity_lines: list[str] = ["【角色身份约束】"]
    for b in bindings:
        if not b.get("identity_slot_id"):
            continue
        label = _image_no_label(b.get("identity_image_no"), b["identity_slot_id"], syntax)
        identity_lines.append(f"{b['character_name']} 始终保持 {label} 对应的角色身份。")
    if len(bindings) > 1:
        identity_lines.append("各角色必须保持独立外观特征，在整个视频过程中不能发生角色身份交换。")
        names = "、".join(b["character_name"] for b in bindings if b.get("character_name"))
        if names:
            identity_lines.append(f"{names} 的外观特征不得互相迁移。")

    spatial_lines: list[str] = ["【角色空间位置】"]
    for c in spatial.get("characters") or []:
        ini = c.get("initial_position") or {}
        if ini.get("status") != "confirmed":
            continue
        parts = []
        if ini.get("horizontal"):
            parts.append(HORIZONTAL_LABEL.get(ini["horizontal"], ini["horizontal"]))
        if ini.get("depth"):
            parts.append(DEPTH_LABEL.get(ini["depth"], ini["depth"]))
        facing = ini.get("facing_direction")
        line = f"初始画面：{c['character_name']}位于{'、'.join(parts) or '画面中'}"
        if facing:
            line += f"，{FACING_LABEL.get(facing, facing)}"
        spatial_lines.append(line + "。")
        fin = c.get("final_position") or {}
        if fin.get("status") == "confirmed":
            fparts = []
            if fin.get("horizontal"):
                fparts.append(HORIZONTAL_LABEL.get(fin["horizontal"], fin["horizontal"]))
            if fin.get("depth"):
                fparts.append(DEPTH_LABEL.get(fin["depth"], fin["depth"]))
            spatial_lines.append(f"最终画面：{c['character_name']}位于{'、'.join(fparts) or '画面中'}。")
    if len(spatial.get("characters") or []) > 1:
        spatial_lines.append("除非剧情明确要求，否则角色不能无故交换左右位置、改变景深或瞬移。")

    action_lines: list[str] = ["【动作过程】"]
    for a in timeline.get("actions") or []:
        subject = a.get("character_name") or "未指定角色"
        action_lines.append(
            f"进度 {a.get('start_progress')}～{a.get('end_progress')}：{subject}{a.get('action_description') or ''}。"
        )
    if not timeline.get("actions"):
        action_lines.append("按已确认分镜图完成镜头动作。")

    prop_lines: list[str] = ["【道具约束】"]
    for p in props:
        if p.get("reference_id"):
            prop_lines.append(f"{p['prop_name']} 参考 {p.get('reference_slot_id')}。")
        if p.get("holder_character_name"):
            prop_lines.append(f"{p['prop_name']}由{p['holder_character_name']}持有。")
            if p.get("follows_character"):
                prop_lines.append(f"{p['holder_character_name']}移动时，{p['prop_name']}随之移动。")
        prop_lines.append(f"{p['prop_name']}始终保持相同外观，不能无故转移给其他角色。")

    camera_lines = ["【镜头运动】"]
    camera_lines.append(
        f"镜头运动：{camera_movement}。" if camera_movement else "固定镜头，不添加复杂运镜。"
    )

    scene_lines = ["【场景约束】"]
    if scene_title:
        scene_lines.append(f"场景：{scene_title}，保持与分镜图一致的场景空间关系。")
    else:
        scene_lines.append("保持与分镜图一致的场景空间关系。")

    return {
        "reference_bindings": "\n".join(ref_lines),
        "character_identity": "\n".join(identity_lines),
        "scene": "\n".join(scene_lines),
        "spatial": "\n".join(spatial_lines),
        "action_timeline": "\n".join(action_lines),
        "prop": "\n".join(prop_lines),
        "camera": "\n".join(camera_lines),
        "user_text": user_text or "",
        "continuity": "",
        "style": "",
    }


SECTION_ORDER = (
    "reference_bindings",
    "character_identity",
    "scene",
    "spatial",
    "action_timeline",
    "prop",
    "camera",
    "continuity",
    "style",
    "user_text",
)


def compose_final_prompt(sections: dict) -> str:
    """按固定顺序拼装最终提示词；空段自动跳过，不插入空标题。"""
    parts: list[str] = []
    for key in SECTION_ORDER:
        text = (sections.get(key) or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def validate_workspace(
    db: Session,
    *,
    shot: Shot,
    references: list[dict],
    bindings: list[dict],
    spatial: dict,
    timeline: dict,
    props: list[dict],
    final_prompt: str,
    nodes: dict[str, dict],
) -> dict:
    """确定性校验：参考图槽位、角色身份、空间、动作、道具、提示词引用。

    分级：error 阻断生成，warning 需用户确认，pass 全部通过。
    """
    errors: list[dict] = []
    warnings: list[dict] = []
    checks: list[dict] = []

    enabled = [r for r in references if r.get("enabled", True)]
    slots_seen: set[str] = set()
    for ref in enabled:
        slot = ref.get("slot_id") or ""
        if slot in slots_seen:
            errors.append({"code": "DUPLICATE_SLOT", "slot": slot, "message": f"槽位 {slot} 被重复占用"})
        slots_seen.add(slot)
        node = nodes.get(slot)
        if node is None:
            errors.append({
                "code": "SLOT_NOT_SUPPORTED",
                "slot": slot,
                "message": f"当前工作流不支持槽位 {slot}",
            })
            continue
        if ref.get("node_id") and ref["node_id"] != node.get("node_id"):
            warnings.append({
                "code": "NODE_ID_STALE",
                "slot": slot,
                "message": f"槽位 {slot} 的节点映射已随工作流变化，保存的 {ref['node_id']} 已更新为 {node.get('node_id')}",
            })
        if not ref.get("asset_id"):
            errors.append({"code": "REF_ASSET_MISSING", "slot": slot, "message": f"槽位 {slot} 缺少绑定资产"})
            continue
        asset = db.get(Asset, ref["asset_id"])
        if asset is None:
            errors.append({"code": "REF_ASSET_NOT_FOUND", "slot": slot, "message": f"槽位 {slot} 绑定的资产不存在"})
        elif not asset.file_path:
            errors.append({"code": "REF_FILE_MISSING", "slot": slot, "message": f"槽位 {slot} 的参考图文件路径为空"})
        checks.append({
            "code": "REF_SLOT_OK",
            "slot": slot,
            "image_no": node.get("image_index"),
            "message": f"{_image_no_label(node.get('image_index'), slot)}（{slot}）绑定正常，节点 {node.get('node_id')}",
        })
    for slot, node in nodes.items():
        if node.get("required") and slot not in slots_seen:
            errors.append({
                "code": "REQUIRED_SLOT_MISSING",
                "slot": slot,
                "message": f"槽位 {slot} 为当前工作流必需输入，不可缺失",
            })

    for b in bindings:
        if not b.get("character_id"):
            warnings.append({
                "code": "CHARACTER_UNRESOLVED",
                "message": f"出场角色 {b.get('character_name')} 未在角色库中匹配到",
            })
            continue
        if not b.get("identity_slot_id"):
            warnings.append({
                "code": "IDENTITY_REF_MISSING",
                "character_id": b["character_id"],
                "message": f"{b['character_name']} 缺少独立身份参考图，角色一致性无法保证",
            })

    char_ids = {c.get("character_id") for c in (spatial.get("characters") or [])}
    for c in spatial.get("characters") or []:
        if c.get("character_id") not in {b.get("character_id") for b in bindings}:
            errors.append({
                "code": "SPATIAL_CHARACTER_UNKNOWN",
                "character_id": c.get("character_id"),
                "message": f"空间布局中的角色 {c.get('character_name')} 不在本镜出场角色内",
            })
        for key in ("initial_position", "final_position"):
            pos = c.get(key) or {}
            if pos.get("status") != "confirmed":
                warnings.append({
                    "code": "SPATIAL_INCOMPLETE",
                    "character_id": c.get("character_id"),
                    "message": f"{c.get('character_name')} 的{'初始' if key == 'initial_position' else '最终'}位置待确认",
                })
            for axis, allowed in (("x", (0, 1)), ("y", (0, 1))):
                v = pos.get(axis)
                if isinstance(v, (int, float)) and not (allowed[0] <= v <= allowed[1]):
                    errors.append({
                        "code": "SPATIAL_OUT_OF_RANGE",
                        "character_id": c.get("character_id"),
                        "message": f"{c.get('character_name')} 的 {axis} 坐标 {v} 超出 0～1 归一化范围",
                    })

    duration = timeline.get("duration_seconds")
    for a in timeline.get("actions") or []:
        if not a.get("character_id"):
            errors.append({
                "code": "ACTION_SUBJECT_MISSING",
                "action_id": a.get("action_id"),
                "message": f"动作『{a.get('action_description')}』未绑定动作主体",
            })
        elif a["character_id"] not in char_ids:
            errors.append({
                "code": "ACTION_SUBJECT_NOT_IN_SHOT",
                "action_id": a.get("action_id"),
                "message": f"动作主体 {a.get('character_name')} 不在本镜出场角色内",
            })
        start = a.get("start_progress")
        end = a.get("end_progress")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            if start < 0 or end > 1 or start >= end:
                errors.append({
                    "code": "ACTION_TIME_INVALID",
                    "action_id": a.get("action_id"),
                    "message": f"动作『{a.get('action_description')}』时间区间 {start}～{end} 非法",
                })
    if duration is None:
        warnings.append({"code": "DURATION_UNKNOWN", "message": "镜头时长未设置，动作时间轴无法换算为秒"})

    known_props = {p.get("prop_id") for p in props if p.get("prop_id")}
    for p in props:
        if p.get("prop_id") and p.get("holder_character_id") and p["holder_character_id"] not in char_ids:
            warnings.append({
                "code": "PROP_HOLDER_NOT_IN_SHOT",
                "prop_id": p["prop_id"],
                "message": f"道具 {p.get('prop_name')} 的持有者不在本镜出场角色内",
            })
        if p.get("status") == "holder_pending":
            warnings.append({
                "code": "PROP_HOLDER_PENDING",
                "prop_id": p.get("prop_id"),
                "message": f"道具 {p.get('prop_name')} 的持有者待确认",
            })

    prompt = final_prompt or ""
    references_in_prompt = {int(m.group(1)) for m in re.finditer(r"<Picture\s*(\d+)>", prompt)}
    references_in_prompt |= {int(m.group(1)) for m in re.finditer(r"<image(\d+)>", prompt)}
    available_numbers = {
        nodes[r["slot_id"]].get("image_index")
        for r in enabled
        if r.get("slot_id") in nodes and nodes[r["slot_id"]].get("image_index") is not None
    }
    for num in sorted(references_in_prompt):
        if num not in available_numbers:
            errors.append({
                "code": "PROMPT_REF_MISSING",
                "image_no": num,
                "message": f"提示词引用了 Image {num}，但没有对应的启用参考图",
            })
        else:
            checks.append({
                "code": "PROMPT_REF_OK",
                "image_no": num,
                "message": f"Image {num} 引用正确",
            })
    for num in sorted(available_numbers - references_in_prompt):
        slot = next(
            (r["slot_id"] for r in enabled
             if r.get("slot_id") in nodes and nodes[r["slot_id"]].get("image_index") == num),
            "",
        )
        warnings.append({
            "code": "REF_NOT_REFERENCED",
            "image_no": num,
            "slot": slot,
            "message": f"参考图 {slot}（Image {num}）已注入但未被提示词引用",
        })

    for p in props:
        if p.get("prop_name") and p.get("holder_character_name"):
            holders = [
                b["character_name"] for b in bindings
                if b.get("character_name") and b["character_name"] != p["holder_character_name"]
            ]
            for other in holders:
                pattern = f"{other}持有{p['prop_name']}"
                if pattern in prompt:
                    warnings.append({
                        "code": "PROP_HOLDER_CONFLICT",
                        "prop_id": p.get("prop_id"),
                        "message": f"提示词描述 {other} 持有 {p['prop_name']}，与绑定的持有者 {p['holder_character_name']} 冲突",
                    })

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "summary": {
            "references": len(enabled),
            "characters": len(bindings),
            "actions": len(timeline.get("actions") or []),
            "props": len([p for p in props if p.get("prop_id")]),
            "known_props": len(known_props),
        },
    }


def _pos_label(pos):
    """位置字典转可读短语。"""
    data = pos or {}
    parts = [data.get("depth"), data.get("horizontal"), data.get("vertical")]
    shown = [p for p in parts if p]
    if shown:
        return "/".join(shown)
    x, y = data.get("x"), data.get("y")
    if x is not None or y is not None:
        return "(" + str(x) + ", " + str(y) + ")"
    return "未知位置"


def load_saved_config(db, shot_id, target):
    """读取镜头在指定产物目标下已保存的工作台配置；缺失字段保持缺失，不补默认值。"""
    from app.models.regen_config import StoryboardRegenConfig

    if not shot_id or not target:
        return None
    row = (
        db.query(StoryboardRegenConfig)
        .filter(StoryboardRegenConfig.shot_id == shot_id)
        .filter(StoryboardRegenConfig.target == target)
        .first()
    )
    if row is None:
        return None
    return {
        "config_id": row.id,
        "version": row.version,
        "workflow_id": row.workflow_id,
        "workflow_version": row.workflow_version,
        "references": list(row.references or []),
        "character_bindings": list(row.character_bindings or []),
        "spatial_layout": dict(row.spatial_layout or {}),
        "action_timeline": dict(row.action_timeline or {}),
        "prop_bindings": list(row.prop_bindings or []),
        "prompt_sections": dict(row.prompt_sections or {}),
        "final_prompt": row.final_prompt,
        "parameters": dict(row.parameters or {}),
        "extra": dict(row.extra or {}),
    }


def build_continuity(db, shot, *, current_spatial=None, current_props=None):
    """比对上一镜与本镜的结构化状态，输出连续性结论与自然语言衔接描述。

    - 上一镜无已保存结构化配置 -> PREV_STATE_UNKNOWN / PREV_STATE_UNCONFIRMED
    - 位置、景深、朝向变化 -> POSITION_JUMP / DEPTH_JUMP / FACING_CHANGED
    - 道具持有者变化 -> PROP_HOLDER_CHANGED / PROP_HOLDER_UNSET
    - status 取 conflict / ok / first_shot；auto_applied 恒为 False，只提示不改写
    """
    from app.services.shot_chain import shot_project_id

    issues = []
    result = {
        "status": "first_shot",
        "previous_shot_id": None,
        "previous_episode": None,
        "previous_seq": None,
        "previous_config_found": False,
        "issues": issues,
        "summary": "",
        "auto_applied": False,
    }
    project_id = shot_project_id(db, shot)
    if not project_id or shot.episode is None or shot.seq is None:
        issues.append({
            "code": "PREV_STATE_UNKNOWN",
            "level": "info",
            "message": "镜头缺少集数或序号，无法定位上一镜，已跳过连续性比对",
        })
        return result
    from app.services.shot_chain import ordered_episode_shots

    prev = None
    for item in ordered_episode_shots(db, project_id, shot.episode):
        if item.id == shot.id:
            break
        prev = item
    if prev is None:
        return result
    result["previous_shot_id"] = prev.id
    result["previous_episode"] = prev.episode
    result["previous_seq"] = prev.seq
    saved = load_saved_config(db, prev.id, "clip")
    if not saved:
        result["status"] = "ok"
        issues.append({
            "code": "PREV_STATE_UNKNOWN",
            "level": "warning",
            "previous_shot_id": prev.id,
            "message": "上一镜（第 {} 镜）没有已保存的结构化配置，无法逐项比对连续性".format(prev.seq),
        })
        result["summary"] = (
            "衔接上一镜（第 {} 镜）：缺少上一镜的结构化状态，位置与朝向需要人工目视确认。".format(prev.seq)
        )
        return result
    result["previous_config_found"] = True
    prev_spatial = saved.get("spatial_layout") or {}
    prev_chars = {}
    for c in (prev_spatial.get("characters") or []):
        if isinstance(c, dict) and c.get("character_id"):
            prev_chars[c["character_id"]] = c
    for c in ((current_spatial or {}).get("characters") or []):
        if not isinstance(c, dict) or not c.get("character_id"):
            continue
        cid = c["character_id"]
        name = c.get("character_name") or cid
        initial = c.get("initial_position") or {}
        before = prev_chars.get(cid)
        if before is None:
            continue
        final_pos = before.get("final_position") or {}
        if initial.get("status") != "confirmed" or final_pos.get("status") != "confirmed":
            issues.append({
                "code": "PREV_STATE_UNCONFIRMED",
                "level": "warning",
                "character_id": cid,
                "character_name": name,
                "message": "{} 在上一镜落点或本镜起点的位置缺少结构化数据，连续性待确认".format(name),
            })
            continue
        if (
            initial.get("horizontal") != final_pos.get("horizontal")
            or initial.get("vertical") != final_pos.get("vertical")
        ):
            issues.append({
                "code": "POSITION_JUMP",
                "level": "warning",
                "character_id": cid,
                "character_name": name,
                "message": "{} 从上一镜的 {} 变到本镜的 {}，中间没有过渡描述".format(
                    name, _pos_label(final_pos), _pos_label(initial)
                ),
            })
        if final_pos.get("depth") != initial.get("depth"):
            issues.append({
                "code": "DEPTH_JUMP",
                "level": "warning",
                "character_id": cid,
                "character_name": name,
                "message": "{} 的景深由上一镜的 {} 变为本镜的 {}".format(
                    name, final_pos.get("depth"), initial.get("depth")
                ),
            })
        prev_facing = final_pos.get("facing_direction")
        cur_facing = initial.get("facing_direction")
        if prev_facing and cur_facing and prev_facing != cur_facing:
            issues.append({
                "code": "FACING_CHANGED",
                "level": "info",
                "character_id": cid,
                "character_name": name,
                "message": "{} 的朝向由上一镜的 {} 变为本镜的 {}".format(name, prev_facing, cur_facing),
            })
    prev_props = {}
    for p in (saved.get("prop_bindings") or []):
        if isinstance(p, dict) and p.get("prop_id"):
            prev_props[p["prop_id"]] = p
    for p in (current_props or []):
        if not isinstance(p, dict) or not p.get("prop_id"):
            continue
        prop_id = p["prop_id"]
        pname = p.get("prop_name") or prop_id
        holder_now = p.get("holder_character_id")
        if not holder_now:
            issues.append({
                "code": "PROP_HOLDER_UNSET",
                "level": "info",
                "prop_id": prop_id,
                "prop_name": pname,
                "message": "道具 {} 在本镜未绑定持有者，持有关系无法校验".format(pname),
            })
            continue
        before_prop = prev_props.get(prop_id)
        if before_prop is None:
            continue
        holder_before = before_prop.get("holder_character_id")
        if holder_before and holder_before != holder_now:
            name_before = before_prop.get("holder_character_name") or holder_before
            name_now = p.get("holder_character_name") or holder_now
            issues.append({
                "code": "PROP_HOLDER_CHANGED",
                "level": "warning",
                "prop_id": prop_id,
                "prop_name": pname,
                "message": "道具 {} 的持有者由 {} 变为 {}，需确认是否有交接戏".format(
                    pname, name_before, name_now
                ),
            })
    blocking = [i for i in issues if i.get("level") == "error"]
    result["status"] = "conflict" if blocking else "ok"
    if issues:
        described = "；".join(i.get("message", "") for i in issues if i.get("message"))
        result["summary"] = "衔接上一镜（第 {} 镜）：{}。".format(prev.seq, described)
    else:
        # 已比对但无任何冲突时也必须给出明确结论：
        # 空字符串会被前端误判为『尚未检查』
        result["summary"] = (
            "衔接上一镜（第 {} 镜）：已比对上一镜的结构化状态，"
            "位置、景深、朝向与道具持有关系未发现冲突。".format(prev.seq)
        )
    return result


def build_workspace(
    db: Session,
    *,
    shot: Shot,
    target: str,
    workflow: Workflow | None,
    base_config: dict,
    saved: dict | None = None,
) -> dict:
    """构建完整工作区数据：有已保存配置时优先使用，否则由基础配置推导并标注待确认。"""
    nodes = slot_node_map(workflow)
    if saved:
        references = list(saved.get("references") or [])
        bindings = list(saved.get("character_bindings") or [])
        spatial = dict(saved.get("spatial_layout") or {})
        timeline = dict(saved.get("action_timeline") or {})
        props = list(saved.get("prop_bindings") or [])
        sections = dict(saved.get("prompt_sections") or {})
        final_prompt = saved.get("final_prompt") or base_config.get("prompt", {}).get("rendered") or ""
        parameters = dict(saved.get("parameters") or base_config.get("model_params") or {})
        continuity = build_continuity(
            db,
            shot,
            current_spatial=spatial,
            current_props=props,
        )
        if continuity.get("summary") and not sections.get("continuity"):
            sections["continuity"] = continuity["summary"]
        source = "saved"
    else:
        references = build_references(db, base_config, nodes, shot)
        bindings = build_character_bindings(db, references, shot)
        spatial = build_spatial_layout(db, shot, bindings)
        timeline = build_action_timeline(db, shot, bindings, spatial)
        props = build_prop_bindings(db, shot, references)
        from app.models import Scene

        scene = db.get(Scene, shot.scene_id)
        base_prompt = base_config.get("prompt", {}).get("rendered") or ""
        sections = render_prompt_sections(
            references,
            bindings,
            spatial,
            timeline,
            props,
            scene_title=scene.title if scene is not None else None,
            camera_movement=shot.camera_movement,
            user_text=base_prompt,
            syntax=_reference_syntax(nodes),
        )
        continuity = build_continuity(
            db,
            shot,
            current_spatial=spatial,
            current_props=props,
        )
        if continuity.get("summary") and not sections.get("continuity"):
            sections["continuity"] = continuity["summary"]
        final_prompt = base_prompt or compose_final_prompt(sections)
        parameters = dict(base_config.get("model_params") or {})
        source = "derived"

    if target == "clip":
        # 系统槽位（上一镜尾帧/本镜分镜图）以当前生效产物为准，
        # 已保存配置与任务快照里的旧绑定都不可信
        references = refresh_system_references(db, shot, references, nodes)

    validation = validate_workspace(
        db,
        shot=shot,
        references=references,
        bindings=bindings,
        spatial=spatial,
        timeline=timeline,
        props=props,
        final_prompt=final_prompt,
        nodes=nodes,
    )
    base_block = base_config.get("base")
    history_compat = {
        "has_saved_config": bool(saved),
        "saved_config_id": (saved or {}).get("config_id"),
        "saved_version": (saved or {}).get("version"),
        "source": source,
        "base_snapshot_complete": bool(base_config.get("snapshot_complete", False)),
        "incomplete_reasons": list(base_config.get("incomplete_reasons") or []),
        "base_asset_id": (
            base_block.get("asset_id") if isinstance(base_block, dict) else None
        ),
    }
    return {
        "shot_id": shot.id,
        "target": target,
        "source": source,
        "workflow": base_config.get("workflow") or {},
        "base": base_config.get("base"),
        "snapshot_complete": base_config.get("snapshot_complete", False),
        "incomplete_reasons": base_config.get("incomplete_reasons") or [],
        "available_slots": [
            {
                "param": name,
                "node_id": node.get("node_id"),
                "input_key": node.get("input_key"),
                "image_no": node.get("image_index"),
                "required": node.get("required"),
            }
            for name, node in nodes.items()
        ],
        "references": references,
        "character_bindings": bindings,
        "spatial_layout": spatial,
        "action_timeline": timeline,
        "prop_bindings": props,
        "prompt_sections": sections,
        "final_prompt": final_prompt,
        "parameters": parameters,
        "validation": validation,
        "continuity": continuity,
        "history_compat": history_compat,
        "capabilities": {
            "spatial_control": "prompt_level",
            "timeline_control": "prompt_level",
            "reason": "H3 ref2va 工作流仅接受参考图与文本提示词，不支持结构化坐标或时间节点输入",
        },
        "shot": {
            "id": shot.id,
            "episode": shot.episode,
            "seq": shot.seq,
            "duration": shot.duration,
            "camera_movement": shot.camera_movement,
            "shot_type": shot.shot_type,
            "camera_angle": shot.camera_angle,
        },
    }
