"""A3：音频输入校验（v2 §7.6 / §8.4）。

用真实 ffmpeg 生成测试音频（不是 mock），验证：
- 合法音频通过
- 文件不存在 / 空文件 / 非音频文件被拒
- 采样率、声道、时长、格式约束真的生效
- 约束只从工作流声明读，未声明不凭空编造要求
- 批量校验能把失败定位到具体槽位与角色
"""
import shutil
import subprocess

import pytest

from app.services.workflow import audio_check

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="需要 ffmpeg/ffprobe",
)


def _make_audio(path, *, freq=440, duration=1.0, rate=24000, channels=1, codec="libmp3lame", ext="mp3"):
    """用 ffmpeg 生成真实音频文件。"""
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}:sample_rate={rate}",
        "-ac", str(channels),
    ]
    if codec:
        cmd += ["-c:a", codec]
    out = path.with_suffix(f".{ext}")
    cmd.append(str(out))
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return out


# --- probe ---------------------------------------------------------------

def test_probe_real_mp3_reports_true_parameters(tmp_path):
    f = _make_audio(tmp_path / "a", duration=1.5, rate=24000, channels=1)
    probe = audio_check.probe_audio(str(f))
    assert probe.ok is True, probe.error
    assert probe.sample_rate == 24000
    assert probe.channels == 1
    assert 1400 <= probe.duration_ms <= 1600
    assert probe.codec == "mp3"


def test_probe_missing_file_rejected(tmp_path):
    probe = audio_check.probe_audio(str(tmp_path / "nope.mp3"))
    assert probe.ok is False
    assert "不存在" in probe.error


def test_probe_empty_file_rejected(tmp_path):
    f = tmp_path / "empty.mp3"
    f.write_bytes(b"")
    probe = audio_check.probe_audio(str(f))
    assert probe.ok is False
    assert "0" in probe.error


def test_probe_non_audio_file_rejected(tmp_path):
    """纯文本伪装成 mp3：ffprobe 读不到音频流 → 必须拒绝。"""
    f = tmp_path / "fake.mp3"
    f.write_text("this is not audio" * 100, encoding="utf-8")
    probe = audio_check.probe_audio(str(f))
    assert probe.ok is False
    assert probe.error


# --- 约束读取 -------------------------------------------------------------

def test_constraints_empty_when_not_declared():
    """工作流未声明约束 → 返回空 dict，不凭空编造采样率要求（AGENTS.md #10）。"""
    assert audio_check.constraints_from_capabilities({}) == {}
    assert audio_check.constraints_from_capabilities({"audio": True}) == {}
    assert audio_check.constraints_from_capabilities(None) == {}


def test_constraints_parsed_from_capabilities():
    caps = {
        "audio_input_constraints": {
            "formats": ["wav", "mp3"],
            "sample_rates": [16000, 24000],
            "channels": [1],
            "max_duration_ms": 12000,
            "min_duration_ms": 200,
            "ignored_key": "x",
        }
    }
    cons = audio_check.constraints_from_capabilities(caps)
    assert cons["formats"] == ["wav", "mp3"]
    assert cons["sample_rates"] == [16000, 24000]
    assert cons["channels"] == [1]
    assert cons["max_duration_ms"] == 12000
    assert cons["min_duration_ms"] == 200
    assert "ignored_key" not in cons


def test_constraints_ignore_invalid_shapes():
    caps = {"audio_input_constraints": {"formats": "mp3", "max_duration_ms": -5, "channels": []}}
    assert audio_check.constraints_from_capabilities(caps) == {}


# --- 校验规则 -------------------------------------------------------------

def test_valid_audio_passes_with_declared_constraints(tmp_path):
    f = _make_audio(tmp_path / "ok", duration=1.0, rate=24000, channels=1)
    res = audio_check.check_audio_input(
        str(f), {"formats": ["mp3"], "sample_rates": [24000], "channels": [1]}
    )
    assert res.ok is True, res.violations


