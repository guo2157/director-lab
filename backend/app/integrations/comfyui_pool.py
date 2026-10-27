"""ComfyUI 实例池：多实例加权负载均衡与故障转移（替代原三方通道降级机制）。
- 实例配置：[{base_url, weight, enabled}]，经设置页管理，运行时生效
- 选择策略：加权轮询（weighted round-robin），权重为正整数
- 故障转移：获取客户端时探测实例，不可达则按权重尝试其余实例
- 未配置实例时回退到 comfyui_base_url 单实例（兼容旧配置）
- 健康检查熔断（D-19）：后台心跳探测实例可达性，连续失败 N 次熔断摘除，
  半开探测恢复；acquire_client 优先选择健康实例，全部熔断时降级为直接尝试
"""
import json
import random
import threading
import time
from app.core.config import get_settings, save_runtime_settings
from app.integrations.comfyui import ComfyUIClient, ComfyUIError

_lock = threading.Lock()
_rr_counter = 0

# ---- 健康检查熔断状态（进程内） ----
_health_lock = threading.Lock()
_health_state: dict[str, dict] = {}  # base_url -> {fail_count, last_check, circuit_open, open_until}

HEARTBEAT_INTERVAL = 30.0       # 心跳探测间隔（秒）
FAILURE_THRESHOLD = 3           # 连续失败次数达到阈值后熔断
CIRCUIT_OPEN_DURATION = 60.0    # 熔断持续时间（秒），过后进入半开
HALF_OPEN_PROBE_INTERVAL = 10.0 # 半开状态下探测间隔（秒）


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
    save_runtime_settings(s)
    return normalized


def enabled_instances() -> list[dict]:
    return [i for i in list_instances() if i["enabled"]]


def _get_health(base_url: str) -> dict:
    """获取或初始化实例的健康状态。"""
    with _health_lock:
        if base_url not in _health_state:
            _health_state[base_url] = {
                "fail_count": 0,
                "last_check": 0.0,
                "circuit_open": False,
                "open_until": 0.0,
            }
        return _health_state[base_url]


def _record_success(base_url: str) -> None:
    """记录实例探测成功，重置失败计数并关闭熔断。"""
    with _health_lock:
        h = _health_state.get(base_url)
        if h:
            h["fail_count"] = 0
            h["circuit_open"] = False
            h["open_until"] = 0.0
            h["last_check"] = time.time()


def _record_failure(base_url: str) -> None:
    """记录实例探测失败，达到阈值后熔断。"""
    with _health_lock:
        h = _health_state.get(base_url)
        if not h:
            return
        h["fail_count"] += 1
        h["last_check"] = time.time()
        if h["fail_count"] >= FAILURE_THRESHOLD and not h["circuit_open"]:
            h["circuit_open"] = True
            h["open_until"] = time.time() + CIRCUIT_OPEN_DURATION


def is_circuit_open(base_url: str) -> bool:
    """判断实例是否处于熔断状态。"""
    with _health_lock:
        h = _health_state.get(base_url)
        if not h or not h["circuit_open"]:
            return False
        # 熔断时间已过，进入半开状态（允许探测）
        if time.time() >= h["open_until"]:
            h["circuit_open"] = False
            h["fail_count"] = FAILURE_THRESHOLD - 1  # 半开：再失败一次即重新熔断
            return False
        return True


def get_health_summary() -> list[dict]:
    """获取所有实例的健康摘要（供监控面板使用）。"""
    summary = []
    for inst in list_instances():
        url = inst["base_url"]
        h = _get_health(url)
        summary.append({
            "base_url": url,
            "weight": inst["weight"],
            "enabled": inst["enabled"],
            "fail_count": h["fail_count"],
            "circuit_open": h["circuit_open"],
            "last_check": h["last_check"],
        })
    return summary


def _probe_instance(base_url: str) -> bool:
    """探测单个实例可达性。"""
    try:
        client = ComfyUIClient(base_url=base_url, timeout=5.0)
        client.test_connection()
        _record_success(base_url)
        return True
    except (ComfyUIError, Exception):
        _record_failure(base_url)
        return False


def _heartbeat_loop() -> None:
    """后台心跳：定期探测所有启用实例，维护熔断状态。"""
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        try:
            for inst in enabled_instances():
                url = inst["base_url"]
                h = _get_health(url)
                # 熔断中且未到半开时间，跳过探测
                if h["circuit_open"] and time.time() < h["open_until"]:
                    continue
                # 半开状态下限制探测频率
                if not h["circuit_open"] and h["fail_count"] >= FAILURE_THRESHOLD - 1:
                    if time.time() - h["last_check"] < HALF_OPEN_PROBE_INTERVAL:
                        continue
                _probe_instance(url)
        except Exception:
            pass  # 心跳循环不因单次异常退出


