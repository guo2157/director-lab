"""A00b 守卫：测试套件禁止发起真实 LLM / ComfyUI 网络请求。

服务器 .env 里有真实配置，误连会真的烧 token、真的渲染。
conftest 的 _block_real_external_network 把 httpx 真实传输层换成直接抛错，
这里验证闸门确实生效，且进程内 ASGI 调用不受影响。
"""
from __future__ import annotations

import httpx
import pytest


def test_llm_client_real_call_is_blocked():
    from app.integrations.llm import LLMClient

    client = LLMClient(base_url="https://llm.example.invalid/v1", api_key="x", model="m", timeout=1.0)
    with pytest.raises(Exception) as ei:
        client.chat("system", "user")
    assert "禁止发起真实外部网络请求" in str(ei.value)


def test_comfyui_client_real_call_is_blocked():
    from app.integrations.comfyui import ComfyUIClient

    client = ComfyUIClient(base_url="https://comfy.example.invalid", timeout=1.0)
    with pytest.raises(Exception) as ei:
        client.get_queue()
    assert "禁止发起真实外部网络请求" in str(ei.value)


def test_plain_httpx_call_is_blocked():
    with pytest.raises(Exception) as ei:
        httpx.get("https://example.invalid/ping", timeout=1.0)
    assert "禁止发起真实外部网络请求" in str(ei.value)


def test_loopback_probe_is_allowed():
    """回环探活（既有 startup 自检用例依赖）不拦。"""
    from app.integrations.comfyui import ComfyUIClient

    client = ComfyUIClient(base_url="http://127.0.0.1:9", timeout=0.2)
    with pytest.raises(Exception) as ei:
        client.get_queue()
    assert "禁止发起真实外部网络请求" not in str(ei.value)


@pytest.mark.asyncio
async def test_async_external_call_is_blocked():
    async with httpx.AsyncClient() as c:
        with pytest.raises(Exception) as ei:
            await c.get("https://example.invalid/ping")
    assert "禁止发起真实外部网络请求" in str(ei.value)


@pytest.mark.asyncio
async def test_async_asgi_transport_still_allowed():
    from fastapi.testclient import TestClient  # noqa: F401  - 确认无副作用

    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/api/health")
    assert resp.status_code in (200, 404)


def test_in_process_asgi_still_works():
    """闸门只拦真实传输层，TestClient / ASGITransport 的进程内调用必须照常。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code in (200, 404)
