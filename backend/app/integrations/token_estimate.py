"""输入 token 保守估算（不引入 tokenizer 依赖）。

背景：项目环境无网络、不装新依赖，无法使用官方 tokenizer。这里用「字符类别 ×
系���」的保守估算：CJK 字符按 1.2 token/字、其他字符按 0.3 token/字符（可配置）。
该估算只用于**发起请求前的预检拦截**，不参与计费或精确预算；宁可略微高估
（提前拦截），也不要低估（放行后再被供应商拒绝、白白承担一次退避重试）。

真实系数可用 ``calibrate_hint`` 结合实际 ``prompt_tokens`` 反推：阶段 1 的
``agent.chunking`` 事件记录了每批输入字符数，``agent.parse_attempts`` 记录了
每次尝试的 ``prompt_tokens``，两者按批次范围对齐即可得到「每字符多少 token」。
"""
from __future__ import annotations

import re
from statistics import median
from typing import Any

__all__ = [
    "estimate_tokens",
    "estimate_input_tokens",
    "input_budget",
    "precheck_input_budget",
    "calibrate_hint",
]

# CJK 统一表意文字 + 中日韩标点 + 全角符号
_CJK_RE = re.compile(
    r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)


def estimate_tokens(
    text: str,
    *,
    cjk_factor: float = 1.2,
    other_factor: float = 0.3,
) -> int:
    """保守估算 ``text`` 的 token 数（向上取整）。

    这是**估算**而不是精确计数：CJK 字符按 ``cjk_factor``（默认 1.2）计，
    其余字符（英文、数字、标点、空白）按 ``other_factor``（默认 0.3）计。
    系数来自经验值，可用 ``calibrate_hint`` 结合真实用量校准后收紧。

    空输入返回 0；非法输入按空串处理，不抛异常（预检路径不应因脏数据中断）。
    """
    source = text if isinstance(text, str) else ""
    if not source:
        return 0
    cjk_count = len(_CJK_RE.findall(source))
    other_count = max(0, len(source) - cjk_count)
    total = cjk_count * float(cjk_factor) + other_count * float(other_factor)
    return int(total) + (1 if total > int(total) else 0)


def estimate_input_tokens(text: str, *, settings: Any = None) -> int:
    """按当前配置的系数估算输入 token（配置缺失时回落函数默认值）。"""
    cjk = getattr(settings, "llm_cjk_token_factor", None) if settings is not None else None
    other = getattr(settings, "llm_other_token_factor", None) if settings is not None else None
    return estimate_tokens(
        text,
        cjk_factor=1.2 if cjk is None else float(cjk),
        other_factor=0.3 if other is None else float(other),
    )


def input_budget(settings: Any) -> int:
    """输入 token 预算 = 上下文窗口 × 输入占比（窗口或占比为 0 表示不预检）。

    注意不能用 ``getattr(settings, "llm_input_budget_ratio", 0.6) or 0.6``：
    ``or`` 会把显式配置的 0 当成缺失值回落到 0.6，使「置 0 关闭预检」失效。
    这里只对 ``None``/缺失回落默认值。
    """
    window = int(getattr(settings, "llm_context_window_tokens", 0) or 0)
    if window <= 0:
        return 0
    raw_ratio = getattr(settings, "llm_input_budget_ratio", None)
    ratio = 0.6 if raw_ratio is None else float(raw_ratio)
    if ratio <= 0:
        return 0
    return int(window * ratio)


