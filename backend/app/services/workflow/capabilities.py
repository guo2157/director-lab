"""CapabilityRegistry（P2）：把各工作流 meta 里自定义的能力字段归一化成统一结构。

为什么需要：现有 meta 的能力字段是逐工作流手写的平铺结构（`audio`、`frame_count_*`、
`tier`、`reference_slots` 混用字符串数组与对象数组两种写法），无法回答
「这个工作流能不能接 TTS 音频」「最多几张图」「是不是 H3 VA」这类问题。
业务层若自己去猜，就会退回「按名称推断能力」的老路（审计 D5）。

归一化后的结构（供 matcher / 前端 / 校验共用）：

{
  "workflow_type": "video",
  "generation_modes": ["image_to_video", "reference_to_video", "multi_reference"],
  "modalities": {"image_input": true, "audio_input": true, "video_input": false,
                 "image_output": false, "video_output": true, "audio_output": true},
  "audio": {"accepts_tts_reference_audio": true, "max_audio_inputs": 3,
            "audio_output_mode": "embedded", "supports_native_audio": true},
  "video": {"supports_first_frame": false, "supports_last_frame": false,
            "supports_previous_tail_frame": true, "duration_control": true,
            "frame_count_range": [124, 362], "fps": 24},
  "reference": {"supports_multi_reference": true, "max_image_references": 9,
                "slots": [{"param": "ref_identity", "input_key": "...", "optional": false,
                           "semantic": "character_identity", "media_type": "image"}]},
  "constraints": {"resolution": {...}},
  "source": {"tier": "std_5s", "local_only": true, "cost": "free_local"}
}

全部字段来自工作流真实标记与 meta，不做名称推测；缺失即 null/false，并由
`missing_capabilities()` 让调用方显式报错，不静默降级。
"""

from app.services.workflow.schema_builder import build_schema

# 槽位名 → (semantic, media_type)。与 schema_builder 的语义表同源，避免两处漂移。
_SLOT_SEMANTIC = {
    "ref_prev_frame": ("previous_shot_last_frame", "image"),
    "ref_image": ("current_storyboard", "image"),
    "ref_identity": ("character_identity", "image"),
    "ref_scene": ("scene_identity", "image"),
    "ref_prop": ("prop_identity", "image"),
    "first_frame": ("first_frame", "image"),
    "end_frame": ("last_frame", "image"),
    "last_frame": ("last_frame", "image"),
    "tts_voice_ref": ("voice_reference_audio", "audio"),
}

# workflow_type + 槽位/音频输入 → 细分生成模式（AGENTS.md #6：细分类型只放这里）
def _generation_modes(workflow_type: str, has_image_slots: bool, has_audio_slots: bool,
                      has_first_frame: bool, has_last_frame: bool) -> list[str]:
    modes: list[str] = []
    if workflow_type == "image":
        modes.append("text_to_image")
        if has_image_slots:
            modes.append("image_to_image")
            modes.append("multi_reference") if _multi(has_image_slots) else None
    elif workflow_type == "video":
        modes.append("text_to_video")
        if has_image_slots:
            modes.append("image_to_video")
            modes.append("reference_to_video")
            modes.append("multi_reference")
        if has_first_frame:
            modes.append("first_last_frame_to_video")
        if has_audio_slots:
            modes.append("audio_conditioned_video")
    elif workflow_type == "tts":
        modes.append("text_to_speech")
    elif workflow_type == "bgm":
        modes.append("text_to_music")
    elif workflow_type == "sfx":
        modes.append("text_to_sound")
    return [m for m in modes if m]


def _multi(has_slots: bool) -> bool:
    return bool(has_slots)


def _slot_entries(capabilities: dict, schema: dict) -> list[dict]:
    """参考槽位统一成对象数组。

    来源有两处，必须合并——这是避免「按名称推断能力」的关键：
    1. **真实标记**（schema.params 里 type ∈ image/audio/video）：工作流实际接什么，以它为准；
       H3 的 `ref_audio_0..2` 只存在于节点标记，meta 的 reference_slots 里并没有。
    2. **meta 的 reference_slots**：只用于补充 input_key / optional 这类声明信息。
    meta 声明了但标记里不存在的槽位一律忽略（声明与实现不符时以实现为准）。
    """
    declared = capabilities.get("reference_slots") or []
    declared_by_param: dict[str, dict] = {}
    for item in declared:
        if isinstance(item, str):
            declared_by_param[item] = {"param": item, "input_key": None, "optional": True}
        elif isinstance(item, dict):
            param = item.get("param") or item.get("key")
            if param:
                declared_by_param[param] = {
                    "param": param,
                    "input_key": item.get("input_key"),
                    "optional": bool(item.get("optional", True)),
                }

    entries: list[dict] = []
    for name, param in (schema.get("params") or {}).items():
        media_type = param.get("type")
        if media_type not in ("image", "audio", "video"):
            continue
        semantic = param.get("semantic")
        if media_type == "audio":
            # H3 的 ref_audio_N 承载本镜台词 → voice_audio；克隆参考音频 → voice_reference_audio
            semantic = ("voice_reference_audio"
                        if name.startswith("tts_voice_ref") or name.endswith("voice_ref")
                        else "voice_audio")
        elif semantic is None:
            semantic, media_type = _SLOT_SEMANTIC.get(name, (None, None))
        info = declared_by_param.get(name) or {}
        entries.append({
            "param": name,
            "input_key": info.get("input_key"),
            "optional": info.get("optional", not bool(param.get("required"))),
            "semantic": semantic,
            "media_type": media_type,
        })
    return entries


