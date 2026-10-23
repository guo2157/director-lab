"""合成服务层：PRD.md §6.7 M7 时间轴/音画对齐/字幕/混音规则的纯逻辑实现。

与数据库解耦：输入为镜头与其产物的普通数据结构，输出时间轴、SRT 文本与 FFmpeg 命令，
便于单元测试；实际命令执行与数据库交互在 worker/compose_executor.py。

核心规则（P0，不得各写一套）：
- 有配音镜头（h3_va 内嵌或 tts_fallback 补救）以 shot.actual_duration 为成片时长基准；
  无配音纯视觉/转场镜头以 shot.duration 为基准
- 视频与目标时长差值 ≤ compose_tolerance：短则末帧定格补齐（tpad）、长则截断（trim）；
  差值超容差且未审核放行 → 拒绝合成（镜头 review）
- 镜头起始时间 = 前面所有镜头时长之和，逐镜累加不四舍五入，仅写 SRT 时按毫秒取整
- 字幕按镜头归属不跨镜；dialogue 超 40 字按标点切分，时间按字符数比例分配
- BGM 人声优先：短于成片循环、长于成片结尾 1 秒淡出；混音后响度归一 -16 LUFS、峰值 -1 dBTP
"""
import json
import shlex
import subprocess
from dataclasses import dataclass, field

# --- 统一成片参数（concat 要求各段编码参数一致） ---
COMPOSE_FPS = 24
COMPOSE_AUDIO_RATE = 48000
RESOLUTION_BY_RATIO = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (720, 720),
                       "4:3": (960, 720), "3:4": (720, 960)}
DEFAULT_RESOLUTION = (1280, 720)
LOUDNESS_TARGET = "I=-16:TP=-1"  # PRD M7-04：-16 LUFS，峰值 -1 dBTP
SUBTITLE_SPLIT_THRESHOLD = 40  # 单句超 40 字切分
SUBTITLE_MIN_SECONDS = 1.0     # 最短显示时长（软约束）


@dataclass
class ShotInput:
    """合成输入的单个镜头及其产物。"""
    shot_id: str
    episode: int
    seq: int
    scene_seq: int
    duration: int
    actual_duration: float | None
    audio_source: str | None          # h3_va / tts_fallback / none
    status: str                        # pending/generating/review/done/redo
    dialogue: str | None
    video_path: str | None             # clip 资产文件路径
    video_asset_id: str | None
    video_asset_version: int | None
    audio_path: str | None             # tts_fallback 独立音频路径（h3_va 为 None，音轨内嵌于视频）
    audio_asset_id: str | None
    audio_asset_version: int | None
    review_released: bool = False      # 超容差经人工放行


@dataclass
class TimelineItem:
    shot: ShotInput
    target_duration: float
    start_time: float
    align_mode: str                    # trim / pad / none
    video_duration: float


@dataclass
class TimelineResult:
    items: list = field(default_factory=list)
    total_duration: float = 0.0
    errors: list = field(default_factory=list)   # (code, message) 合成前校验拒绝
    warnings: list = field(default_factory=list)


def probe_duration(path: str, timeout: float = 60.0) -> float:
    """ffprobe 读取媒体时长（秒）。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {out.stderr[:200]}")
    return float(json.loads(out.stdout)["format"]["duration"])


def probe_has_audio(path: str, timeout: float = 60.0) -> bool:
    """ffprobe 判断文件是否含音轨。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=timeout)
    return bool(out.stdout.strip())


