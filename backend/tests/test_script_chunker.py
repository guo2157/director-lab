"""剧本分集切片测试（阶段 1：按集切片）。

覆盖：
- 阿拉伯数字 / 中文数字集标记（第3集、第三集、第十二集、第 12 集）
- 无集标记时返回 None（调用方走降级）
- 第一个标记之前的内容作为 preamble 保留
- 台词里出现「第1集」不误切（行首锚定）
- 首尾边界（正文即标记、单集、末集无换行）
- slice_range 只拼本批集
- fallback_split 按段落边界近似均分、不丢字
"""
from __future__ import annotations

import pytest

from app.services.script_chunker import (
    EpisodeChunk,
    cn_to_int,
    fallback_split,
    is_section_line,
    slice_range,
    split_episodes,
)

SCRIPT_5 = """人物表：
林昭——女主
陈默——男主

第1集 归途
场次1 黄昏 车站
林昭：我回来了。
陈默：三年了。

第2集 旧信
场次1 夜 书房
林昭：这封信是谁写的？

第3集 对峙
场次1 日 天台
陈默：别再查了。

第4集 真相
场次1 夜 仓库
林昭：原来是你。

第5集 落幕
场次1 黎明 海边
陈默：结束了。
"""


# ============================================== 中文数字解析
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3", 3), ("12", 12), ("一", 1), ("十", 10), ("十二", 12),
        ("二十", 20), ("二十三", 23), ("一百零三", 103), ("两", 2),
        ("", 0), ("X", 0),
    ],
)
def test_cn_to_int(raw, expected):
    assert cn_to_int(raw) == expected


# ============================================== 基本切分
def test_split_episodes_basic():
    split = split_episodes(SCRIPT_5)
    assert split is not None
    assert split.mode == "markers"
    assert [c.number for c in split.chunks] == [1, 2, 3, 4, 5]
    assert split.chunks[0].title == "归途"
    assert split.chunks[3].title == "真相"
    for c in split.chunks:
        assert c.text.lstrip().startswith(f"第{c.number}集")


def test_preamble_preserved():
    split = split_episodes(SCRIPT_5)
    assert "人物表" in split.preamble
    assert "林昭——女主" in split.preamble
    assert "人物表" not in split.chunks[0].text


def test_offsets_are_contiguous():
    split = split_episodes(SCRIPT_5)
    chunks = split.chunks
    for prev, cur in zip(chunks, chunks[1:]):
        assert prev.end_offset == cur.start_offset
    assert chunks[-1].end_offset == len(SCRIPT_5)
    assert chunks[0].start_offset == len(split.preamble)


# ============================================== 数字形态
@pytest.mark.parametrize(
    "marker,number",
    [
        ("第3集 三", 3),
        ("第三集 三", 3),
        ("第十二集 十二", 12),
        ("第 12 集 十二", 12),
        ("第二十集 二十", 20),
    ],
)
def test_marker_number_forms(marker, number):
    text = f"{marker}\n正文A\n"
    split = split_episodes(text)
    assert split is not None
    assert len(split.chunks) == 1
    assert split.chunks[0].number == number


# ============================================== 无标记与误切
def test_no_marker_returns_none():
    assert split_episodes("林昭：我回来了。\n陈默：三年了。") is None


def test_empty_text_returns_none():
    assert split_episodes("") is None
    assert split_episodes(None) is None


def test_episode_marker_inside_dialogue_not_split():
    """台词里提到「第1集」不是分集标记（必须行首）。"""
    text = (
        "第1集 开篇\n"
        "林昭：我们第1集说过的话你还记得吗？\n"
        "陈默：记得。\n"
    )
    split = split_episodes(text)
    assert split is not None
    assert len(split.chunks) == 1
    assert "我们第1集说过" in split.chunks[0].text


def test_is_section_line_consistency():
    assert is_section_line("第1集 归途") is True
    assert is_section_line("场次3 黄昏") is True
    assert is_section_line("第十二集") is True
    assert is_section_line("林昭：第1集的事") is False
    assert is_section_line("") is False


