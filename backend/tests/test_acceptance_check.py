"""成片验收检查测试（阶段11）：导出文件质量合规与完整性校验。

覆盖：
- 文件不存在 → fail
- 文件为空 → fail
- ffprobe 不可用 → partial（降级为基础检查）
- 正常文件全部通过 → pass
- 分辨率不匹配 → warning
- 时长偏差超容差 → warning
- 音频轨缺失 → fail
- 验收报告结构完整性校验
"""
import json
import os
import subprocess
from unittest.mock import patch, MagicMock

import pytest
from app.services.acceptance_check import (
    AcceptanceReport,
    CheckItem,
    run_acceptance_check,
)


@pytest.fixture()
def tmp_video(tmp_path):
    """创建一个临时空文件模拟视频文件。"""
    f = tmp_path / "test_export.mp4"
    f.write_bytes(b"\x00" * 1024)
    return str(f)


def _mock_ffprobe_output(width=1080, height=1920, duration=90.0, has_audio=True, has_subtitle=False):
    """Construct ffprobe JSON output."""
    streams = [{
        "codec_type": "video",
        "codec_name": "h264",
        "width": width,
        "height": height,
        "r_frame_rate": "30/1",
    }]
    if has_audio:
        streams.append({
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "44100",
            "channels": 2,
        })
    if has_subtitle:
        streams.append({
            "codec_type": "subtitle",
            "codec_name": "mov_text",
        })
    return {
        "streams": streams,
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": str(duration),
            "bit_rate": "2000000",
        },
    }


def test_file_not_exists():
    """file not exists -> fail."""
    report = run_acceptance_check(
        export_id="exp-1", file_path="/nonexistent/file.mp4", file_name="file.mp4",
    )
    assert report.overall_status == "fail"
    assert any(c.name == "file_exists" and c.status == "fail" for c in report.checks)


def test_file_empty(tmp_path):
    """file size 0 -> fail."""
    f = tmp_path / "empty.mp4"
    f.write_bytes(b"")
    report = run_acceptance_check(
        export_id="exp-2", file_path=str(f), file_name="empty.mp4",
    )
    assert report.overall_status == "fail"
    assert any(c.name == "file_non_empty" and c.status == "fail" for c in report.checks)


def test_ffprobe_unavailable(tmp_video):
    """ffprobe unavailable -> partial, degraded to basic checks."""
    with patch("app.services.acceptance_check._run_ffprobe", return_value=None):
        report = run_acceptance_check(
            export_id="exp-3", file_path=tmp_video, file_name="test_export.mp4",
        )
    assert report.overall_status == "partial"
    assert any(c.name == "ffprobe_available" and c.status == "skipped" for c in report.checks)
    assert any(c.name == "file_exists" and c.status == "pass" for c in report.checks)


def test_all_checks_pass(tmp_video):
    """all checks pass -> pass."""
    mock_data = _mock_ffprobe_output(width=1080, height=1920, duration=90.0)
    with patch("app.services.acceptance_check._run_ffprobe", return_value=mock_data):
        report = run_acceptance_check(
            export_id="exp-4", file_path=tmp_video, file_name="test_export.mp4",
            expected_duration=90.0,
            expected_resolution=(1080, 1920),
            require_audio=True,
        )
    assert report.overall_status == "pass"
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["file_exists"] == "pass"
    assert statuses["file_non_empty"] == "pass"
    assert statuses["video_stream"] == "pass"
    assert statuses["audio_track"] == "pass"
    assert statuses["resolution"] == "pass"
    assert statuses["duration"] == "pass"


def test_resolution_mismatch(tmp_video):
    """resolution mismatch -> warning."""
    mock_data = _mock_ffprobe_output(width=720, height=1280, duration=90.0)
    with patch("app.services.acceptance_check._run_ffprobe", return_value=mock_data):
        report = run_acceptance_check(
            export_id="exp-5", file_path=tmp_video, file_name="test_export.mp4",
            expected_resolution=(1080, 1920),
        )
    assert report.overall_status == "partial"
    res_check = next(c for c in report.checks if c.name == "resolution")
    assert res_check.status == "warning"
    assert "720x1280" in res_check.actual


def test_duration_mismatch(tmp_video):
    """duration deviation beyond tolerance -> warning."""
    mock_data = _mock_ffprobe_output(duration=60.0)
    with patch("app.services.acceptance_check._run_ffprobe", return_value=mock_data):
        report = run_acceptance_check(
            export_id="exp-6", file_path=tmp_video, file_name="test_export.mp4",
            expected_duration=90.0,
        )
    assert report.overall_status == "partial"
    dur_check = next(c for c in report.checks if c.name == "duration")
    assert dur_check.status == "warning"


def test_audio_missing(tmp_video):
    """audio required but missing -> fail."""
    mock_data = _mock_ffprobe_output(has_audio=False)
    with patch("app.services.acceptance_check._run_ffprobe", return_value=mock_data):
        report = run_acceptance_check(
            export_id="exp-7", file_path=tmp_video, file_name="test_export.mp4",
            require_audio=True,
        )
    assert report.overall_status == "fail"
    audio_check = next(c for c in report.checks if c.name == "audio_track")
    assert audio_check.status == "fail"


def test_subtitle_optional(tmp_video):
    """subtitle not required -> skipped."""
    mock_data = _mock_ffprobe_output(has_subtitle=False)
    with patch("app.services.acceptance_check._run_ffprobe", return_value=mock_data):
        report = run_acceptance_check(
            export_id="exp-8", file_path=tmp_video, file_name="test_export.mp4",
            require_subtitle=False,
        )
    sub_check = next(c for c in report.checks if c.name == "subtitle_track")
    assert sub_check.status == "skipped"


def test_report_to_dict_structure(tmp_video):
    """report to_dict structure completeness."""
    mock_data = _mock_ffprobe_output()
    with patch("app.services.acceptance_check._run_ffprobe", return_value=mock_data):
        report = run_acceptance_check(
            export_id="exp-9", file_path=tmp_video, file_name="test_export.mp4",
        )
    d = report.to_dict()
    assert d["export_id"] == "exp-9"
    assert d["overall_status"] in ("pass", "partial", "fail")
    assert d["file_name"] == "test_export.mp4"
    assert d["file_size"] > 0
    assert isinstance(d["checks"], list)
    assert len(d["checks"]) > 0
    for c in d["checks"]:
        assert "name" in c
        assert "status" in c
        assert "detail" in c
    assert isinstance(d["metadata"], dict)
    assert "summary" in d