def test_sample_rate_violation_reported(tmp_path):
    f = _make_audio(tmp_path / "sr", rate=44100)
    res = audio_check.check_audio_input(str(f), {"sample_rates": [16000, 24000]})
    assert res.ok is False
    assert any("采样率" in v and "44100" in v for v in res.violations)


def test_channel_violation_reported(tmp_path):
    f = _make_audio(tmp_path / "ch", channels=2)
    res = audio_check.check_audio_input(str(f), {"channels": [1]})
    assert res.ok is False
    assert any("声道" in v for v in res.violations)


def test_duration_over_max_rejected(tmp_path):
    f = _make_audio(tmp_path / "long", duration=3.0)
    res = audio_check.check_audio_input(str(f), {"max_duration_ms": 1000})
    assert res.ok is False
    assert any("超过上限" in v for v in res.violations)


def test_duration_under_min_rejected(tmp_path):
    f = _make_audio(tmp_path / "short", duration=0.1)
    res = audio_check.check_audio_input(str(f), {"min_duration_ms": 500})
    assert res.ok is False
    assert any("低于下限" in v for v in res.violations)


def test_format_violation_reported(tmp_path):
    f = _make_audio(tmp_path / "fmt", ext="wav", codec="pcm_s16le")
    res = audio_check.check_audio_input(str(f), {"formats": ["mp3"]})
    assert res.ok is False
    assert any("格式" in v for v in res.violations)


def test_no_constraints_only_checks_usability(tmp_path):
    """未声明约束时只校验「能读出来」，不因为采样率/声道不同而失败。"""
    f = _make_audio(tmp_path / "free", rate=44100, channels=2, duration=1.0)
    res = audio_check.check_audio_input(str(f), {})
    assert res.ok is True, res.violations


def test_multiple_violations_all_reported(tmp_path):
    f = _make_audio(tmp_path / "multi", rate=44100, channels=2, duration=5.0)
    res = audio_check.check_audio_input(
        str(f), {"sample_rates": [24000], "channels": [1], "max_duration_ms": 1000}
    )
    assert res.ok is False
    assert len(res.violations) == 3


# --- 批量（多角色） -------------------------------------------------------

def test_batch_all_pass(tmp_path):
    a = _make_audio(tmp_path / "r0", freq=440)
    b = _make_audio(tmp_path / "r1", freq=660)
    verdict = audio_check.check_audio_batch(
        [
            {"slot": "ref_audio_0", "file_path": str(a), "character_id": "chr_a"},
            {"slot": "ref_audio_1", "file_path": str(b), "character_id": "chr_b"},
        ],
        {"formats": ["mp3"]},
    )
    assert verdict["ok"] is True
    assert verdict["failed_slots"] == []
    assert [c["slot"] for c in verdict["checks"]] == ["ref_audio_0", "ref_audio_1"]


def test_batch_failure_identifies_slot_and_character(tmp_path):
    """第 2 个角色的音频坏了 → 报错必须指出是 ref_audio_1 / chr_b，不能只说「音频有问题」。"""
    good = _make_audio(tmp_path / "good", freq=440)
    bad = tmp_path / "bad.mp3"
    bad.write_text("broken", encoding="utf-8")
    verdict = audio_check.check_audio_batch(
        [
            {"slot": "ref_audio_0", "file_path": str(good), "character_id": "chr_a"},
            {"slot": "ref_audio_1", "file_path": str(bad), "character_id": "chr_b"},
        ]
    )
    assert verdict["ok"] is False
    assert verdict["failed_slots"] == ["ref_audio_1"]
    failed = [c for c in verdict["checks"] if not c["ok"]]
    assert len(failed) == 1
    assert failed[0]["character_id"] == "chr_b"
    # 违规信息里带槽位标签，便于任务详情直接展示
    assert any("ref_audio_1" in v for v in failed[0]["violations"])


def test_batch_empty_is_ok(tmp_path):
    """没有参考音频不算失败（H3 可以不配音）。"""
    verdict = audio_check.check_audio_batch([], {})
    assert verdict["ok"] is True
    assert verdict["checks"] == []
