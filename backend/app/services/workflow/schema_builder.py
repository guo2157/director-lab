"""参数 / 输出 Schema 构建器（P1）。

把「API Format 工作流 + $param/$output 标记」转成前端可渲染的 Schema。
- 参数白名单：只有带标记的节点输入才进入 Schema，内部节点（模型加载、VAE、CLIP 等）一律不暴露
- 默认值取自工作流节点当前输入值（工作流内置默认），不是凭空造的
- 语义按 WORKFLOW.md §3 的统一参数名映射；未收录的参数按前缀归类

parsed_schema 形状（与 scripts/seed_workflows.py 一致，向后兼容）：
{
  "schema_version": "1.0", "parser_version": "1.0",
  "params": {name: {key,label,type,semantic,required,default,min,max,step,
                    options,group,advanced,order,source,binding:{node_id,input_key}}},
  "outputs": [{key,label,media_type,node_id,multiple,required}],
  "errors": [...], "warnings": [...]
}
"""

from app.services.workflow_inject import parse_workflow

SCHEMA_VERSION = "1.0"
PARSER_VERSION = "1.0"

# 媒体类型判定：节点 class_type → 产物类型
_NODE_MEDIA_TYPE = {
    "LoadImage": "image",
    "LoadImageMask": "image",
    "LoadAudio": "audio",
    "LoadVideo": "video",
    "SaveImage": "image",
    "PreviewImage": "image",
    "SaveVideo": "video",
    "VHS_VideoCombine": "video",
    "CreateVideo": "video",
    "SaveAudio": "audio",
    "SaveAudioMP3": "audio",
    "SaveAudioOpus": "audio",
}

# 参数语义表（WORKFLOW.md §3 统一参数名）
# 字段：semantic / type / label / group / order / required / min / max / step / advanced / description
_SEMANTIC_TABLE: dict[str, dict] = {
    "prompt": {"semantic": "positive_prompt", "type": "textarea", "label": "正向提示词",
               "group": "basic", "order": 10, "required": True},
    "negative": {"semantic": "negative_prompt", "type": "textarea", "label": "负向提示词",
                 "group": "basic", "order": 20,
                 "description": "工作流未绑定该参数时不会下发，业务侧不伪造"},
    "width": {"semantic": "width", "type": "integer", "label": "画面宽度",
              "group": "generation", "order": 30, "min": 256, "max": 2048, "step": 32},
    "height": {"semantic": "height", "type": "integer", "label": "画面高度",
               "group": "generation", "order": 31, "min": 256, "max": 2048, "step": 32},
    "seed": {"semantic": "seed", "type": "integer", "label": "随机种子",
             "group": "generation", "order": 40, "min": -1, "max": 2_147_483_647,
             "advanced": True, "description": "-1 表示随机（同一任务内共用，任务间不共享）"},
    "length": {"semantic": "frame_count", "type": "integer", "label": "帧数",
               "group": "generation", "order": 50, "advanced": True},
    "frames": {"semantic": "frame_count", "type": "integer", "label": "帧数",
               "group": "generation", "order": 50},
    "duration": {"semantic": "duration", "type": "number", "label": "时长（秒）",
                 "group": "generation", "order": 51, "min": 0.1},
    "tts_text": {"semantic": "tts_text", "type": "textarea", "label": "配音文本",
                 "group": "basic", "order": 10, "required": True},
    "tts_voice": {"semantic": "tts_voice", "type": "string", "label": "音色",
                  "group": "basic", "order": 20},
    "tts_voice_ref": {"semantic": "tts_voice_ref", "type": "audio", "label": "音色参考音频",
                      "group": "reference", "order": 60},
    "tts_speed": {"semantic": "tts_speed", "type": "number", "label": "语速",
                  "group": "generation", "order": 30, "min": 0.5, "max": 2.0, "step": 0.1,
                  "advanced": True},
    "tts_emotion": {"semantic": "tts_emotion", "type": "string", "label": "情感",
                    "group": "generation", "order": 31, "advanced": True},
    "bgm_style": {"semantic": "bgm_style", "type": "string", "label": "音乐风格",
                  "group": "basic", "order": 10, "required": True},
    "soundscape": {"semantic": "soundscape", "type": "string", "label": "环境声描述",
                   "group": "basic", "order": 10},
    "first_frame": {"semantic": "first_frame", "type": "image", "label": "首帧",
                    "group": "reference", "order": 60},
    "last_frame": {"semantic": "last_frame", "type": "image", "label": "尾帧",
                   "group": "reference", "order": 61},
    "end_frame": {"semantic": "last_frame", "type": "image", "label": "尾帧",
                  "group": "reference", "order": 61},
    "ref_prev_frame": {"semantic": "previous_shot_last_frame", "type": "image",
                       "label": "上一镜尾帧", "group": "reference", "order": 62},
    "ref_image": {"semantic": "current_storyboard", "type": "image",
                  "label": "当前分镜图", "group": "reference", "order": 63},
    "ref_identity": {"semantic": "character_identity", "type": "image",
                     "label": "角色参考图", "group": "reference", "order": 64},
    "ref_scene": {"semantic": "scene_identity", "type": "image",
                  "label": "场景参考图", "group": "reference", "order": 65},
    "ref_prop": {"semantic": "prop_identity", "type": "image",
                 "label": "道具参考图", "group": "reference", "order": 66},
}

