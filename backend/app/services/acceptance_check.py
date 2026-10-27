"""成片验收检查服务（阶段11）：导出文件质量合规与完整性校验。

设计约定：
- 在导出成功后或独立调用时，使用 ffprobe 提取导出文件元数据
- 执行验收清单：文件完整性、视频流存在、分辨率匹配、时长匹配、音频轨存在、字幕轨存在
- 生成结构化验收报告，包含 pass/fail 状态和逐项检查结果
- 不修改导出记录状态，仅提供只读验收报告
- ffprobe 不可用时降级为文件存在性检查并标记 skipped
"""
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CheckItem:
    """单项检查结果。"""
    name: str
    status: str  # pass / fail / skipped / warning
    detail: str = ""
    expected: str = ""
    actual: str = ""


@dataclass
class AcceptanceReport:
    """成片验收报告。"""
    export_id: str
    overall_status: str = ""  # pass / fail / partial
    file_name: str = ""
    file_size: int = 0
    checks: list[CheckItem] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "export_id": self.export_id,
            "overall_status": self.overall_status,
            "file_name": self.file_name,
            "file_size": self.file_size,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail,
                 "expected": c.expected, "actual": c.actual}
                for c in self.checks
            ],
            "metadata": self.metadata,
            "summary": self.summary,
        }


def _run_ffprobe(file_path: str, timeout: float = 30.0) -> dict | None:
    """调用 ffprobe 提取媒体文件元数据，失败返回 None。"""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", file_path],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _parse_duration(duration_str: str) -> float:
    """将 ffprobe 返回的时长字符串转为浮点秒数。"""
    try:
        return float(duration_str)
    except (ValueError, TypeError):
        return 0.0