_heartbeat_thread: threading.Thread | None = None
_heartbeat_started = False


def start_heartbeat() -> None:
    """启动后台心跳线程（幂等，worker 启动时调用）。"""
    global _heartbeat_thread, _heartbeat_started
    with _health_lock:
        if _heartbeat_started:
            return
        _heartbeat_started = True
    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True, name="comfyui-heartbeat")
    _heartbeat_thread.start()


def pick_instance() -> dict | None:
    """加权轮询选择一个启用且未熔断的实例。"""
    global _rr_counter
    pool = [i for i in enabled_instances() if not is_circuit_open(i["base_url"])]
    if not pool:
        # 全部熔断时降级：仍从全部启用实例中选择（直接尝试）
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


def get_instance_load(inst: dict) -> int | None:
    """查询实例实时负载（ComfyUI 队列排队数 + 执行中数）。不可达返回 None。"""
    try:
        client = ComfyUIClient(base_url=inst["base_url"], timeout=5.0)
        queue = client.get_queue()
        pending = len(queue.get("queue_pending", []))
        running = len(queue.get("queue_running", []))
        return pending + running
    except Exception:
        return None


def pick_instance_by_load(exclude: list[str] | None = None) -> dict | None:
    """按实时负载选择实例（least-connections）：在健康且负载可查询的实例中
    选择负载最轻者；全部无法查询负载时降级为加权轮询。
    exclude 为要排除的 base_url 列表（任务级故障转移重试时跳过已失败实例）。"""
    excluded = set(exclude or [])
    pool = [i for i in enabled_instances()
            if not is_circuit_open(i["base_url"]) and i["base_url"] not in excluded]
    if not pool:
        pool = [i for i in enabled_instances() if i["base_url"] not in excluded]
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    best, best_score = None, None
    for inst in pool:
        load = get_instance_load(inst)
        if load is None:
            continue
        # 评分：实际负载 / 权重（权重越高可承担越多负载），同分时取权重高者
        score = (load / inst["weight"], -inst["weight"])
        if best_score is None or score < best_score:
            best, best_score = inst, score
    return best if best is not None else pick_instance()


def acquire_client_by_load(preferred: dict | None = None, exclude: list[str] | None = None) -> tuple[ComfyUIClient, dict]:
    """按实时负载获取客户端：优先 preferred（或 least-connections 选择），
    探测失败时按权重尝试其余启用实例，全部失败抛 comfy_unreachable。
    探测结果同步更新健康状态。
    exclude 为要排除的 base_url 列表（任务级故障转移重试时使用）。"""
    tried: list[str] = list(exclude or [])
    inst = preferred if preferred else pick_instance_by_load(exclude=exclude)
    while inst is not None:
        client = ComfyUIClient(base_url=inst["base_url"])
        try:
            client.test_connection()
            _record_success(inst["base_url"])
            return client, inst
        except ComfyUIError:
            _record_failure(inst["base_url"])
            tried.append(inst["base_url"])
            remaining = [
                i for i in enabled_instances()
                if i["base_url"] not in tried and not is_circuit_open(i["base_url"])
            ]
            if not remaining:
                remaining = [i for i in enabled_instances() if i["base_url"] not in tried]
            inst = _pick_among(remaining)
    raise ComfyUIError("comfy_unreachable", "所有 ComfyUI 实例均不可用")


def acquire_client(preferred: dict | None = None) -> tuple[ComfyUIClient, dict]:
    """获取可用实例的客户端：优先 preferred（或加权轮询选择），
    探测失败时按权重尝试其余启用实例，全部失败抛 comfy_unreachable。
    探测结果同步更新健康状态（成功重置计数，失败累计计数）。"""
    tried: list[str] = []
    inst = preferred if preferred else pick_instance()
    while inst is not None:
        client = ComfyUIClient(base_url=inst["base_url"])
        try:
            client.test_connection()
            _record_success(inst["base_url"])
            return client, inst
        except ComfyUIError:
            _record_failure(inst["base_url"])
            tried.append(inst["base_url"])
            remaining = [
                i for i in enabled_instances()
                if i["base_url"] not in tried and not is_circuit_open(i["base_url"])
            ]
            if not remaining:
                remaining = [i for i in enabled_instances() if i["base_url"] not in tried]
            inst = _pick_among(remaining)
    raise ComfyUIError("comfy_unreachable", "所有 ComfyUI 实例均不可用")
