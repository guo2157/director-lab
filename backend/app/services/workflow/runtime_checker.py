"""运行环境检查（P3）：工作流能解析 ≠ 当前 ComfyUI 实例能执行。

检查两类依赖，全部来自真实 `/object_info`，不读文档、不猜名称：
1. **节点依赖**：definition 里出现的每个 `class_type` 是否已在实例注册（缺失即无法提交）
2. **模型依赖**：definition 里引用的权重文件名是否出现在任一加载器节点的可选项里

状态语义（写入 `workflow.runtime_status`）：
- `unchecked`     未检查
- `ready`         节点与模型齐全
- `missing_deps`  缺节点或缺模型（附明细）
- `unreachable`   实例连不上（不代表工作流有问题）

`submit_test()` 走真实提交：用 Schema 默认值编译最小 prompt，可选槽位一律剔除；
必需媒体槽位无法自造文件，直接拒绝并列出缺什么——不伪造「测试通过」。
"""

from typing import Any

from app.integrations.comfyui import ComfyUIClient, ComfyUIError
from app.services.workflow.schema_builder import build_schema
from app.services.workflow.versions import _dependency_snapshot
from app.services.workflow_inject import inject_params, prune_unbound_media_inputs

# 加载器节点里代表「可选文件列表」的输入键（/object_info 的 required 首元素为候选值数组）
_MODEL_INPUT_KEYS = (
    "ckpt_name", "unet_name", "vae_name", "clip_name", "clip_name1", "clip_name2",
    "lora_name", "control_net_name", "style_model_name", "model_name",
    "audio_encoder_name", "text_encoder", "diffusion_model", "model",
)


def _options_from_object_info(object_info: dict) -> set[str]:
    """把 /object_info 里所有加载器的候选文件名摊平成一个集合。"""
    options: set[str] = set()
    for _class_type, spec in (object_info or {}).items():
        if not isinstance(spec, dict):
            continue
        required = ((spec.get("input") or {}).get("required") or {})
        for key, value in required.items():
            if key not in _MODEL_INPUT_KEYS and not key.endswith("_name"):
                continue
            if isinstance(value, list) and value and isinstance(value[0], list):
                options.update(str(v) for v in value[0] if isinstance(v, (str, int)))
    return options


def default_base_url() -> str | None:
    """默认实例：优先取实例池里第一个启用项，其次 settings.comfyui_base_url。

    实例池是运行时的真实配置来源（`comfyui_instances_json`），
    `settings.comfyui_base_url` 在启用多实例后可能为空——这正是首次实机检查
    拿到空 base_url 的原因。
    """
    from app.core.config import get_settings
    from app.integrations import comfyui_pool

    try:
        instances = comfyui_pool.enabled_instances()
    except Exception:  # noqa: BLE001 - 实例池未初始化时退回单实例配置
        instances = []
    if instances:
        return instances[0].get("base_url")
    return get_settings().comfyui_base_url or None


def _make_client(base_url: str | None, client: Any) -> Any:
    return client or ComfyUIClient(base_url=base_url or default_base_url())


def check_definition(definition: dict, capabilities: dict | None = None, *,
                     base_url: str | None = None, client: Any = None) -> dict:
    """对单个实例做依赖检查。client 可注入（测试用），缺省走真实 ComfyUI。"""
    client = _make_client(base_url, client)
    snapshot = _dependency_snapshot(definition)
    required_nodes = set(snapshot["node_types"])
    required_models = set(snapshot["model_files"])

    try:
        object_info = client._request("GET", "/object_info")  # noqa: SLF001 - 复用既有客户端
    except ComfyUIError as e:
        return {
            "runtime_status": "unreachable",
            "instance": client.base_url,
            "error": e.message,
            "missing_node_types": sorted(required_nodes),
            "missing_models": sorted(required_models),
            "checked_nodes": 0,
        }

    installed = set(object_info.keys()) if isinstance(object_info, dict) else set()
    available_files = _options_from_object_info(object_info)
    missing_nodes = sorted(required_nodes - installed)
    # 模型只在实例能报出候选清单时才判定；清单为空（老版本/自定义加载器）不误报
    missing_models = sorted(required_models - available_files) if available_files else []

    status = "ready" if not missing_nodes and not missing_models else "missing_deps"
    return {
        "runtime_status": status,
        "instance": client.base_url,
        "checked_nodes": len(installed),
        "required_nodes": len(required_nodes),
        "required_models": len(required_models),
        "missing_node_types": missing_nodes,
        "missing_models": missing_models,
        "node_types": snapshot["node_types"],
    }


