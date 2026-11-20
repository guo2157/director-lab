"""剧本分集切片（纯函数，无 DB、无 LLM 依赖）。

背景：``script_agent._parse_batch`` 之前依赖 ``ScriptVersion.get_episode_chunk``，
该方法在整个仓库里并不存在，``hasattr`` 恒为 False，于是**每一集都发送整本剧本**。
本模块提供按集切分与范围拼接，让每批只发送本批正文。

约定：
- 集标记只认行首（与 ``SECTION_LINE_RE`` 同源判定），因此台词里
  出现的「第1集」不会被误判为分集标记。
- ``SECTION_LINE_RE`` 自 ``script_writing._SECTION_RE`` 抽出为公共常量，
  两边共用同一份判定，避免「台词过滤」与「分集切分」两套口径漂移。
- 第一个标记之前的内容（人物表 / 序言）作为 preamble 单独返回，不丢弃。
- 无集标记时 ``split_episodes`` 返回 None，由调用方决定是否 ``fallback_split``。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "EpisodeChunk",
    "EpisodeSplit",
    "SECTION_LINE_RE",
    "is_section_line",
    "split_episodes",
    "slice_range",
    "fallback_split",
    "cn_to_int",
]


@dataclass
class EpisodeChunk:
    """单集正文片段。``text`` 为原文切片，不改写任何字符。"""

    number: int
    title: str = ""
    text: str = ""
    start_offset: int = 0
    end_offset: int = 0
    approx: bool = False


@dataclass
class EpisodeSplit:
    """切分结果：``preamble`` 是第一个集标记之前的原文（人物表 / 序言）。"""

    chunks: list[EpisodeChunk] = field(default_factory=list)
    preamble: str = ""
    mode: str = "markers"

    def __len__(self) -> int:
        return len(self.chunks)


# 场次标题行判定（第X集 / 场次N）——原 ``script_writing._SECTION_RE``。
# 与原定义逐字符一致：不加 re.M，按单行 ``.match()`` 调用，因此 script_writing
# 迁到本常量后行为完全不变。全文扫描请用下面的 ``_EPISODE_MARK_RE``。
SECTION_LINE_RE = re.compile(
    r"^\s*(第\s*[0-9一二三四五六七八九十]+\s*集|场次\s*[0-9一二三四五六七八九十]+)"
)


def is_section_line(line: str) -> bool:
    """判断一行是否为场次/集标记行（台词过滤与人物抽取共用同一口径）。"""
    return bool(SECTION_LINE_RE.match(line or ""))


# 集标记：行首 +「第X集」，X 支持阿拉伯数字与中文数字。
# 用 ``[ \t]*`` 而不是 ``\s*``：多行模式下 ``\s`` 会吃掉空行，把标记的
# start_offset 前移，导致切出的片段带上不属于它的空行。
_EPISODE_MARK_RE = re.compile(
    r"^[ \t]*第[ \t]*(?P<num>[0-9]+|[一二三四五六七八九十百千零〇两]+)[ \t]*集[ \t]*(?P<title>[^\n]*)$",
    re.M,
)

# 场次标题行（粗切场景时用，不做集号解析）
_SCENE_MARK_RE = re.compile(
    r"^[ \t]*(场次[ \t]*[0-9一二三四五六七八九十]+|场景[ \t]*[0-9一二三四五六七八九十]+)",
    re.M,
)

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def cn_to_int(raw: str) -> int:
    """把「12」或「十二 / 二十三 / 一百零三」转成整数；无法识别返回 0。"""
    s = (raw or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    section = 0
    number = 0
    for ch in s:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
        else:
            return 0
    return section + number


def split_episodes(text: str) -> EpisodeSplit | None:
    """按「第X集」行首标记切分正文。

    返回 ``EpisodeSplit``（含 chunks 与 preamble）；没有任何集标记时返回 None，
    调用方据此决定走 ``fallback_split`` 还是继续用全文。
    """
    source = text or ""
    marks = list(_EPISODE_MARK_RE.finditer(source))
    if not marks:
        return None
    preamble = source[: marks[0].start()]
    chunks: list[EpisodeChunk] = []
    for idx, m in enumerate(marks):
        start = m.start()
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(source)
        number = cn_to_int(m.group("num")) or (idx + 1)
        chunks.append(
            EpisodeChunk(
                number=number,
                title=(m.group("title") or "").strip(),
                text=source[start:end],
                start_offset=start,
                end_offset=end,
            )
        )
    return EpisodeSplit(chunks=chunks, preamble=preamble, mode="markers")


def slice_range(chunks: list[EpisodeChunk], start: int, end: int) -> str:
    """把集号落在 [start, end] 的片段按原顺序拼接；无命中时返回空串。"""
    picked = [c for c in (chunks or []) if start <= c.number <= end]
    picked.sort(key=lambda c: c.start_offset)
    return "".join(c.text for c in picked)


def _blocks(text: str) -> list[tuple[int, int]]:
    """按「场次标题 / 空行段落」切出粗粒度块边界，保证不在句中切开。"""
    source = text or ""
    cuts: list[int] = [0]
    for m in _SCENE_MARK_RE.finditer(source):
        # 场次标题若紧跟在段首，则把它作为新块起点
        line_start = source.rfind("\n", 0, m.start()) + 1
        if line_start > 0:
            cuts.append(line_start)
    for m in re.finditer(r"\n[ \t]*\n", source):
        cuts.append(m.end())
    cuts = sorted({c for c in cuts if 0 <= c < len(source)} or {0})
    bounds: list[tuple[int, int]] = []
    for i, c in enumerate(cuts):
        nxt = cuts[i + 1] if i + 1 < len(cuts) else len(source)
        if nxt > c:
            bounds.append((c, nxt))
    return bounds or [(0, len(source))]


def fallback_split(
    text: str, total_episodes: int, *, overlap_chars: int = 0
) -> list[EpisodeChunk]:
    """无集标记时，按段落/场次边界把全文近似均分成 ``total_episodes`` 段。

    只在块边界处切开，不会切断句子；切出的片段 ``approx=True``，调用方应据此
    在事件与警告中标注「近似切分」。``overlap_chars`` 让相邻片段保留少量重叠
    正文，减少跨段上下文丢失。
    """
    source = text or ""
    total = max(1, int(total_episodes or 1))
    if not source:
        return [
            EpisodeChunk(number=i + 1, text="", start_offset=0, end_offset=0, approx=True)
            for i in range(total)
        ]
    bounds = _blocks(source)
    total_chars = len(source)
    target = total_chars / total
    chunks: list[EpisodeChunk] = []
    cur_start = bounds[0][0]
    number = 0
    for i, (_bs, be) in enumerate(bounds):
        # 用本块结束位置的绝对偏移判断是否达到目标边界（旧实现按块起点累加，
        # 首批 acc 恒为 0，导致首段偏短、末段超长）。
        # 剩余块数（不含本块）必须够填满剩余集号，否则不再切。
        remaining_groups = total - number - 1
        remaining_blocks = len(bounds) - i - 1
        if (
            number < total - 1
            and be >= target * (number + 1)
            and remaining_blocks >= remaining_groups
        ):
            number += 1
            chunks.append(
                EpisodeChunk(
                    number=number,
                    text=source[cur_start:be],
                    start_offset=cur_start,
                    end_offset=be,
                    approx=True,
                )
            )
            cur_start = max(0, be - max(0, int(overlap_chars or 0)))
    number += 1
    chunks.append(
        EpisodeChunk(
            number=number,
            text=source[cur_start:],
            start_offset=cur_start,
            end_offset=len(source),
            approx=True,
        )
    )
    # 块数少于集数时补空片段，保证集号连续可索引
    while len(chunks) < total:
        chunks.append(
            EpisodeChunk(
                number=len(chunks) + 1,
                text="",
                start_offset=len(source),
                end_offset=len(source),
                approx=True,
            )
        )
    return chunks[:total]