def build_timeline(shots: list[ShotInput], *, tolerance: float = 0.5) -> TimelineResult:
    """构建时间轴：校验产物、计算每镜目标时长与对齐方式、累加起始时间。

    校验拒绝情形（PRD 时间轴取值表）：
    - 镜头视频缺失/失效 → output_missing
    - 按策略要求配音但音频缺失/失效 → output_missing
    - 音画差值超容差且未放行 → duration_mismatch_review_required
    """
    result = TimelineResult()
    cursor = 0.0
    for s in shots:
        # 视频必须存在
        if not s.video_path:
            result.errors.append(("output_missing", f"镜头 ep{s.episode}/seq{s.seq} 视频产物缺失"))
            continue
        # 目标时长基准
        voiced = s.audio_source in ("h3_va", "tts_fallback")
        if voiced:
            if not s.actual_duration or s.actual_duration <= 0:
                result.errors.append(("output_missing", f"镜头 ep{s.episode}/seq{s.seq} 配音时长无效"))
                continue
            if s.audio_source == "tts_fallback" and not s.audio_path:
                result.errors.append(("output_missing", f"镜头 ep{s.episode}/seq{s.seq} TTS 补救音频缺失"))
                continue
            target = float(s.actual_duration)
        else:
            target = float(s.duration)
        # 音画对齐校验（需视频实际时长；由调用方预先 probe 填入 video_duration）
        vdur = probe_duration(s.video_path)
        diff = abs(vdur - target)
        align = "none"
        if diff > tolerance:
            if s.review_released:
                result.warnings.append(
                    f"镜头 ep{s.episode}/seq{s.seq} 音画差 {diff:.2f}s 超容差 {tolerance}s，经人工放行按目标时长对齐")
                align = "trim" if vdur > target else "pad"
            else:
                result.errors.append((
                    "duration_mismatch_review_required",
                    f"镜头 ep{s.episode}/seq{s.seq} 音画差 {diff:.2f}s 超容差 {tolerance}s，须审核放行"))
                continue
        elif diff > 0.01:
            align = "trim" if vdur > target else "pad"
        item = TimelineItem(shot=s, target_duration=target, start_time=cursor,
                            align_mode=align, video_duration=vdur)
        result.items.append(item)
        cursor += target  # 逐镜累加，不四舍五入
    result.total_duration = cursor
    return result


# --- 字幕（PRD 字幕规则） ---
_PUNCT = "，。！？；、,.!?;:：\n"


def _split_dialogue(text: str) -> list[str]:
    """超 40 字按标点切分为多条；不足 40 字原样返回。"""
    text = text.strip()
    if len(text) <= SUBTITLE_SPLIT_THRESHOLD:
        return [text] if text else []
    parts, buf = [], ""
    for ch in text:
        buf += ch
        if ch in _PUNCT and len(buf) >= 8:
            parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    # 标点切不出短句时按阈值硬切
    final = []
    for p in parts:
        while len(p) > SUBTITLE_SPLIT_THRESHOLD:
            final.append(p[:SUBTITLE_SPLIT_THRESHOLD])
            p = p[SUBTITLE_SPLIT_THRESHOLD:]
        if p:
            final.append(p)
    return final or [text]


def _srt_time(seconds: float) -> str:
    """秒 → SRT 时间码（仅此处按毫秒取整）。"""
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_srt(timeline: TimelineResult) -> str:
    """按镜头归属生成 SRT：文本来自 shot.dialogue，时间为镜头起止；
    超 40 字按标点切分，时间按字符数比例分配；不越过镜头边界。"""
    entries = []
    idx = 1
    for item in timeline.items:
        text = (item.shot.dialogue or "").strip()
        if not text:
            continue
        start, end = item.start_time, item.start_time + item.target_duration
        span = end - start
        parts = _split_dialogue(text)
        total_chars = sum(len(p) for p in parts) or 1
        cursor = start
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                p_end = end  # 最后一条收编到镜头边界
            else:
                p_end = cursor + span * (len(part) / total_chars)
            if p_end - cursor < SUBTITLE_MIN_SECONDS and i < len(parts) - 1:
                p_end = min(end, cursor + SUBTITLE_MIN_SECONDS)
            entries.append(f"{idx}\n{_srt_time(cursor)} --> {_srt_time(p_end)}\n{part}\n")
            idx += 1
            cursor = p_end
    return "\n".join(entries)


# --- FFmpeg 命令构造 ---
def resolution_for(aspect_ratio: str | None) -> tuple[int, int]:
    return RESOLUTION_BY_RATIO.get(aspect_ratio or "", DEFAULT_RESOLUTION)