def _test_values(definition: dict, schema: dict) -> tuple[dict, list[str]]:
    """按 Schema 默认值构造测试参数；必需媒体槽位无法自造，返回缺口名称。"""
    values: dict = {}
    blockers: list[str] = []
    for name, param in (schema.get("params") or {}).items():
        default = param.get("default")
        ptype = param.get("type")
        if ptype in ("image", "audio", "video"):
            if param.get("required"):
                blockers.append(name)
            continue
        if default is not None:
            values[name] = default
        elif param.get("required"):
            # 文本类必填项用可识别占位文案，方便在 ComfyUI 侧辨认这是测试产物
            values[name] = "director-lab workflow test"
    return values, blockers


def submit_test(definition: dict, capabilities: dict | None = None, *,
                trigger: dict | None = None, base_url: str | None = None,
                client: Any = None) -> dict:
    """真实提交一次测试生成，返回 prompt_id（不等结果）。"""
    schema = build_schema(definition, capabilities=capabilities, trigger=trigger)
    values, blockers = _test_values(definition, schema)
    if blockers:
        return {
            "submitted": False,
            "reason": "workflow_test_requires_media",
            "message": "以下必需媒体槽位无法自动构造测试文件，请用测试页上传后再试：" + "、".join(blockers),
            "blocking_slots": blockers,
        }

    trimmed, pruned = prune_unbound_media_inputs(definition, values)
    try:
        prompt, snapshot = inject_params(trimmed, values)
    except ValueError as e:
        return {"submitted": False, "reason": "workflow_marker_missing", "message": str(e)}

    client = _make_client(base_url, client)
    try:
        prompt_id = client.submit_prompt(prompt, client_id="director-lab-workflow-test")
    except ComfyUIError as e:
        return {"submitted": False, "reason": e.code, "message": e.message,
                "instance": client.base_url}
    return {
        "submitted": True,
        "prompt_id": prompt_id,
        "instance": client.base_url,
        "pruned_slots": pruned,
        "injected_params": sorted(snapshot.keys()),
        "note": "已提交真实任务；用 GET /api/workflows/test/{prompt_id} 查询状态",
    }


def test_status(prompt_id: str, *, base_url: str | None = None, client: Any = None) -> dict:
    client = _make_client(base_url, client)
    try:
        history = client.get_history(prompt_id)
    except ComfyUIError as e:
        return {"prompt_id": prompt_id, "status": "unknown", "error": e.message,
                "instance": client.base_url}
    if history is None:
        try:
            queue = client.get_queue()
        except ComfyUIError:
            queue = {}
        return {
            "prompt_id": prompt_id,
            "status": "running_or_queued",
            "instance": client.base_url,
            "queue_running": len(queue.get("queue_running") or []),
            "queue_pending": len(queue.get("queue_pending") or []),
        }
    done, err = client.is_job_done(history)
    outputs = client.extract_outputs(history)
    return {
        "prompt_id": prompt_id,
        "status": "succeeded" if done and not err else ("failed" if err else "running"),
        "error": err,
        "instance": client.base_url,
        "artifacts": {
            node_id: [item["filename"] for item in items] for node_id, items in outputs.items()
        },
        "artifact_count": sum(len(v) for v in outputs.values()),
    }