# 参数名 → 输出是否必需（按 trigger.target 判定，WORKFLOW.md §4）
_REQUIRED_OUTPUTS = {
    "keyframe": ("cover",),
    "clip": ("clip",),
    "audio": ("voice",),
    "bgm": ("bgm",),
    "sfx": ("sfx",),
}

_OUTPUT_LABELS = {
    "cover": "生成图片",
    "clip": "生成视频",
    "voice": "生成配音",
    "bgm": "背景音乐",
    "sfx": "音效",
}


def _node_class(definition: dict, node_id: str) -> str:
    node = definition.get(node_id)
    return str(node.get("class_type") or "") if isinstance(node, dict) else ""


def _media_type_of(definition: dict, node_id: str, default: str = "image") -> str:
    return _NODE_MEDIA_TYPE.get(_node_class(definition, node_id), default)


def _raw_default(definition: dict, binding) -> object:
    """工作流节点当前输入值＝工作流内置默认值；连线（list）与媒体不取。"""
    node = definition.get(binding.node_id)
    if not isinstance(node, dict):
        return None
    value = (node.get("inputs") or {}).get(binding.field)
    if isinstance(value, (str, int, float, bool)):
        # 媒体槽位的值是占位图文件名，不能当默认值
        if binding.media:
            return None
        # 工作流里标注「运行时注入」的占位提示词不能当默认值展示给用户
        if isinstance(value, str) and "injected at runtime" in value:
            return None
        return value
    return None


def _classify(name: str, binding, definition: dict) -> dict:
    """参数归类：先查语义表，再按 ref_ 前缀兜底。"""
    base = dict(_SEMANTIC_TABLE.get(name) or {})
    if not base:
        if name.startswith("ref_character") or name.startswith("ref_char"):
            base = {"semantic": "character_identity", "type": "image",
                    "label": f"角色参考图（{name}）", "group": "reference", "order": 70}
        elif name.startswith("ref_prop"):
            base = {"semantic": "prop_identity", "type": "image",
                    "label": f"道具参考图（{name}）", "group": "reference", "order": 71}
        elif name.startswith("ref_scene"):
            base = {"semantic": "scene_identity", "type": "image",
                    "label": f"场景参考图（{name}）", "group": "reference", "order": 72}
        elif name.startswith("ref_audio"):
            base = {"semantic": "audio_reference", "type": "audio",
                    "label": f"参考音频（{name}）", "group": "reference", "order": 73,
                    "advanced": True}
        elif name.startswith("ref_") or name.startswith("image"):
            base = {"semantic": "general_reference", "type": "image",
                    "label": f"参考图（{name}）", "group": "reference", "order": 74}
        else:
            base = {"semantic": name, "type": "string", "label": name,
                    "group": "advanced", "order": 90}
    # 媒体参数类型以真实节点为准（LoadImage / LoadAudio / LoadVideo）
    if binding.media:
        base["type"] = _media_type_of(definition, binding.node_id, base.get("type") or "image")
        base.setdefault("group", "reference")
    return base


