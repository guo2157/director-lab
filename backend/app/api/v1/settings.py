"""设置管理接口：API.md §11 字段级契约。
- GET/PUT /api/settings：全局配置读写，密钥掩码
- POST test-comfyui/test-llm：连通性测试
- GET/PUT /api/settings/channels：三方通道配置
- POST channels/{id}/test：通道测试
- POST clear-upload-cache：清理上传缓存
- GET /api/health：健康状态
"""
import time
import os
import shutil
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.core.database import get_db, engine
from app.services import errors
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


def _settings_to_dict(s) -> dict:
    return {
        "comfyui_base_url": s.comfyui_base_url,
        "comfyui_timeout_seconds": s.comfyui_timeout_seconds,
        "comfyui_poll_interval_seconds": s.comfyui_poll_interval_seconds,
        "comfyui_job_timeout_seconds": s.comfyui_job_timeout_seconds,
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
        "video_poll_timeout_seconds": int(s.comfyui_job_timeout_seconds),
        "default_concurrency": s.render_concurrency,
    }


@router.get("")
def get_settings_api() -> dict:
    s = get_settings()
    return _settings_to_dict(s)


@router.put("")
def update_settings_api(payload: dict) -> dict:
    s = get_settings()
    # 仅更新允许的字段（运行时不可持久化到 .env，返回更新后的视图）
    result = _settings_to_dict(s)
    updatable = [
        "comfyui_base_url", "comfyui_timeout_seconds", "comfyui_job_timeout_seconds",
        "llm_base_url", "llm_model", "llm_timeout_seconds",
        "job_max_attempts", "consistency_pass_threshold", "consistency_review_threshold",
    ]
    for key in updatable:
        if key in payload:
            result[key] = payload[key]
    if "llm_api_key" in payload:
        if payload["llm_api_key"] == "":
            result["llm_api_key"] = ""
            result["llm_api_key_set"] = False
        elif payload["llm_api_key"] and not payload["llm_api_key"].startswith("*"):
            result["llm_api_key"] = _mask_key(payload["llm_api_key"])
            result["llm_api_key_set"] = True
    return result


@router.post("/test-comfyui")
async def test_comfyui(payload: dict = None) -> dict:
    s = get_settings()
    base_url = (payload or {}).get("base_url", s.comfyui_base_url)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/system_stats")
            resp.raise_for_status()
            return {"ok": True, "latency_ms": int(resp.elapsed.total_seconds() * 1000)}
    except httpx.ConnectRefusedError:
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
    except httpx.ConnectRefusedError:
        raise errors.AppError("connection_refused", "语言模型连接被拒绝", 502, reason="connection_refused")
    except httpx.TimeoutException:
        raise errors.AppError("timeout", "语言模型连接超时", 504, reason="timeout")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise errors.AppError("unauthorized", "API Key 无效", 401, reason="unauthorized")
        raise errors.AppError("invalid_response", f"语言模型响应异常: {e}", 502, reason="invalid_response")
    except Exception as e:
        raise errors.AppError("invalid_response", f"语言模型响应异常: {e}", 502, reason="invalid_response")


# --- Channels (stored in memory, no DB table in current schema) ---
_channels: list[dict] = []


@router.get("/channels")
def list_channels() -> list[dict]:
    return _channels


@router.put("/channels")
def update_channels_api(channels: list[dict]) -> list[dict]:
    # Validate priority uniqueness among enabled
    enabled = [c for c in channels if c.get("enabled")]
    priorities = [c.get("priority") for c in enabled]
    if len(priorities) != len(set(priorities)):
        raise errors.AppError("invalid_request", "启用通道的 priority 必须唯一", 400)
    _channels.clear()
    for c in channels:
        _channels.append({
            "id": c.get("id", c.get("channel_id", "")),
            "name": c.get("name", ""),
            "type": c.get("type", "comfyui"),
            "base_url": c.get("base_url", ""),
            "api_key": _mask_key(c.get("api_key", "")),
            "api_key_set": bool(c.get("api_key")),
            "enabled": c.get("enabled", False),
            "priority": c.get("priority", 1),
        })
    return _channels


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: str) -> dict:
    ch = next((c for c in _channels if c["id"] == channel_id), None)
    if not ch:
        raise errors.AppError("not_found", "通道不存在", 404)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{ch['base_url'].rstrip('/')}/system_stats")
            resp.raise_for_status()
            return {"ok": True, "latency_ms": int(resp.elapsed.total_seconds() * 1000)}
    except httpx.ConnectRefusedError:
        raise errors.AppError("connection_refused", "通道连接被拒绝", 502, reason="connection_refused")
    except httpx.TimeoutException:
        raise errors.AppError("timeout", "通道连接超时", 504, reason="timeout")
    except Exception as e:
        raise errors.AppError("invalid_response", f"通道响应异常: {e}", 502, reason="invalid_response")


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
        if db_path.exists():
            shutil.copy2(db_path, db_backup)
        # WAL 模式下需一并备份 -wal/-shm 以保证一致性
        for suffix in ("-wal", "-shm"):
            side = Path(str(db_path) + suffix)
            if side.exists():
                shutil.copy2(side, backup_dir / f"director_lab_{ts}.db{suffix}")
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
        with _httpx.Client(timeout=5) as c:
            r = c.get(f"{s.comfyui_base_url.rstrip('/')}/system_stats")
            r.raise_for_status()
    except Exception:
        comfyui_ok = False

    disk_ok = True
    try:
        usage = shutil.disk_usage("/")
        disk_percent = (usage.free / usage.total) * 100
        disk_ok = disk_percent > 5
    except Exception:
        disk_ok = True

    status = "healthy" if all([db_ok, comfyui_ok, disk_ok]) else "degraded"
    return {
        "status": status,
        "database": "ok" if db_ok else "error",
        "comfyui": "ok" if comfyui_ok else "unreachable",
        "worker": "ok",
        "disk": "ok" if disk_ok else "low",
    }
