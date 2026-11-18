"""音频输入校验（A3，v2 计划 §7.6 / §8.4）。

TTS 音频注入 H3 之前必须校验，否则不合格的音频会一路传到 ComfyUI 才失败
（或者更糟：静默产出口型错位的视频）。

约束来源分两级：
1. **工作流声明**：`capabilities.audio_input_constraints`（可选，由 meta 声明）
   ```json
   {"formats": ["wav","mp3"], "sample_rates": [16000,24000,44100],
    "channels": [1], "max_duration_ms": 15000, "min_duration_ms": 200}
   ```
2. **默认保守约束**：工作流未声明时只用「必须是有效音频 + 有可解析时长」，
   不凭空编造采样率要求（AGENTS.md 硬约束 #10 不得虚构）。

校验用 ffprobe 读取真实文件，不信任文件名后缀或数据库里的旧值。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# 未声明约束时的兜底：只拦「明显不能用」的情况
DEFAULT_MAX_DURATION_MS = 60_000
DEFAULT_MIN_DURATION_MS = 100
DEFAULT_FORMATS = ("wav", "mp3", "flac", "m4a", "ogg", "opus", "aac")


@dataclass
class AudioProbe:
    """ffprobe 结果（只取校验需要的字段）。"""

    ok: bool
    format: str = ""
    duration_ms: int = 0
    sample_rate: int = 0
    channels: int = 0
    codec: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "format": self.format,
            "duration_ms": self.duration_ms,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "codec": self.codec,
            "error": self.error,
        }


@dataclass
class AudioCheck:
    """单个文件的校验结论。"""

    path: str
    ok: bool
    probe: AudioProbe
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "ok": self.ok,
            "probe": self.probe.to_dict(),
            "violations": self.violations,
        }


def probe_audio(file_path: str, *, timeout: int = 30) -> AudioProbe:
    """用 ffprobe 读真实音频参数。文件不存在 / 无音频流 / ffprobe 失败都返回 ok=False。"""
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return AudioProbe(ok=False, error=f"文件不存在：{file_path}")
    if p.stat().st_size == 0:
        return AudioProbe(ok=False, error="文件大小为 0")
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(p)],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return AudioProbe(ok=False, error=f"ffprobe 调用失败：{exc}")
    if proc.returncode != 0:
        return AudioProbe(ok=False, error=(proc.stderr or "ffprobe failed").strip()[:200])
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return AudioProbe(ok=False, error=f"ffprobe 输出不是合法 JSON：{exc}")

    streams = info.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        return AudioProbe(ok=False, error="文件不含音频流")

    duration = 0.0
    if audio.get("duration"):
        try:
            duration = float(audio["duration"])
        except (TypeError, ValueError):
            duration = 0.0
    if duration <= 0:
        fmt_dur = (info.get("format") or {}).get("duration")
        if fmt_dur:
            try:
                duration = float(fmt_dur)
            except (TypeError, ValueError):
                duration = 0.0

    fmt = (info.get("format") or {}).get("format_name") or ""
    # ffprobe 返回逗号分隔的容器名（如 "mp3"、 "mov,mp4,m4a,3gp,3g2,mj2"），取首个做展示
    fmt_short = fmt.split(",")[0] if fmt else p.suffix.lstrip(".").lower()

    return AudioProbe(
        ok=True,
        format=fmt_short,
        duration_ms=int(round(duration * 1000)),
        sample_rate=int(audio.get("sample_rate") or 0),
        channels=int(audio.get("channels") or 0),
        codec=str(audio.get("codec_name") or ""),
    )


def constraints_from_capabilities(capabilities: dict | None) -> dict:
    """从工作流能力声明读音频输入约束；未声明的键不下发默认值（保持「未声明=不限制」）。"""
    caps = capabilities or {}
    raw = caps.get("audio_input_constraints")
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key in ("formats", "sample_rates", "channels"):
        val = raw.get(key)
        if isinstance(val, list) and val:
            out[key] = val
    for key in ("max_duration_ms", "min_duration_ms"):
        val = raw.get(key)
        if isinstance(val, (int, float)) and val > 0:
            out[key] = int(val)
    return out


def check_audio_input(file_path: str, constraints: dict | None = None, *,
                      label: str = "") -> AudioCheck:
    """校验单个音频输入。返回 ok=False 时 `violations` 列出全部违反项。"""
    cons = constraints or {}
    probe = probe_audio(file_path)
    prefix = f"{label}：" if label else ""
    if not probe.ok:
        return AudioCheck(path=file_path, ok=False, probe=probe,
                          violations=[f"{prefix}{probe.error or '音频不可用'}"])

    violations: list[str] = []

    formats = [str(f).lower() for f in (cons.get("formats") or [])]
    if formats and probe.format and probe.format.lower() not in formats:
        violations.append(
            f"{prefix}格式为 {probe.format}，工作流要求 {', '.join(formats)}"
        )

    rates = cons.get("sample_rates") or []
    if rates and probe.sample_rate and probe.sample_rate not in [int(r) for r in rates]:
        violations.append(
            f"{prefix}采样率 {probe.sample_rate}Hz，工作流要求 {', '.join(str(r) for r in rates)}Hz"
        )

    channels = cons.get("channels") or []
    if channels and probe.channels and probe.channels not in [int(c) for c in channels]:
        violations.append(
            f"{prefix}声道数 {probe.channels}，工作流要求 {', '.join(str(c) for c in channels)}"
        )

    max_ms = int(cons.get("max_duration_ms") or DEFAULT_MAX_DURATION_MS)
    min_ms = int(cons.get("min_duration_ms") or DEFAULT_MIN_DURATION_MS)
    if probe.duration_ms > max_ms:
        violations.append(
            f"{prefix}时长 {probe.duration_ms}ms 超过上限 {max_ms}ms"
        )
    if probe.duration_ms < min_ms:
        violations.append(
            f"{prefix}时长 {probe.duration_ms}ms 低于下限 {min_ms}ms"
        )

    return AudioCheck(path=file_path, ok=not violations, probe=probe, violations=violations)


def check_audio_batch(bindings: list[dict], constraints: dict | None = None) -> dict:
    """批量校验（H3 多角色参考音频）。

    bindings 形如 `{"slot": "ref_audio_0", "file_path": ..., "character_id": ...}`，
    与 `executor._group_audio_inputs` 的返回同构。

    返回：
    - `ok`：全部通过才 True
    - `checks`：逐条结论（含槽位与角色，便于报错定位到人）
    - `failed_slots`：不通过的槽位名
    """
    checks: list[dict] = []
    failed: list[str] = []
    for b in bindings or []:
        slot = b.get("slot") or ""
        path = b.get("file_path") or b.get("path") or ""
        label = f"槽位 {slot}"
        if b.get("character_id"):
            label += f"（{b['character_id']}）"
        result = check_audio_input(path, constraints, label=label)
        entry = result.to_dict()
        entry.update({
            "slot": slot,
            "character_id": b.get("character_id"),
            "segment_id": b.get("segment_id"),
        })
        checks.append(entry)
        if not result.ok:
            failed.append(slot)
    return {
        "ok": not failed,
        "checks": checks,
        "failed_slots": failed,
        "constraints": constraints or {},
    }
