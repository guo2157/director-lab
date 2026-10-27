"""设置管理接口：API.md §11 字段级契约。
- GET/PUT /api/settings：全局配置读写，密钥掩码
- POST test-comfyui/test-llm：连通性测试
- GET/PUT /api/settings/comfyui-instances：多 ComfyUI 实例配置（加权负载均衡）
- POST comfyui-instances/test：逐实例连通性测试
- POST clear-upload-cache：清理上传缓存
- GET /api/health：健康状态
"""
import time
import os
import shutil
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.core.config import get_settings, save_runtime_settings
from app.core.database import get_db, engine
from app.services import errors
from app.integrations import comfyui_pool
import httpx

router = APIRouter(prefix="/api/settings", tags=["settings"])

_last_cache_clear: dict | None = None
_last_cache_clear_time: float = 0


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def _derive_provider(base_url: str) -> str:
    """根据 base_url 推导提供商（兼容旧配置未持久化 llm_provider 时的回显）。"""
    u = (base_url or "").lower()
    if not u:
        return ""
    if "deepseek" in u:
        return "deepseek"
    if "openai" in u:
        return "openai"
    if "dashscope" in u or "aliyun" in u:
        return "qwen"
    if "11434" in u or "ollama" in u:
        return "ollama"
    return "custom"


def _settings_to_dict(s) -> dict:
    return {
        "comfyui_base_url": s.comfyui_base_url,
        "comfyui_timeout_seconds": s.comfyui_timeout_seconds,
        "comfyui_job_timeout_seconds": s.comfyui_job_timeout_seconds,
        "llm_provider": s.llm_provider or _derive_provider(s.llm_base_url),
        "llm_base_url": s.llm_base_url,
        "llm_api_key": _mask_key(s.llm_api_key),
        "llm_api_key_set": bool(s.llm_api_key),
        "llm_model": s.llm_model,
        "llm_timeout_seconds": s.llm_timeout_seconds,
        "render_concurrency": s.render_concurrency,
        "worker_poll_interval_seconds": s.worker_poll_interval_seconds,
        "job_max_attempts": s.job_max_attempts,
        "consistency_pass_threshold": s.consistency_pass_threshold,
        "consistency_review_threshold": s.consistency_review_threshold,
        "default_concurrency": s.render_concurrency,
    }


@router.get("")
def get_settings_api() -> dict:
    s = get_settings()
    return _settings_to_dict(s)


@router.put("")
def update_settings_api(payload: dict) -> dict:
    s = get_settings()
    # 真实写入 Settings 对象并持久化到 data/runtime_settings.json（重启后保留）
    updatable = [
        "comfyui_base_url", "comfyui_timeout_seconds", "comfyui_job_timeout_seconds",
        "llm_provider", "llm_base_url", "llm_model", "llm_timeout_seconds",
        "job_max_attempts", "consistency_pass_threshold", "consistency_review_threshold",
    ]
    for key in updatable:
        if key in payload:
            setattr(s, key, payload[key])
    if "llm_api_key" in payload:
        if payload["llm_api_key"] == "":
            s.llm_api_key = ""
        elif payload["llm_api_key"] and not str(payload["llm_api_key"]).startswith("*"):
            s.llm_api_key = str(payload["llm_api_key"])
    save_runtime_settings(s)
    return _settings_to_dict(s)


@router.post("/test-comfyui")
async def test_comfyui(payload: dict = None) -> dict:
    s = get_settings()
    # 优先使用请求中的 base_url；否则从实例池取第一个启用实例；最后回退单实例配置
    requested = (payload or {}).get("base_url")
    if requested:
        base_url = requested
    else:
        pool = comfyui_pool.enabled_instances()
        base_url = pool[0]["base_url"] if pool else s.comfyui_base_url
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/system_stats")
            resp.raise_for_status()
            return {"ok": True, "latency_ms": int(resp.elapsed.total_seconds() * 1000)}
    except httpx.ConnectError:
        raise errors.AppError("connection_refused", "ComfyUI 连接被拒绝", 502, reason="connection_refused")
    except httpx.TimeoutException:
        raise errors.AppError("timeout", "ComfyUI 连接超时", 504, reason="timeout")
    except Exception as e:
        raise errors.AppError("invalid_response", f"ComfyUI 响应异常: {e}", 502, reason="invalid_response")


@router.post("/test-llm")
async def test_llm(payload: dict = None) -> dict:
    s = get_settings()
    base_url = (payload or {}).get("base_url", s.llm_base_url)
    model = (payload or {}).get("model", s.llm_model)
    api_key = (payload or {}).get("api_key", s.llm_api_key)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5},
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {"ok": True, "latency_ms": int(resp.elapsed.total_seconds() * 1000), "echo": content}
    except httpx.ConnectError:
        raise errors.AppError("connection_refused", "语言模型连接被拒绝", 502, reason="connection_refused")
    except httpx.TimeoutException:
        raise errors.AppError("timeout", "语言模型连接超时", 504, reason="timeout")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise errors.AppError("unauthorized", "API Key 无效", 401, reason="unauthorized")
        raise errors.AppError("invalid_response", f"语言模型响应异常: {e}", 502, reason="invalid_response")
    except Exception as e:
        raise errors.AppError("invalid_response", f"语言模型响应异常: {e}", 502, reason="invalid_response")