def build_schema(definition: dict, *, capabilities: dict | None = None,
                 trigger: dict | None = None) -> dict:
    """构建 parsed_schema。标记非法时把错误带回，不抛异常（便于落库排查）。"""
    parsed = parse_workflow(definition if isinstance(definition, dict) else {})
    params: dict[str, dict] = {}
    for name, binding in parsed.params.items():
        meta = _classify(name, binding, definition)
        param = {
            "key": name,
            "label": meta.get("label") or name,
            "type": meta.get("type") or "string",
            "semantic": meta.get("semantic") or name,
            "required": bool(binding.required or meta.get("required")),
            "default": _raw_default(definition, binding),
            "source": "business_or_user" if meta.get("group") != "reference" else "business_asset",
            "group": meta.get("group") or "advanced",
            "order": int(meta.get("order") or 90),
            "advanced": bool(meta.get("advanced")),
            "binding": {"node_id": binding.node_id, "input_key": binding.field},
        }
        for extra in ("min", "max", "step", "options", "placeholder", "description", "visible_if"):
            if meta.get(extra) is not None:
                param[extra] = meta[extra]
        # 能力声明里的帧数/分辨率约束优先于静态表（同一工作流可自定义）
        caps = capabilities or {}
        if param["semantic"] == "frame_count" and caps.get("frame_count_range"):
            rng = caps["frame_count_range"]
            if isinstance(rng, (list, tuple)) and len(rng) == 2:
                param["min"], param["max"] = int(rng[0]), int(rng[1])
            if caps.get("frame_count_step"):
                param["step"] = int(caps["frame_count_step"])
        params[name] = param

    required_outputs = _REQUIRED_OUTPUTS.get(str((trigger or {}).get("target") or ""), ())
    outputs: list[dict] = []
    for out_name in sorted(parsed.outputs):
        node_id = next(
            (nid for nid, node in definition.items()
             if isinstance(node, dict)
             and f"$output.{out_name}" in str((node.get("_meta") or {}).get("title") or "")),
            None,
        )
        media_type = _media_type_of(definition, node_id or "", "image")
        outputs.append({
            "key": out_name,
            "label": _OUTPUT_LABELS.get(out_name, out_name),
            "media_type": media_type,
            "node_id": node_id,
            "multiple": media_type == "image",
            "required": out_name in required_outputs or not required_outputs,
        })

    errors = list(parsed.errors)
    warnings = list(parsed.warnings)
    if "width" in params and "height" not in params:
        warnings.append("仅绑定 width 未绑定 height：该侧以工作流内置值为准（WORKFLOW.md §5）")
    if "height" in params and "width" not in params:
        warnings.append("仅绑定 height 未绑定 width：该侧以工作流内置值为准（WORKFLOW.md §5）")

    return {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "params": params,
        "outputs": outputs,
        "errors": errors,
        "warnings": warnings,
    }


def public_schema(schema: dict | None) -> dict:
    """对外 Schema：参数字典转数组、剥离节点绑定（前端不需要也不允许知道 node_id）。"""
    schema = schema or {}
    params = []
    for name, param in (schema.get("params") or {}).items():
        item = {k: v for k, v in param.items() if k != "binding"}
        item.setdefault("key", name)
        params.append(item)
    params.sort(key=lambda p: (int(p.get("order") or 90), str(p.get("key"))))
    outputs = [
        {k: v for k, v in out.items() if k != "node_id"}
        for out in (schema.get("outputs") or [])
    ]
    return {
        "schema_version": schema.get("schema_version") or SCHEMA_VERSION,
        "parser_version": schema.get("parser_version") or PARSER_VERSION,
        "params": params,
        "outputs": outputs,
        "errors": list(schema.get("errors") or []),
        "warnings": list(schema.get("warnings") or []),
    }
