"""Web Fetch Tool：网页正文读取（联网搜索整改 Phase 1）。

设计约束（联网搜索整改方案 §10）：
- 搜索与网页读取分离：web_search 负责发现 URL，本工具负责获取正文
- 只允许 HTTP/HTTPS；阻止 SSRF（内网/localhost/云元数据/链路本地地址）
- 限制响应体大小与超时；重定向后再次做 SSRF 校验
- 网页内容作为不可信数据：只做文本提取，不执行脚本
- 统一结果结构（status/url/title/content/content_type/retrieved_at/error）
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import httpx

# 单次请求超时（秒）与响应体大小上限（字节）
FETCH_TIMEOUT = 15.0
MAX_CONTENT_BYTES = 2 * 1024 * 1024  # 2MB
# 提取正文的最大字符数（超出截断）
MAX_CONTENT_CHARS = 20000

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class FetchError(Exception):
    """网页读取失败（携带错误码与可读信息）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class FetchResult:
    status: str  # success / failed
    url: str
    title: str | None = None
    content: str | None = None
    content_type: str | None = None
    retrieved_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "url": self.url,
            "title": self.title,
            "content": self.content,
            "content_type": self.content_type,
            "retrieved_at": self.retrieved_at,
            "error": self.error,
        }


def _assert_safe_url(url: str) -> None:
    """SSRF 防护：只允许 HTTP/HTTPS 公网地址。

    拒绝：非 http(s) 协议、无主机名、内网/保留/链路本地 IP、localhost。
    """
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise FetchError("invalid_url", f"URL 非法：{e}") from e
    if parsed.scheme not in ("http", "https"):
        raise FetchError("scheme_not_allowed", f"只允许 http/https，当前 {parsed.scheme or '空'}")
    host = parsed.hostname
    if not host:
        raise FetchError("invalid_url", "URL 缺少主机名")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        raise FetchError("host_not_allowed", f"不允许访问本地主机 {host}")
    # 域名解析后逐 IP 校验（域名可能解析到内网地址）
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise FetchError("dns_failed", f"域名解析失败：{host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise FetchError("host_not_allowed", f"不允许访问内网/保留地址 {ip}")


def _extract_text(html: str) -> tuple[str | None, str]:
    """粗提取标题与正文：去 script/style/标签，压缩空白。"""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else None
    body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return title, body


def web_fetch(url: str, *, timeout: float = FETCH_TIMEOUT) -> FetchResult:
    """读取网页正文（同步短事务；失败返回结构化错误，不抛异常给调用方）。"""
    now = datetime.now().isoformat(timespec="seconds")
    try:
        _assert_safe_url(url)
    except FetchError as e:
        return FetchResult("failed", url, error=f"{e.code}: {e.message}", retrieved_at=now)
    try:
        with httpx.Client(
            timeout=timeout,
            headers=_DEFAULT_HEADERS,
            follow_redirects=True,
        ) as client:
            # 流式读取并限制响应体大小（超过上限中断，防超大响应）
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return FetchResult(
                        "failed", url, error=f"http_{resp.status_code}", retrieved_at=now
                    )
                ctype = resp.headers.get("content-type", "")
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > MAX_CONTENT_BYTES:
                        break
                    chunks.append(chunk)
                text = b"".join(chunks).decode("utf-8", errors="replace")
                # 重定向后对最终 URL 再次校验（防重定向绕过 SSRF）
                _assert_safe_url(str(resp.url))
        if "html" in ctype or "xml" in ctype or "text/plain" in ctype or not ctype:
            title, body = _extract_text(text)
        else:
            # 非文本内容（PDF/图片等）：不解析正文，仅记录类型
            return FetchResult(
                "failed", url, content_type=ctype,
                error="unsupported_content_type", retrieved_at=now,
            )
        if len(body) < 50:
            return FetchResult("failed", url, title=title, error="empty_content", retrieved_at=now)
        return FetchResult(
            "success", url, title=title, content=body[:MAX_CONTENT_CHARS],
            content_type=ctype.split(";")[0].strip() or None, retrieved_at=now,
        )
    except httpx.TimeoutException:
        return FetchResult("failed", url, error="timeout", retrieved_at=now)
    except httpx.HTTPError as e:
        return FetchResult("failed", url, error=f"http_error: {type(e).__name__}", retrieved_at=now)
    except FetchError as e:
        return FetchResult("failed", url, error=f"{e.code}: {e.message}", retrieved_at=now)