def shot_segment_cmd(item: TimelineItem, out_path: str, *, width: int, height: int) -> list[str]:
    """单镜片段命令：对齐到目标时长（tpad 定格 / trim 截断）、统一分辨率帧率与音轨。

    音轨规则：h3_va 保留视频内嵌音轨（无音轨时补静音）；tts_fallback 用独立音频替换；
    none 补目标时长静音——所有片段统一带音轨以便 concat。
    """
    target = item.target_duration
    s = item.shot
    inputs = ["-i", s.video_path]
    audio_index = None
    if s.audio_source == "tts_fallback" and s.audio_path:
        inputs += ["-i", s.audio_path]
        audio_index = 1
    # 视频链：pad 模式先定格补齐，再统一 trim 到目标时长，最后缩放/补边/帧率
    vchain = f"[0:v]"
    if item.align_mode == "pad":
        vchain += f"tpad=stop_mode=clone:stop={max(0.0, target - item.video_duration) + 0.5:.3f},"
    vchain += (f"trim=0:{target:.3f},setpts=PTS-STARTPTS,"
               f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={COMPOSE_FPS}[v]")
    # 音频链
    if audio_index is not None:
        achain = f"[{audio_index}:a]atrim=0:{target:.3f},asetpts=PTS-STARTPTS,apad=whole_dur={target:.3f}[a]"
        fc = f"{vchain};{achain}"
        maps = ["-map", "[v]", "-map", "[a]"]
    elif s.audio_source == "h3_va":
        # 内嵌音轨：存在则截到目标并补齐，不存在时由调用方改为静音方案
        achain = (f"[0:a]atrim=0:{target:.3f},asetpts=PTS-STARTPTS,"
                  f"apad=whole_dur={target:.3f},aresample={COMPOSE_AUDIO_RATE}[a]")
        fc = f"{vchain};{achain}"
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        fc = f"{vchain};anullsrc=r={COMPOSE_AUDIO_RATE}:cl=stereo:d={target:.3f}[a]"
        maps = ["-map", "[v]", "-map", "[a]"]
    return (["ffmpeg", "-y", *inputs, "-filter_complex", fc, *maps,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-ar", str(COMPOSE_AUDIO_RATE), "-ac", "2",
            "-movflags", "+faststart", out_path])


def concat_cmd(list_file: str, out_path: str) -> list[str]:
    """concat demuxer 拼接（各段已统一编码参数）。"""
    return ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
            "-c", "copy", out_path]


def bgm_mix_cmd(video_path: str, bgm_path: str, out_path: str, *,
                total_duration: float, volume: float = 0.3) -> list[str]:
    """BGM 混音：循环铺满、结尾 1 秒淡出、人声优先（响度归一由两遍法独立处理）。"""
    fade_start = max(0.0, total_duration - 1.0)
    fc = (f"[1:a]aloop=loop=-1:size=2147483647,atrim=0:{total_duration:.3f},"
          f"volume={volume:.3f},afade=t=out:st={fade_start:.3f}:d=1[bgm];"
          f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
          f"aresample={COMPOSE_AUDIO_RATE}[a]")
    return (["ffmpeg", "-y", "-i", video_path, "-i", bgm_path,
             "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
             "-c:v", "copy", "-c:a", "aac", "-ar", str(COMPOSE_AUDIO_RATE), out_path])


def measure_loudness(path: str, timeout: float = 300.0) -> dict | None:
    """两遍响度归一第一遍（测量）：返回 loudnorm measured 参数；静音或测量无效返回 None。

    静音/极短音频直接套用 loudnorm 会产生 NaN 导致编码失败，必须先测量再决定。
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", path, "-af",
         f"loudnorm={LOUDNESS_TARGET}:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, timeout=timeout)
    text = proc.stderr or ""
    start, end = text.rfind("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
        input_i = float(data.get("input_i", "nan"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not (input_i == input_i) or input_i <= -70:  # NaN 或静音下限
        return None
    return {
        "measured_I": data["input_i"],
        "measured_TP": data["input_tp"],
        "measured_LRA": data["input_lra"],
        "measured_thresh": data["input_thresh"],
        "offset": data.get("target_offset", "0.0"),
    }


def loudnorm_apply_cmd(video_path: str, out_path: str, measured: dict) -> list[str]:
    """两遍响度归一第二遍（应用）：按第一遍测量值线性归一（PRD M7-04：-16 LUFS / -1 dBTP）。"""
    params = ":".join(f"{k}={v}" for k, v in measured.items())
    fc = f"[0:a]loudnorm={LOUDNESS_TARGET}:{params}:linear=true,aresample={COMPOSE_AUDIO_RATE}[a]"
    return (["ffmpeg", "-y", "-i", video_path, "-filter_complex", fc,
             "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", out_path])


def burn_subtitle_cmd(video_path: str, srt_path: str, out_path: str) -> list[str]:
    """烧入字幕（libass subtitles 滤镜）。"""
    srt_escaped = srt_path.replace("\\", "/").replace(":", "\\:")
    return (["ffmpeg", "-y", "-i", video_path,
             "-vf", f"subtitles='{srt_escaped}'",
             "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-c:a", "copy", out_path])


def run_cmd(cmd: list[str], timeout: float = 1800.0) -> None:
    """执行 FFmpeg 命令，失败抛含截断 stderr 的异常（不含输入路径以外的敏感信息）。"""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-500:]
        raise RuntimeError(f"命令失败({shlex.split(cmd[0])[0]}): {tail}")