def run_acceptance_check(
    export_id: str,
    file_path: str,
    file_name: str,
    *,
    expected_duration: float | None = None,
    expected_resolution: tuple[int, int] | None = None,
    aspect_ratio: str | None = None,
    require_audio: bool = True,
    require_subtitle: bool = False,
) -> AcceptanceReport:
    """对导出文件执行成片验收检查，返回结构化验收报告。

    参数：
    - export_id: 导出记录 ID
    - file_path: 导出文件绝对路径
    - file_name: 导出文件名
    - expected_duration: 期望时长（秒），None 则跳过时长检查
    - expected_resolution: 期望分辨率 (width, height)，None 则跳过分辨率检查
    - aspect_ratio: 项目宽高比（如 '9:16'），用于辅助分辨率校验
    - require_audio: 是否要求存在音频轨
    - require_subtitle: 是否要求存在字幕轨
    """
    report = AcceptanceReport(export_id=export_id, file_name=file_name)
    checks: list[CheckItem] = []

    # 1. 文件存在性检查
    if not os.path.exists(file_path):
        checks.append(CheckItem(
            name="file_exists", status="fail",
            detail=f"导出文件不存在: {file_path}",
        ))
        report.checks = checks
        report.overall_status = "fail"
        report.summary = "导出文件不存在，验收失败"
        return report

    file_size = os.path.getsize(file_path)
    report.file_size = file_size
    checks.append(CheckItem(
        name="file_exists", status="pass",
        detail=f"文件存在，大小 {file_size / 1024 / 1024:.2f} MB",
        actual=str(file_size),
    ))

    # 2. 文件非空检查
    if file_size == 0:
        checks.append(CheckItem(
            name="file_non_empty", status="fail",
            detail="导出文件大小为 0 字节",
        ))
        report.checks = checks
        report.overall_status = "fail"
        report.summary = "导出文件为空，验收失败"
        return report

    checks.append(CheckItem(name="file_non_empty", status="pass", detail="文件非空"))

    # 3. ffprobe 元数据提取
    probe_data = _run_ffprobe(file_path)
    if probe_data is None:
        checks.append(CheckItem(
            name="ffprobe_available", status="skipped",
            detail="ffprobe 不可用或提取失败，跳过媒体级检查",
        ))
        report.checks = checks
        report.overall_status = "partial"
        report.summary = "文件存在但 ffprobe 不可用，仅完成基础检查"
        return report

    checks.append(CheckItem(name="ffprobe_available", status="pass", detail="ffprobe 元数据提取成功"))

    # 提取流信息
    streams = probe_data.get("streams", [])
    format_info = probe_data.get("format", {})
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    # 元数据存入报告
    report.metadata = {
        "format_name": format_info.get("format_name", ""),
        "duration": format_info.get("duration", ""),
        "bit_rate": format_info.get("bit_rate", ""),
        "video_count": len(video_streams),
        "audio_count": len(audio_streams),
        "subtitle_count": len(subtitle_streams),
    }
    if video_streams:
        vs = video_streams[0]
        report.metadata["video_codec"] = vs.get("codec_name", "")
        report.metadata["width"] = vs.get("width", 0)
        report.metadata["height"] = vs.get("height", 0)
        report.metadata["fps"] = vs.get("r_frame_rate", "")
    if audio_streams:
        as_ = audio_streams[0]
        report.metadata["audio_codec"] = as_.get("codec_name", "")
        report.metadata["sample_rate"] = as_.get("sample_rate", "")
        report.metadata["channels"] = as_.get("channels", 0)

    # 4. 视频流存在检查
    if video_streams:
        checks.append(CheckItem(
            name="video_stream", status="pass",
            detail=f"视频流存在: {report.metadata.get('video_codec', 'unknown')}",
        ))
    else:
        checks.append(CheckItem(
            name="video_stream", status="fail",
            detail="未检测到视频流",
        ))

    # 5. 音频轨检查
    if require_audio:
        if audio_streams:
            checks.append(CheckItem(
                name="audio_track", status="pass",
                detail=f"音频轨存在: {report.metadata.get('audio_codec', 'unknown')}, "
                       f"{report.metadata.get('channels', 0)} 通道",
            ))
        else:
            checks.append(CheckItem(
                name="audio_track", status="fail",
                detail="要求音频轨但未检测到",
            ))
    else:
        checks.append(CheckItem(
            name="audio_track", status="skipped",
            detail="未要求音频轨，跳过检查",
        ))

    # 6. 字幕轨检查
    if require_subtitle:
        if subtitle_streams:
            checks.append(CheckItem(
                name="subtitle_track", status="pass",
                detail=f"字幕轨存在: {len(subtitle_streams)} 条",
            ))
        else:
            checks.append(CheckItem(
                name="subtitle_track", status="warning",
                detail="要求字幕轨但未检测到（可能为硬字幕烧录）",
            ))
    else:
        checks.append(CheckItem(
            name="subtitle_track", status="skipped",
            detail="未要求字幕轨，跳过检查",
        ))

    # 7. 分辨率检查
    if expected_resolution and video_streams:
        vw = video_streams[0].get("width", 0)
        vh = video_streams[0].get("height", 0)
        ew, eh = expected_resolution
        if vw == ew and vh == eh:
            checks.append(CheckItem(
                name="resolution", status="pass",
                detail=f"分辨率匹配: {vw}x{vh}",
                expected=f"{ew}x{eh}", actual=f"{vw}x{vh}",
            ))
        else:
            checks.append(CheckItem(
                name="resolution", status="warning",
                detail=f"分辨率不匹配: 期望 {ew}x{eh}，实际 {vw}x{vh}",
                expected=f"{ew}x{eh}", actual=f"{vw}x{vh}",
            ))
    elif expected_resolution:
        checks.append(CheckItem(
            name="resolution", status="fail",
            detail="期望校验分辨率但无视频流",
        ))
    else:
        checks.append(CheckItem(
            name="resolution", status="skipped",
            detail="未指定期望分辨率，跳过检查",
        ))

    # 8. 时长检查
    actual_duration = _parse_duration(format_info.get("duration", "0"))
    if expected_duration is not None and actual_duration > 0:
        tolerance = max(1.0, expected_duration * 0.05)  # 5% 或至少 1 秒容差
        diff = abs(actual_duration - expected_duration)
        if diff <= tolerance:
            checks.append(CheckItem(
                name="duration", status="pass",
                detail=f"时长匹配: {actual_duration:.1f}s（期望 {expected_duration:.1f}s）",
                expected=f"{expected_duration:.1f}s", actual=f"{actual_duration:.1f}s",
            ))
        else:
            checks.append(CheckItem(
                name="duration", status="warning",
                detail=f"时长偏差 {diff:.1f}s 超出容差 {tolerance:.1f}s",
                expected=f"{expected_duration:.1f}s", actual=f"{actual_duration:.1f}s",
            ))
    else:
        checks.append(CheckItem(
            name="duration", status="skipped",
            detail="未指定期望时长或无法提取实际时长",
            actual=f"{actual_duration:.1f}s",
        ))

    # 9. 文件完整性检查（moov atom 在文件尾部则需 faststart 优化）
    if format_info.get("format_name", ""):
        checks.append(CheckItem(
            name="format_valid", status="pass",
            detail=f"媒体格式: {format_info.get('format_name', 'unknown')}",
        ))
    else:
        checks.append(CheckItem(
            name="format_valid", status="warning",
            detail="无法确定媒体格式",
        ))

    # 汇总
    report.checks = checks
    fail_count = sum(1 for c in checks if c.status == "fail")
    warning_count = sum(1 for c in checks if c.status == "warning")

    if fail_count > 0:
        report.overall_status = "fail"
        report.summary = f"验收失败：{fail_count} 项不通过，{warning_count} 项警告"
    elif warning_count > 0:
        report.overall_status = "partial"
        report.summary = f"部分通过：{warning_count} 项警告，无致命失败"
    else:
        report.overall_status = "pass"
        report.summary = "全部检查通过"

    return report