@router.post("/llm-models")
async def list_llm_models(payload: dict = None) -> dict:
    """查询语言模型列表（OpenAI 兼容 /models 接口），供前端模型名称下拉选择。"""
    s = get_settings()
    base_url = (payload or {}).get("base_url", s.llm_base_url)
    api_key = (payload or {}).get("api_key", s.llm_api_key)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data", [])
            models = sorted(
                [str(m.get("id")) for m in items if m.get("id")],
            )
            return {"ok": True, "models": models}
    except httpx.ConnectError:
        raise errors.AppError("connection_refused", "语言模型连接被拒绝", 502, reason="connection_refused")
    except httpx.TimeoutException:
        raise errors.AppError("timeout", "语言模型连接超时", 504, reason="timeout")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise errors.AppError("unauthorized", "API Key 无效", 401, reason="unauthorized")
        raise errors.AppError("invalid_response", f"语言模型响应异常: {e}", 502, reason="invalid_response")
    except Exception as e:
        raise errors.AppError("invalid_response", f"语言模型响应异常: {e}", 502, reason="invalid_response")


# --- ComfyUI 实例池（多实例加权负载均衡，替代原三方通道） ---
@router.get("/comfyui-instances")
def list_comfyui_instances() -> list[dict]:
    return comfyui_pool.list_instances()


@router.get("/comfyui-instances/status")
def comfyui_instances_status() -> list[dict]:
    """实例监控面板（D-23）：健康摘要 + 实时系统状态（显存/队列深度/在线状态）。"""
    from app.integrations.comfyui import ComfyUIClient, ComfyUIError

    result = []
    for inst in comfyui_pool.list_instances():
        url = inst["base_url"]
        entry = {
            "base_url": url,
            "weight": inst["weight"],
            "enabled": inst["enabled"],
            "online": False,
            "system_stats": None,
            "queue_pending": None,
            "queue_running": None,
        }
        if inst["enabled"]:
            try:
                client = ComfyUIClient(base_url=url, timeout=5.0)
                stats = client.test_connection()
                entry["online"] = True
                entry["system_stats"] = {
                    "system": stats.get("system", {}),
                    "devices": [
                        {
                            "name": d.get("name"),
                            "type": d.get("type"),
                            "vram_total": d.get("vram_total"),
                            "vram_free": d.get("vram_free"),
                        }
                        for d in stats.get("devices", [])
                    ],
                }
                queue = client.get_queue()
                entry["queue_pending"] = len(queue.get("queue_pending", []))
                entry["queue_running"] = len(queue.get("queue_running", []))
            except (ComfyUIError, Exception):
                pass
        result.append(entry)
    # 合并健康熔断状态
    health = {h["base_url"]: h for h in comfyui_pool.get_health_summary()}
    for entry in result:
        h = health.get(entry["base_url"])
        if h:
            entry["fail_count"] = h["fail_count"]
            entry["circuit_open"] = h["circuit_open"]
    return result


@router.put("/comfyui-instances")
def update_comfyui_instances(instances: list[dict]) -> list[dict]:
    return comfyui_pool.set_instances(instances)


@router.post("/comfyui-instances/test")
async def test_comfyui_instances() -> dict:
    """逐实例测试连通性，返回每个实例的测试结果。"""
    results: list[dict] = []
    for inst in comfyui_pool.list_instances():
        item = {"base_url": inst["base_url"], "enabled": inst["enabled"], "weight": inst["weight"]}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{inst['base_url'].rstrip('/')}/system_stats")
                resp.raise_for_status()
                item.update({"ok": True, "latency_ms": int(resp.elapsed.total_seconds() * 1000)})
        except httpx.ConnectError:
            item.update({"ok": False, "reason": "connection_refused", "message": "连接被拒绝"})
        except httpx.TimeoutException:
            item.update({"ok": False, "reason": "timeout", "message": "连接超时"})
        except Exception as e:
            item.update({"ok": False, "reason": "invalid_response", "message": f"响应异常: {e}"})
        results.append(item)
    return {"ok": all(r.get("ok") for r in results) if results else False, "instances": results}


@router.post("/clear-upload-cache")
def clear_upload_cache() -> dict:
    global _last_cache_clear, _last_cache_clear_time
    now = time.time()
    if _last_cache_clear and (now - _last_cache_clear_time) < 60:
        result = dict(_last_cache_clear)
        result["cached"] = True
        return result

    s = get_settings()
    media_dir = Path(s.media_dir)
    freed = 0
    removed = 0
    skipped = 0
    if media_dir.exists():
        for f in media_dir.iterdir():
            if f.is_file() and f.suffix in (".tmp", ".upload"):
                try:
                    size = f.stat().st_size
                    f.unlink()
                    freed += size
                    removed += 1
                except OSError:
                    skipped += 1
    result = {"freed_bytes": freed, "removed_files": removed, "skipped_in_use": skipped}
    _last_cache_clear = dict(result)
    _last_cache_clear_time = now
    return result


