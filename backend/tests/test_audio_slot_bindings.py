"""P4-A1/A2：H3 参考音频槽位必须记住「谁的声音」，且不静默丢人。

v2 计划 §8.6 要求：多角色对白要保存合并前的独立音频与角色映射；
槽位不足时必须显式告警，不允许静默丢弃。这两条此前都没有覆盖。
"""

from app.worker.executor import _group_audio_inputs


def _row(speaker: str | None, track: str, seq: int, ms: int, seg: str) -> dict:
    return {
        "file_path": f"/media/{seg}.mp3",
        "asset_id": f"ast_{seg}",
        "character_id": speaker,
        "track_type": track,
        "segment_id": seg,
        "sequence": seq,
        "duration_ms": ms,
    }


def test_two_speakers_become_two_bindings_with_identity():
    rows = [_row("chr_a", "dialogue", 1, 1200, "s1"), _row("chr_b", "dialogue", 2, 800, "s2")]
    out = _group_audio_inputs(rows)
    assert out["paths"] == ["/media/s1.mp3", "/media/s2.mp3"]
    assert [b["slot"] for b in out["bindings"]] == ["ref_audio_0", "ref_audio_1"]
    # 关键：绑定里必须带角色身份，否则任务详情无法回答「ref_audio_1 是谁」
    assert [b["character_id"] for b in out["bindings"]] == ["chr_a", "chr_b"]
    assert out["bindings"][0]["segment_id"] == "s1"
    assert out["total_ms"] == 2000
    assert out["overflow"] == []


def test_same_speaker_multiple_segments_uses_first_only():
    rows = [_row("chr_a", "dialogue", 1, 500, "s1"), _row("chr_a", "dialogue", 2, 700, "s2")]
    out = _group_audio_inputs(rows)
    assert len(out["paths"]) == 1
    assert out["bindings"][0]["segment_id"] == "s1"
    # 总时长仍按全部片段累加（帧数反算要用）
    assert out["total_ms"] == 1200


def test_fourth_speaker_is_reported_not_silently_dropped():
    rows = [
        _row("chr_a", "dialogue", 1, 400, "s1"),
        _row("chr_b", "dialogue", 2, 400, "s2"),
        _row("chr_c", "dialogue", 3, 400, "s3"),
        _row("chr_d", "dialogue", 4, 400, "s4"),
    ]
    out = _group_audio_inputs(rows)
    assert len(out["paths"]) == 3
    assert len(out["overflow"]) == 1
    assert out["overflow"][0]["character_id"] == "chr_d"
    assert out["overflow"][0]["reason"] == "max_audio_inputs_exceeded"
    assert out["total_ms"] == 1600


def test_narration_has_its_own_slot_and_no_character_id():
    rows = [_row(None, "narration", 1, 900, "n1"), _row("chr_a", "dialogue", 2, 600, "s1")]
    out = _group_audio_inputs(rows)
    assert len(out["paths"]) == 2
    assert out["bindings"][0]["character_id"] is None
    assert out["bindings"][0]["track_type"] == "narration"
    # 旁白与角色各占一槽，不能被合并
    assert out["bindings"][1]["character_id"] == "chr_a"
