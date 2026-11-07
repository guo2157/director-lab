"""Web Search Tool：联网搜索工具与 Provider Adapter（联网搜索整改 Phase 1）。

设计约束（联网搜索整改方案 §3/§9）：
- Provider Adapter 模式：Bing/360 搜索结果页抓取（今日实测可用，零 API Key，不引入收费 API）
- 统一结果结构：status/query/provider/result_count/results[{title,url,snippet,domain}]/error
- 状态：success / partial / empty / failed / timeout
- 搜索请求成功 ≠ 搜索结果有效：结果条目需经 Validator 校验（Phase 2 接入）
- 失败不抛异常，返回结构化错误（调用方决定重试/降级）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

import httpx

SEARCH_TIMEOUT = 15.0
MAX_RESULTS = 8

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class SearchError(Exception):
    """搜索失败（携带错误码与可读信息）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class SearchItem:
    title: str
    url: str
    snippet: str
    domain: str

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "domain": self.domain,
            "published_at": None,
        }


@dataclass
class SearchOutcome:
    """单次 Provider 搜索结果。"""

    provider: str
    status: str  # success / empty / failed / timeout
    items: list[SearchItem] = field(default_factory=list)
    error: str | None = None


class SearchProvider:
    """Provider 统一接口：不同搜索引擎实现独立适配器。"""

    name: str = "base"

    def search(self, query: str, *, max_results: int = MAX_RESULTS) -> SearchOutcome:
        raise NotImplementedError


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc or ""
    except ValueError:
        return ""


def _decode_bing_redirect(url: str) -> str:
    """解码 Bing 跳转链接（bing.com/ck/a?...&u=a1<base64>）为真实目标 URL。

    u 参数格式：'a1' + base64(目标 URL)；解码失败原样返回。
    """
    import base64
    from urllib.parse import parse_qs, unquote

    if "bing.com/ck/" not in url:
        return url
    try:
        qs = parse_qs(urlparse(url).query)
        u = (qs.get("u") or [""])[0]
        if u.startswith("a1"):
            # Bing 的 base64 变体：'-' 还原为 '+'，'_' 还原为 '/'
            b64 = u[2:].replace("-", "+").replace("_", "/")
            pad = "=" * (-len(b64) % 4)
            decoded = base64.b64decode(b64 + pad).decode("utf-8", errors="replace")
            if decoded.startswith("http"):
                return unquote(decoded)
    except Exception:
        pass
    return url


class BingProvider(SearchProvider):
    """Bing 搜索结果页抓取（无 API Key，页面解析）。"""

    name = "bing"

    def search(self, query: str, *, max_results: int = MAX_RESULTS) -> SearchOutcome:
        url = f"https://www.bing.com/search?q={query}"
        try:
            with httpx.Client(
                timeout=SEARCH_TIMEOUT, headers=_DEFAULT_HEADERS, follow_redirects=True
            ) as client:
                resp = client.get(url)
            if resp.status_code != 200:
                return SearchOutcome(self.name, "failed", error=f"http_{resp.status_code}")
            items = self._parse(resp.text, max_results)
            return SearchOutcome(self.name, "success" if items else "empty", items=items)
        except httpx.TimeoutException:
            return SearchOutcome(self.name, "timeout", error="timeout")
        except httpx.HTTPError as e:
            return SearchOutcome(self.name, "failed", error=f"http_error: {type(e).__name__}")

    def _parse(self, html: str, max_results: int) -> list[SearchItem]:
        """解析结果条目：<h2><a href=...>标题</a></h2> + 相邻摘要文本。"""
        items: list[SearchItem] = []
        seen: set[str] = set()
        for m in re.finditer(
            r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>\s*</h2>',
            html,
            re.DOTALL,
        ):
            url, title_html = m.group(1), m.group(2)
            # HTML 实体反转义（&amp; → &）后再解码 Bing 跳转链接
            import html as _html
            url = _decode_bing_redirect(_html.unescape(url))
            if url in seen:
                continue
            seen.add(url)
            title = re.sub(r"<[^>]+>", "", title_html)
            title = re.sub(r"\s+", " ", title).strip()
            if not title:
                continue
            # 摘要：取该条目之后的文本块（b_algo 内 <p>）
            tail = html[m.end(): m.end() + 3000]
            sm = re.search(r"<p[^>]*>(.*?)</p>", tail, re.DOTALL)
            snippet = ""
            if sm:
                snippet = re.sub(r"<[^>]+>", " ", sm.group(1))
                snippet = re.sub(r"\s+", " ", snippet).strip()[:300]
            items.append(SearchItem(title, url, snippet, _domain_of(url)))
            if len(items) >= max_results:
                break
        return items