def precheck_input_budget(
    text: str,
    *,
    settings: Any = None,
    allow_segmented: bool = False,
) -> dict[str, Any]:
    """发起解析前的输入长度预检。

    两级判定（按严重程度升序）：
    1. **单集正文**超预算 → 必然失败。按集切片后每次请求发的是单集，
       单集都放不下就没有任何切分手段能救，必须让用户拆集或调大窗口。
    2. **整本**超预算 → 大纲需要整本，此时：``allow_segmented=True`` 返回
       ``outline_mode="segmented"``（供分段大纲使用）；否则视为失败
       （阶段 3 未实现分段时的行为）。

    返回 ``{"ok": bool, "code": str|None, "message": str,
    "outline_mode": "full"|"segmented", "estimated_tokens": int,
    "budget": int, "chars": int, "episode": int|None}``。
    ``budget == 0``（未配置窗口）时直接放行，不做任何拦截。
    """
    from app.services.script_chunker import split_episodes

    budget = input_budget(settings)
    result: dict[str, Any] = {
        "ok": True,
        "code": None,
        "message": "",
        "outline_mode": "full",
        "estimated_tokens": 0,
        "budget": budget,
        "chars": len(text or ""),
        "episode": None,
    }
    if budget <= 0:
        return result

    source = text or ""
    whole_tokens = estimate_input_tokens(source, settings=settings)
    result["estimated_tokens"] = whole_tokens

    split = split_episodes(source)
    if split is not None and split.chunks:
        biggest = max(split.chunks, key=lambda c: len(c.text))
        ep_tokens = estimate_input_tokens(biggest.text, settings=settings)
        ep_no = biggest.number
    else:
        # 无集标记：单集等于整本（调用方会用近似分段，但每段仍接近整本）
        ep_tokens, ep_no = whole_tokens, None

    if ep_tokens > budget:
        where = f"第 {ep_no} 集" if ep_no else "剧本（无集标记）"
        result.update({
            "ok": False,
            "code": "llm_input_too_long",
            "episode": ep_no,
            "message": (
                f"{where}正文约 {est_chars(source, settings)} 字 / 估算 "
                f"{ep_tokens} token，超过当前模型输入预算 {budget}，"
                "请拆分该集或调大 llm_context_window_tokens"
            ),
        })
        return result

    if whole_tokens > budget:
        result["outline_mode"] = "segmented"
        if not allow_segmented:
            result.update({
                "ok": False,
                "code": "llm_input_too_long",
                "message": (
                    f"剧本约 {len(source)} 字 / 估算 {whole_tokens} token，"
                    f"超过当前模型输入预算 {budget}，请拆分或调大 "
                    "llm_context_window_tokens"
                ),
            })
    return result


def est_chars(text: str, settings: Any = None) -> int:
    """辅助：返回字符数（仅用于错误提示文案，便于用户对照剧本长度）。"""
    return len(text or "")


def calibrate_hint(db: Any = None, *, limit_jobs: int = 20) -> dict[str, Any]:
    """用历史事件的真实用量反推建议系数（只返回数值，不改配置）。

    数据来源：同一 job 内
      - ``agent.chunking`` → ``batch_chars``：[{range: "1-1", chars: N}]
      - ``agent.parse_attempts`` → ``attempts``：[{attempt, prompt_tokens, ok}]
    按 ``range`` 对齐，得到「prompt_tokens / 输入字符数」的样本中位数。

    返回 ``{"suggested_factor": float|None, "samples": int}``。样本不足时
    ``suggested_factor`` 为 None，调用方应保持当前系数。
    """
    if db is None:
        return {"suggested_factor": None, "samples": 0}
    from app.models import Job, JobEvent

    ratios: list[float] = []
    jobs = (
        db.query(Job)
        .filter(Job.job_type == "script_agent")
        .order_by(Job.created_at.desc())
        .limit(max(1, int(limit_jobs)))
        .all()
    )
    for job in jobs:
        events = (
            db.query(JobEvent)
            .filter(
                JobEvent.job_id == job.id,
                JobEvent.event_type.in_(["agent.chunking", "agent.parse_attempts"]),
            )
            .all()
        )
        chars_by_range: dict[str, int] = {}
        tokens_by_range: dict[str, int] = {}
        for ev in events:
            payload = ev.payload or {}
            if ev.event_type == "agent.chunking":
                for item in payload.get("batch_chars") or []:
                    rng = str(item.get("range") or "")
                    chars = int(item.get("chars") or 0)
                    if rng and chars > 0:
                        chars_by_range[rng] = chars
            else:
                rng = str(payload.get("range") or "")
                for att in payload.get("attempts") or []:
                    if not att.get("ok"):
                        continue
                    tokens = att.get("prompt_tokens")
                    if rng and isinstance(tokens, (int, float)) and tokens > 0:
                        tokens_by_range[rng] = int(tokens)
                        break
        for rng, chars in chars_by_range.items():
            tokens = tokens_by_range.get(rng)
            if tokens and chars > 0:
                ratios.append(tokens / chars)
    if not ratios:
        return {"suggested_factor": None, "samples": 0}
    return {"suggested_factor": round(median(ratios), 4), "samples": len(ratios)}