def normalize(workflow_type: str, definition: dict,
              capabilities: dict | None = None, trigger: dict | None = None) -> dict:
    """返回归一化能力。definition 必须真实存在——能力从标记与节点读，不从名字读。"""
    caps = dict(capabilities or {})
    schema = build_schema(definition, capabilities=caps, trigger=trigger)
    slots = _slot_entries(caps, schema)
    image_slots = [s for s in slots if s["media_type"] == "image"]
    audio_slots = [s for s in slots if s["media_type"] == "audio"]
    video_slots = [s for s in slots if s["media_type"] == "video"]
    param_names = {s["param"] for s in slots}

    outputs = {o["key"]: o for o in (schema.get("outputs") or [])}
    out_types = {o.get("media_type") for o in outputs.values()}
    audio_output_mode = caps.get("audio_output_mode") or (
        "embedded" if caps.get("audio") and "video" in out_types else
        "separate" if "voice" in outputs else
        "none" if out_types else None
    )
    frame_range = caps.get("frame_count_range")
    tier = caps.get("tier")

    return {
        "workflow_type": workflow_type,
        "generation_modes": _generation_modes(
            workflow_type,
            has_image_slots=bool(image_slots),
            has_audio_slots=bool(audio_slots),
            has_first_frame="first_frame" in param_names,
            has_last_frame=bool({"end_frame", "last_frame"} & param_names),
        ),
        "modalities": {
            "image_input": bool(image_slots),
            "audio_input": bool(audio_slots),
            "video_input": bool(video_slots),
            "image_output": "image" in out_types,
            "video_output": "video" in out_types,
            "audio_output": bool(out_types & {"audio"}) or bool(caps.get("audio")),
        },
        "audio": {
            # H3 的 ref_audio_N 承载本镜台词语音，即计划里的 voice_audio
            "accepts_tts_reference_audio": bool(audio_slots),
            "max_audio_inputs": len(audio_slots),
            "audio_output_mode": audio_output_mode,
            "supports_native_audio": bool(caps.get("audio")),
            "voice_mode": caps.get("voice_mode"),
        },
        "video": {
            "supports_first_frame": "first_frame" in param_names,
            "supports_last_frame": bool({"end_frame", "last_frame"} & param_names),
            "supports_previous_tail_frame": "ref_prev_frame" in param_names,
            "duration_control": bool(frame_range or caps.get("duration_param")),
            "frame_count_range": frame_range,
            "fps": caps.get("fps"),
            "resolution_constraints": caps.get("resolution_constraints"),
        },
        "reference": {
            "supports_multi_reference": len(image_slots) > 1,
            "max_image_references": caps.get("max_reference_images") or len(image_slots) or 0,
            "slots": slots,
        },
        "constraints": {
            "resolution": caps.get("resolution_constraints"),
            "required_params": list((trigger or {}).get("required_params") or []),
        },
        "source": {
            "tier": tier,
            "local_only": caps.get("local_only"),
            "cost": caps.get("cost"),
            "schema_errors": list(schema.get("errors") or []),
        },
    }


def missing_capabilities(capabilities: dict, required: dict) -> list[str]:
    """按 `{能力路径: 期望值}` 检查，返回缺失项的点路径（matcher 用）。

    required 例：{"modalities.audio_input": True, "audio.accepts_tts_reference_audio": True}
    """
    missing: list[str] = []
    for path, expected in (required or {}).items():
        node: object = capabilities
        ok = True
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                ok = False
                break
            node = node[part]
        if not ok or node != expected:
            missing.append(path)
    return missing


def capability_matches(capabilities: dict, required: dict) -> tuple[bool, list[str]]:
    missing = missing_capabilities(capabilities, required)
    return (not missing, missing)
