"""超长剧本的分段大纲分析（阶段 3）。

背景：``analyze_script_outline`` 原本把整本剧本一次性送进 LLM。剧本一长就会
超出模型上下文窗口，而上下文超限属于输入问题，重试、放大 max_tokens 都救不了。

本模块按「集边界」把剧本切成若干个不超预算的窗口，分别做大纲分析后合并：
- 窗口之间不切断单集（依赖 script_chunker.split_episodes 的行首集标记）
- 窗口可并行，沿用 llm_parse_concurrency
- 合并后集号按窗口顺序重新连续编号，时长/对白字数汇总
- 无集标记且超预算时直接 llm_input_too_long，要求先分集：
  近似分段会让集数与制作排期不稳定，因此不做

与整本路径共用同一套返回结构，保证下游（评估提示词、estimate 端点）无感。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.services.prompt_store import get_prompt

__all__ = [
    "outline_user_prompt",
    "build_outline_payload",
    "pack_windows",
    "analyze_outline_segmented",
]


def outline_user_prompt(text: str, episode_duration_target: int,
                        *, window_note: str = "") -> str:
    """拼大纲分析的 user prompt；窗口信息只加在这里，系统提示词本身不改。"""
    parts = [f"目标单集时长：{episode_duration_target} 秒。"]
    if window_note:
        parts.append(window_note)
    parts.append(f"请分析以下剧本的分集结构：\n\n{text}")
    return "\n".join(parts)


def build_outline_payload(
    text: str,
    *,
    algorithm_version: str,
    max_episodes_per_batch: int,
    breakdown: list[dict],
    dialogue_chars: int,
) -> dict:
    """组装大纲返回结构：整本路径与分段路径共用，保证字段完全一致。"""
    total = len(breakdown)
    total_duration = sum(e["estimated_duration"] for e in breakdown)
    exceeds = total > max_episodes_per_batch
    suggestion = (
        f"剧本共 {total} 集，超过单次制作上限 {max_episodes_per_batch} 集，建议分批制作"
        if exceeds else None
    )
    return {
        "algorithm_version": algorithm_version,
        "char_count": len(text),
        "dialogue_char_estimate": dialogue_chars,
        "estimated_total_duration": round(total_duration, 1),
        "estimated_episodes": total,
        "episode_breakdown": breakdown,
        "exceeds_batch_limit": exceeds,
        "suggestion": suggestion,
    }


def pack_windows(chunks: list, *, settings: Any) -> list[list]:
    """按输入预算把相邻集打包成窗口；窗口之间不切断单集。"""
    from app.integrations.token_estimate import estimate_input_tokens, input_budget

    budget = input_budget(settings)
    windows: list[list] = []
    cur: list = []
    cur_tokens = 0
    for chunk in chunks:
        tokens = estimate_input_tokens(chunk.text, settings=settings)
        if cur and budget > 0 and cur_tokens + tokens > budget:
            windows.append(cur)
            cur = []
            cur_tokens = 0
        cur.append(chunk)
        cur_tokens += tokens
    if cur:
        windows.append(cur)
    return windows or [list(chunks)]


def analyze_outline_segmented(
    text: str,
    *,
    episode_duration_target: int,
    max_episodes_per_batch: int,
    algorithm_version: str,
    avg_shot_duration: float,
    client: Any,
    settings: Any,
) -> dict:
    """分段分析超长剧本并合并为统一大纲结构。"""
    from app.integrations.llm import LLMError
    from app.integrations.llm_response import ERR_INPUT_TOO_LONG
    from app.integrations.llm_retry import call_with_retry
    from app.services.script_chunker import split_episodes

    split = split_episodes(text)
    if split is None or not split.chunks:
        raise LLMError(
            ERR_INPUT_TOO_LONG,
            "剧本超过模型输入预算且没有分集标记：请先在剧本里用『第X集』分集，"
            "或调大 llm_context_window_tokens",
        )

    windows = pack_windows(split.chunks, settings=settings)
    system = get_prompt("script_outline_system")
    concurrency = max(1, int(getattr(settings, "llm_parse_concurrency", 3) or 3))

    def _run(window: list) -> dict:
        body = "".join(c.text for c in window)
        note = (
            f"本段从第 {window[0].number} 集开始，共 {len(window)} 集。"
            "请只输出本段的 episodes，集号按本段顺序从 1 开始编号。"
        )
        return call_with_retry(
            client,
            system_prompt=system,
            user_prompt=outline_user_prompt(
                body, episode_duration_target, window_note=note),
            json_mode=True,
            stream=False,
            use_chat_json=True,
        )

    with ThreadPoolExecutor(max_workers=min(concurrency, len(windows))) as pool:
        results = list(pool.map(_run, windows))

    breakdown: list[dict] = []
    dialogue_total = 0
    for window, res in zip(windows, results):
        try:
            dialogue_total += int(res.get("dialogue_char_estimate") or 0)
        except (TypeError, ValueError):
            pass
        episodes = res.get("episodes") or []
        for idx, chunk in enumerate(window):
            item = episodes[idx] if idx < len(episodes) and isinstance(episodes[idx], dict) else {}
            try:
                duration = round(
                    float(item.get("estimated_duration") or episode_duration_target), 1)
            except (TypeError, ValueError):
                duration = float(episode_duration_target)
            try:
                shots = max(1, int(item.get("estimated_shots") or 1))
            except (TypeError, ValueError):
                shots = max(1, round(episode_duration_target / avg_shot_duration))
            breakdown.append({
                "episode": len(breakdown) + 1,
                "title": str(item.get("title") or chunk.title or ""),
                "estimated_duration": duration,
                "estimated_shots": shots,
            })

    return build_outline_payload(
        text,
        algorithm_version=algorithm_version,
        max_episodes_per_batch=max_episodes_per_batch,
        breakdown=breakdown,
        dialogue_chars=dialogue_total,
    )
