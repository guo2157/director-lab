"""Search Result Validator：搜索结果验证器（联网搜索整改 Phase 2）。
设计约束（联网搜索整改方案 §6）：
- 搜索完成后不能直接将结果交给 LLM，必须执行确定性验证
- HTTP 200 不代表搜索成功：请求成功 ≠ 结果有效
- 验证规则确定性执行，不调用 LLM
- 验证失败的结果条目被剔除（valid_results），不伪装成有效结果
- 区分：Search Success（API 返回结果）/ Fetch Partial（部分网页可读）/ Fetch Failed（网页均不可读）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class ValidatedResult:
    """验证后的搜索结果集合。"""

    valid_results: list[dict] = field(default_factory=list)
    invalid_results: list[dict] = field(default_factory=list)
    # 剔除原因统计：{reason_code: count}
    removed_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def valid_count(self) -> int:
        return len(self.valid_results)


def _is_valid_url(url: str) -> bool:
    """URL 有效性：http/https 协议 + 有主机名。"""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def validate_search_results(results: list[dict]) -> ValidatedResult:
    """验证搜索结果条目（确定性规则，不调用 LLM）。

    校验项：
    1. URL 有效（http/https + 主机名）
    2. 标题存在且非空
    3. URL 去重（同 URL 只保留首条）
    4. 摘要可为空（空摘要不剔除，仅记录）
    """
    validated = ValidatedResult()
    seen_urls: set[str] = set()
    for item in results or []:
        url = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip()
        if not _is_valid_url(url):
            validated.invalid_results.append(item)
            validated.removed_reasons["invalid_url"] = validated.removed_reasons.get("invalid_url", 0) + 1
            continue
        if not title:
            validated.invalid_results.append(item)
            validated.removed_reasons["missing_title"] = validated.removed_reasons.get("missing_title", 0) + 1
            continue
        if url in seen_urls:
            validated.invalid_results.append(item)
            validated.removed_reasons["duplicate_url"] = validated.removed_reasons.get("duplicate_url", 0) + 1
            continue
        seen_urls.add(url)
        validated.valid_results.append(item)
    return validated


def classify_fetch_status(fetch_results: list[dict]) -> str:
    """根据网页读取结果分类：success / partial / failed。

    - fetch_results：[{url, status(success/failed/pending)}]
    - 全部成功 → success；部分成功 → partial；全部失败/未读取 → failed
    """
    if not fetch_results:
        return "failed"
    statuses = [r.get("status") for r in fetch_results]
    ok = sum(1 for s in statuses if s == "success")
    if ok == len(statuses):
        return "success"
    if ok > 0:
        return "partial"
    return "failed"