# ============================================== 边界
def test_text_starts_with_marker():
    split = split_episodes("第1集 A\n内容\n第2集 B\n内容2\n")
    assert split is not None
    assert split.preamble == ""
    assert [c.number for c in split.chunks] == [1, 2]


def test_single_episode_no_trailing_newline():
    text = "第1集 唯一\n林昭：结束"
    split = split_episodes(text)
    assert split is not None
    assert len(split.chunks) == 1
    assert split.chunks[0].text == text


def test_last_chunk_extends_to_end():
    split = split_episodes(SCRIPT_5)
    assert split.chunks[-1].text.endswith("结束了。\n")


# ============================================== slice_range
def test_slice_range_only_returns_requested_episodes():
    split = split_episodes(SCRIPT_5)
    got = slice_range(split.chunks, 2, 3)
    assert "第2集" in got and "第3集" in got
    assert "第1集" not in got
    assert "第4集" not in got and "第5集" not in got
    assert "这封信是谁写的" in got
    assert "别再查了" in got
    assert "我回来了" not in got
    assert "原来是你" not in got


def test_slice_range_out_of_bounds_returns_empty():
    split = split_episodes(SCRIPT_5)
    assert slice_range(split.chunks, 9, 12) == ""


def test_slice_range_keeps_original_order():
    chunks = [
        EpisodeChunk(number=2, text="B", start_offset=10, end_offset=11),
        EpisodeChunk(number=1, text="A", start_offset=0, end_offset=1),
    ]
    assert slice_range(chunks, 1, 2) == "AB"


def test_slice_range_single_batch_is_much_shorter():
    split = split_episodes(SCRIPT_5)
    for ep in range(1, 6):
        part = slice_range(split.chunks, ep, ep)
        assert 0 < len(part) < len(SCRIPT_5) * 0.5


# ============================================== fallback_split
FALLBACK_TEXT = (
    "场景一\n林昭走进车站，四下张望。\n\n"
    "场景二\n陈默在长椅上等她。\n\n"
    "场景三\n两人并肩走出。\n\n"
    "场景四\n天开始下雨。\n\n"
    "场景五\n林昭撑开伞。\n\n"
    "场景六\n陈默接过来。\n"
)


def test_fallback_split_makes_requested_count():
    chunks = fallback_split(FALLBACK_TEXT, 3)
    assert len(chunks) == 3
    assert [c.number for c in chunks] == [1, 2, 3]
    assert all(c.approx is True for c in chunks)


def test_fallback_split_covers_all_text_without_loss():
    chunks = fallback_split(FALLBACK_TEXT, 3)
    assert "".join(c.text for c in chunks) == FALLBACK_TEXT


def test_fallback_split_balanced():
    """近似均分：最长段不应超过平均段长的 2 倍（旧实现首段过短、末段超长）。"""
    chunks = fallback_split(FALLBACK_TEXT, 3)
    lengths = [len(c.text) for c in chunks]
    avg = len(FALLBACK_TEXT) / 3
    assert max(lengths) <= avg * 2
    assert all(n > 0 for n in lengths)


def test_fallback_split_no_empty_chunks_when_blocks_enough():
    chunks = fallback_split(FALLBACK_TEXT, 2)
    assert all(c.text.strip() for c in chunks)


def test_fallback_split_more_episodes_than_blocks_pads_empty():
    chunks = fallback_split("场景一\n只有一小段。\n", 5)
    assert len(chunks) == 5
    assert [c.number for c in chunks] == [1, 2, 3, 4, 5]
    assert "只有一小段" in "".join(c.text for c in chunks)


def test_fallback_split_empty_text():
    chunks = fallback_split("", 2)
    assert len(chunks) == 2
    assert all(c.text == "" for c in chunks)


def test_fallback_split_does_not_cut_mid_sentence():
    chunks = fallback_split(FALLBACK_TEXT, 3)
    for c in chunks[:-1]:
        assert c.text.endswith("\n")
