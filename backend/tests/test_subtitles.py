"""字幕生成与烧录测试（PRD M7）：ASS 排版、SRT 时间轴、libass 真实烧录。

背景：烧录模式原先直接把 SRT 交给 libass 默认样式，未指定中文字体与字号，
9:16 成片上长句会挤成三行且可能落到没有 CJK 字形的回退字体。
现改为按成片分辨率生成 ASS（显式字体/字号/折行），此文件覆盖该逻辑。
"""
import subprocess

import pytest

from app.services.compose import (
    ShotInput,
    TimelineItem,
    TimelineResult,
    build_ass,
    burn_subtitle_cmd,
    generate_srt,
    subtitle_layout,
    _wrap_cue,
)


def _shot(idx: int, dialogue: str, duration: float = 6.0) -> ShotInput:
    return ShotInput(
        shot_id=f"sht_{idx}", episode=1, seq=idx + 1, scene_seq=1, duration=int(duration),
        actual_duration=duration, audio_source="h3_va", status="done", dialogue=dialogue,
        video_path=f"/tmp/clip_{idx}.mp4", video_asset_id=f"ast_{idx}", video_asset_version=1,
        audio_path=None, audio_asset_id=None, audio_asset_version=None,
    )


def _timeline(*dialogues: str, duration: float = 6.0) -> TimelineResult:
    items, cursor = [], 0.0
    for i, text in enumerate(dialogues):
        items.append(TimelineItem(
            shot=_shot(i, text, duration), target_duration=duration,
            start_time=cursor, align_mode="none", video_duration=duration,
        ))
        cursor += duration
    return TimelineResult(items=items, total_duration=cursor)


# --- 排版 ---
def test_layout_scales_with_resolution():
    """竖屏字号应大于同高度比例的 16:9，且每行字数随宽度收窄。"""
    portrait = subtitle_layout(720, 1280)
    landscape = subtitle_layout(1280, 720)
    assert portrait["font_size"] > landscape["font_size"]
    assert portrait["chars_per_line"] < landscape["chars_per_line"]
    assert portrait["margin_v"] > landscape["margin_v"]


def test_wrap_cue_prefers_punctuation_and_limits_lines():
    lines = _wrap_cue("文若，进爵魏公、加九锡，是朝廷体面，你也要拦？", 14)
    assert all(len(line) <= 14 for line in lines), lines
    assert len(lines) >= 2
    # 折行点尽量落在标点后
    assert any(line.endswith(("，", "、", "？")) for line in lines[:-1])


def test_build_ass_style_and_events():
    ass = build_ass(_timeline("好无聊啊，我想捉弄大家！"), width=720, height=1280)
    assert "[V4+ Styles]" in ass and "[Events]" in ass
    assert "PlayResX: 720" in ass and "PlayResY: 1280" in ass
    assert "Dialogue: 0,0:00:00.00" in ass
    assert "好无聊啊" in ass
    assert "subtitle_font" not in ass  # 字体名来自 fc-list，不该是占位符
    style = next(line for line in ass.splitlines() if line.startswith("Style: Default,"))
    fields = style.split(",")
    assert fields[1]  # Fontname 非空
    assert int(fields[2]) >= 18  # Fontsize


def test_build_ass_long_dialogue_splits_and_wraps():
    text = "曹操：文若，进爵魏公、加九锡，是朝廷体面，你也要拦？荀彧：丞相，九锡之礼非人臣所宜受。"
    ass = build_ass(_timeline(text, duration=12.0), width=720, height=1280)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(events) >= 2
    assert all("\\N" in event or len(event.rsplit(",,", 1)[-1]) <= 30 for event in events)


def test_ass_cues_stay_within_shot_boundaries():
    ass = build_ass(_timeline("第一镜台词", "第二镜台词", duration=4.0), width=1280, height=720)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]

    def _secs(tc: str) -> float:
        h, m, rest = tc.split(":")
        s, cs = rest.split(".")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100

    for event in events:
        _, start, end, *_ = event.split(",", 3)
        s0, s1 = _secs(start), _secs(end)
        assert 0.0 <= s0 < s1 <= 8.0 + 0.01


# --- SRT（外挂模式）行为保持不变 ---
def test_generate_srt_uses_dialogue_and_shot_bounds():
    srt = generate_srt(_timeline("第一句", "第二句", duration=3.0))
    assert "00:00:00,000 --> 00:00:03,000" in srt
    assert "00:00:03,000 --> 00:00:06,000" in srt
    assert "第一句" in srt and "第二句" in srt


def test_generate_srt_skips_empty_dialogue():
    timeline = _timeline("有台词", "")
    srt = generate_srt(timeline)
    assert "有台词" in srt
    assert srt.count("-->") == 1


# --- 真实烧录 ---
def test_burn_subtitle_produces_playable_video(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=navy:s=320x240:d=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(clip)],
        check=True, capture_output=True,
    )
    timeline = _timeline("测试字幕内容", duration=3.0)
    ass_path = tmp_path / "sub.ass"
    ass_path.write_text(build_ass(timeline, width=320, height=240), encoding="utf-8")
    out = tmp_path / "burned.mp4"
    subprocess.run(burn_subtitle_cmd(str(clip), str(ass_path), str(out)),
                   check=True, capture_output=True)
    assert out.exists() and out.stat().st_size > 0

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
        check=True, capture_output=True, text=True,
    )
    assert float(probe.stdout.strip()) == pytest.approx(3.0, abs=0.2)