import threading
_backup_lock = threading.Lock()


@router.get("/prompts")
def list_prompts_endpoint() -> list[dict]:
    """列出全部提示词（D-15）：名称、是否已外置、内容哈希。"""
    from app.services import prompt_store
    return prompt_store.list_prompts()


@router.get("/prompts/{name}")
def get_prompt_endpoint(name: str) -> dict:
    """读取提示词当前内容（外置文件优先，缺失回退内置默认值）。"""
    from app.services import prompt_store
    try:
        content = prompt_store.get_prompt(name)
    except FileNotFoundError:
        raise errors.AppError("not_found", f"提示词不存在: {name}", 404)
    return {"name": name, "content": content}


@router.put("/prompts/{name}")
def write_prompt_endpoint(name: str, payload: dict) -> dict:
    """写入提示词（保存即生效，无需重启）；写入前自动备份当前版本。"""
    from app.services import prompt_store
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise errors.AppError("invalid_request", "content 必须为非空字符串", 400)
    try:
        return prompt_store.write_prompt(name, content)
    except FileNotFoundError:
        raise errors.AppError("not_found", f"提示词不存在: {name}", 404)
    except ValueError as e:
        raise errors.AppError("invalid_request", str(e), 400)


@router.post("/prompts/{name}/reset")
def reset_prompt_endpoint(name: str) -> dict:
    """重置提示词为内置默认值（删除外置文件）。"""
    from app.services import prompt_store
    try:
        return prompt_store.reset_prompt(name)
    except FileNotFoundError:
        raise errors.AppError("not_found", f"提示词不存在: {name}", 404)


@router.post("/backup", status_code=202)
def backup_settings() -> dict:
    """手动触发一次备份（API.md §11）：备份数据库到 data/backups/。
    并发触发时若已有备份任务在跑，返回 409 invalid_state_transition。
    """
    if not _backup_lock.acquire(blocking=False):
        raise errors.AppError("invalid_state_transition", "已有备份任务在执行中", 409)
    try:
        import hashlib
        s = get_settings()
        backup_dir = Path(s.data_dir) / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        db_path = Path(s.database_url.split("///", 1)[1])
        db_backup = backup_dir / f"director_lab_{ts}.db"
        # sqlite3 backup API 在线一致性快照：自动合并 WAL 内容，不复制 -wal/-shm
        # （避免多连接场景下复制 WAL/SHM 导致的 inode 竞态与数据可见性问题）
        import sqlite3
        src = sqlite3.connect(str(db_path))
        try:
            dst = sqlite3.connect(str(db_backup))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        size = db_backup.stat().st_size if db_backup.exists() else 0
        checksum = ""
        if db_backup.exists():
            h = hashlib.sha256()
            with open(db_backup, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            checksum = f"sha256:{h.hexdigest()}"
        result = {
            "file_path": str(db_backup),
            "size_bytes": size,
            "checksum": checksum,
        }
        return {
            "id": f"job_backup_{ts}",
            "job_type": "backup",
            "status": "succeeded",
            "result": result,
        }
    finally:
        _backup_lock.release()


# --- Health (separate prefix) ---
health_router = APIRouter(prefix="/api", tags=["health"])


@health_router.get("/health")
def health_check() -> dict:
    s = get_settings()
    db_ok = True
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    comfyui_ok = True
    try:
        import httpx as _httpx
        comfyui_instances_status = []
        for inst in comfyui_pool.list_instances():
            inst_ok = False
            try:
                with _httpx.Client(timeout=5) as c:
                    r = c.get(f"{inst['base_url'].rstrip('/')}/system_stats")
                    r.raise_for_status()
                inst_ok = True
            except Exception:
                inst_ok = False
            comfyui_instances_status.append({
                "base_url": inst["base_url"],
                "enabled": inst["enabled"],
                "weight": inst["weight"],
                "status": "ok" if inst_ok else "unreachable",
            })
        comfyui_ok = any(i["status"] == "ok" for i in comfyui_instances_status)
    except Exception:
        comfyui_ok = False
        comfyui_instances_status = []

    disk_ok = True
    try:
        usage = shutil.disk_usage("/")
        disk_percent = (usage.free / usage.total) * 100
        disk_ok = disk_percent > 5
    except Exception:
        disk_ok = True

    # 数据库不可用视为 down；其余任一依赖异常为 degraded（API.md：healthy/degraded/down 三态）
    if not db_ok:
        status = "down"
    elif all([comfyui_ok, disk_ok]):
        status = "healthy"
    else:
        status = "degraded"
    return {
        "status": status,
        "database": "ok" if db_ok else "error",
        "comfyui": "ok" if comfyui_ok else "unreachable",
        "comfyui_instances": comfyui_instances_status,
        "worker": "ok",
        "disk": "ok" if disk_ok else "low",
    }
