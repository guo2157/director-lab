"""ComfyUI 实例池：多实例加权负载均衡与故障转移（替代原三方通道降级机制）。

- 实例配置：[{base_url, weight, enabled}]，经设置页管理，运行时生效
- 选择策略：加权轮询（weighted round-robin），权重为正整数
- 故障转移：获取客户端时探测实例，不可达则按权重尝试其余实例
- 未配置实例时回退到 comfyui_base_url 单实例（兼容旧配置）
"""
import json
import random
import threading
from app.core.config import get_settings
from app.integrations.comfyui import ComfyUIClient, ComfyUIError

_lock = threading.Lock()
_rr_counter = 0


def _normalize(raw: list) -> list[dict]:
    instances: list[dict] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        base_url = (it.get("base_url") or "").strip()
        if not base_url:
            continue
        try:
            weight = max(1, int(it.get("weight", 1)))
        except (TypeError, ValueError):
            weight = 1
        instances.append({
            "base_url": base_url,
            "weight": weight,
            "enabled": bool(it.get("enabled", True)),
        })
    return instances


def list_instances() -> list[dict]:
    """读取全部实例配置；未配置时回退到 comfyui_base_url 单实例。"""
    s = get_settings()
    try:
        raw = json.loads(s.comfyui_instances_json or "[]")
    except (TypeError, ValueError):
        raw = []
    instances = _normalize(raw if isinstance(raw, list) else [])
    if not instances and s.comfyui_base_url:
        instances = [{"base_url": s.comfyui_base_url, "weight": 1, "enabled": True}]
    return instances


def set_instances(instances: list[dict]) -> list[dict]:
    """保存实例配置（运行时生效，写入缓存的 Settings 对象）。"""
    normalized = _normalize(instances)
    s = get_settings()
    s.comfyui_instances_json = json.dumps(normalized, ensure_ascii=False)
    return normalized


def enabled_instances() -> list[dict]:
    return [i for i in list_instances() if i["enabled"]]


def pick_instance() -> dict | None:
    """加权轮询选择一个启用实例。"""
    global _rr_counter
    pool = enabled_instances()
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    seq: list[int] = []
    for idx, inst in enumerate(pool):
        seq.extend([idx] * inst["weight"])
    with _lock:
        pick = seq[_rr_counter % len(seq)]
        _rr_counter += 1
    return pool[pick]


def _pick_among(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return random.choices(candidates, weights=[c["weight"] for c in candidates], k=1)[0]


def acquire_client(preferred: dict | None = None) -> tuple[ComfyUIClient, dict]:
    """获取可用实例的客户端：优先 preferred（或加权轮询选择），
    探测失败时按权重尝试其余启用实例，全部失败抛 comfy_unreachable。"""
    tried: list[str] = []
    inst = preferred if preferred else pick_instance()
    while inst is not None:
        client = ComfyUIClient(base_url=inst["base_url"])
        try:
            client.test_connection()
            return client, inst
        except ComfyUIError:
            tried.append(inst["base_url"])
            remaining = [i for i in enabled_instances() if i["base_url"] not in tried]
            inst = _pick_among(remaining)
    raise ComfyUIError("comfy_unreachable", "所有 ComfyUI 实例均不可用")