class SoProvider(SearchProvider):
    """360 搜索结果页抓取（备用 Provider）。"""

    name = "so"

    def search(self, query: str, *, max_results: int = MAX_RESULTS) -> SearchOutcome:
        url = f"https://www.so.com/s?q={query}"
        try:
            with httpx.Client(
                timeout=SEARCH_TIMEOUT, headers=_DEFAULT_HEADERS, follow_redirects=True
            ) as client:
                resp = client.get(url)
            if resp.status_code != 200:
                return SearchOutcome(self.name, "failed", error=f"http_{resp.status_code}")
            items = self._parse(resp.text, max_results)
            return SearchOutcome(self.name, "success" if items else "empty", items=items)
        except httpx.TimeoutException:
            return SearchOutcome(self.name, "timeout", error="timeout")
        except httpx.HTTPError as e:
            return SearchOutcome(self.name, "failed", error=f"http_error: {type(e).__name__}")

    def _parse(self, html: str, max_results: int) -> list[SearchItem]:
        """解析 360 结果条目：<h3 class="res-title"><a href=...>。"""
        items: list[SearchItem] = []
        seen: set[str] = set()
        for m in re.finditer(
            r'<h3[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        ):
            url, title_html = m.group(1), m.group(2)
            if url in seen:
                continue
            seen.add(url)
            title = re.sub(r"<[^>]+>", "", title_html)
            title = re.sub(r"\s+", " ", title).strip()
            if not title:
                continue
            tail = html[m.end(): m.end() + 3000]
            sm = re.search(r"<p[^>]*class=.res-desc[^>]*>(.*?)</p>", tail, re.DOTALL)
            if sm is None:
                sm = re.search(r"<p[^>]*>(.*?)</p>", tail, re.DOTALL)
            snippet = ""
            if sm:
                snippet = re.sub(r"<[^>]+>", " ", sm.group(1))
                snippet = re.sub(r"\s+", " ", snippet).strip()[:300]
            items.append(SearchItem(title, url, snippet, _domain_of(url)))
            if len(items) >= max_results:
                break
        return items


# Provider 注册表：主 Provider 在前，备用在后（故障切换顺序）
_PROVIDERS: list[SearchProvider] = [BingProvider(), SoProvider()]


def list_providers() -> list[str]:
    """已注册 Provider 清单（含顺序，供健康状态展示）。"""
    return [p.name for p in _PROVIDERS]


def web_search(
    query: str,
    *,
    max_results: int = MAX_RESULTS,
    providers: list[SearchProvider] | None = None,
) -> dict:
    """执行联网搜索：主 Provider 失败/空结果时切换备用 Provider。

    返回统一结构：
    {status, query, provider, result_count, results[{title,url,snippet,domain,published_at}], error}
    - success：拿到有效结果
    - empty：所有 Provider 均无结果
    - failed：所有 Provider 均失败（含超时）
    """
    query = (query or "").strip()
    if not query:
        return {
            "status": "failed", "query": query, "provider": None,
            "result_count": 0, "results": [], "error": "empty_query",
        }
    last_error: str | None = None
    for provider in providers or _PROVIDERS:
        outcome = provider.search(query, max_results=max_results)
        if outcome.status == "success" and outcome.items:
            return {
                "status": "success",
                "query": query,
                "provider": outcome.provider,
                "result_count": len(outcome.items),
                "results": [i.to_dict() for i in outcome.items],
                "error": None,
            }
        if outcome.status == "empty":
            # 该 Provider 可用但无结果：记录后继续尝试备用（可能关键词适配差异）
            last_error = last_error or f"{outcome.provider}: empty"
            continue
        # failed/timeout：切换备用 Provider
        last_error = f"{outcome.provider}: {outcome.error}"
    return {
        "status": "failed",
        "query": query,
        "provider": None,
        "result_count": 0,
        "results": [],
        "error": last_error or "all_providers_failed",
    }
