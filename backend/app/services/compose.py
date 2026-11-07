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
# 对白超长延长镜头上限：超出基础时长的 max(比例, 绝对秒数) 时须人工放行
DIALOGUE_EXTENSION_MAX_RATIO = 0.5
DIALOGUE_EXTENSION_MAX_SECONDS = 3.0
SUBTITLE_SPLIT_THRESHOLD = 40  # SRT 单句超 40 字切分
SUBTITLE_MIN_SECONDS = 1.0     # 最短显示时长（软约束）
SUBTITLE_MAX_LINES = 2         # 单条烧录字幕最多两行，超出按标点再切
# 烧录用中文字体候选（按优先级；缺失时用 fontconfig 里任一 CJK 字体兜底）
SUBTITLE_FONT_FALLBACKS = (
    "WenQuanYi Zen Hei", "Noto Sans CJK SC", "Source Han Sans SC",
    "Microsoft YaHei", "Noto Sans SC", "DejaVu Sans",
)
SUBTITLE_FONT_SIZE_RATIO_PORTRAIT = 0.028   # 竖屏：1280 高约 36px，每行约 18 字
SUBTITLE_FONT_SIZE_RATIO_LANDSCAPE = 0.033  # 横屏：720 高约 24px，每行约 49 字
SUBTITLE_BOTTOM_RATIO = 0.055               # 底部留白 / 成片高度
SUBTITLE_SIDE_RATIO = 0.03                  # 左右留白 / 成片宽度


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
    tts_audios: list = field(default_factory=list)  # TTS 混音输入（discard 策略）：[{segment_id, track_type, path, offset_ms, duration_ms}]
    drop_native_audio: bool = False   # discard 策略：丢弃 H3 内嵌音轨（无 TTS 片段时补静音）
    keep_native_audio: bool = False   # preserve 策略：tts_mix 时把视频内嵌音轨作为底层一并混入


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
    - 对白总时长超基础时长过多且未放行 → dialogue_extension_review_required
    """
    result = TimelineResult()
    cursor = 0.0
    for s in shots:
        # 视频必须存在
        if not s.video_path:
            result.errors.append(("output_missing", f"镜头 ep{s.episode}/seq{s.seq} 视频产物缺失"))
            continue
        # 目标时长基准
        voiced = s.audio_source in ("h3_va", "tts_fallback", "tts_mix")
        if s.audio_source == "tts_mix":
            # 对白时长为音画时长权威依据（V1 解耦方案）：超长优先延长镜头，不截断不加速
            # 基准时长：有配音镜头以视频实测时长（actual_duration）为准，缺失才回落计划时长，
            # 避免用计划时长与实测时长比对导致误判 duration_mismatch_review_required
            tts_total = sum(float(t.get("duration_ms") or 0) for t in (s.tts_audios or [])) / 1000.0
            base = float(s.actual_duration) if (s.actual_duration and s.actual_duration > 0) else float(s.duration)
            target = max(base, tts_total)
            if tts_total > base + 0.001:
                overflow = tts_total - base
                # 延长上限：超出基础时长 max(基础时长×比例, 绝对秒数) 时不得静默延长，
                # 否则单镜可能被配音无限拉长（画面定格数十秒），必须人工放行
                allowed = max(base * DIALOGUE_EXTENSION_MAX_RATIO, DIALOGUE_EXTENSION_MAX_SECONDS)
                if overflow > allowed and not s.review_released:
                    result.errors.append((
                        "dialogue_extension_review_required",
                        f"镜头 ep{s.episode}/seq{s.seq} 对白总时长 {tts_total:.2f}s 超基础时长 "
                        f"{base:.2f}s 共 {overflow:.2f}s（超过上限 {allowed:.2f}s），须审核放行"))
                    continue
                if overflow > allowed:
                    result.warnings.append(
                        f"镜头 ep{s.episode}/seq{s.seq} 对白超出上限 {overflow:.2f}s，经人工放行按对白时长延长镜头")
                else:
                    result.warnings.append(
                        f"镜头 ep{s.episode}/seq{s.seq} 对白总时长 {tts_total:.2f}s 超基础时长 {base:.2f}s，按对白时长延长镜头")
        elif voiced:
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
_PUNCT = "，。！？；、,.!?;:：…—\n"


def _split_dialogue(text: str, threshold: int = SUBTITLE_SPLIT_THRESHOLD) -> list[str]:
    """超过 threshold 字时按标点切分为多条；不足则原样返回。"""
    text = text.strip()
    if len(text) <= threshold:
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
        while len(p) > threshold:
            final.append(p[:threshold])
            p = p[threshold:]
        if p:
            final.append(p)
    return final or [text]


# --- 烧录字幕排版（ASS）：显式指定中文字体、字号与换行，避免 libass 默认真空 ---
def subtitle_layout(width: int, height: int) -> dict:
    """按成片分辨率推导字幕排版参数（字号、边距、每行字数、单条上限）。

    PlayRes 直接取成片分辨率，字号按高度比例计算：
    9:16（720×1280）约 44px，16:9（1280×720）约 24px，避免两套画幅共用字号。
    """
    ratio = (SUBTITLE_FONT_SIZE_RATIO_PORTRAIT if height >= width
             else SUBTITLE_FONT_SIZE_RATIO_LANDSCAPE)
    font_size = max(18, round(height * ratio))
    margin_v = max(12, round(height * SUBTITLE_BOTTOM_RATIO))
    margin_h = max(16, round(width * SUBTITLE_SIDE_RATIO))
    # CJK 字形近似 1em 宽，留 2% 安全余量
    chars_per_line = max(8, int((width - margin_h * 2) // (font_size * 1.02)))
    return {
        "font_size": font_size,
        "margin_v": margin_v,
        "margin_h": margin_h,
        "chars_per_line": chars_per_line,
        "cue_chars": chars_per_line * SUBTITLE_MAX_LINES,
        "outline": max(1.5, round(font_size * 0.09, 1)),
    }


def _zh_fonts() -> list[str]:
    """当前系统可用的中文字体族名（fc-list 不可用时返回空列表）。"""
    try:
        proc = subprocess.run(["fc-list", ":lang=zh", "family"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    names: list[str] = []
    for line in (proc.stdout or "").splitlines():
        for part in line.split(","):
            name = part.strip()
            if name and name not in names:
                names.append(name)
    return names


def resolve_subtitle_font(preferred: str | None = None) -> str:
    """选一个真正能渲染中文的字体：显式配置 > 候选表 > 系统任一 CJK 字体。"""
    available = _zh_fonts()
    if preferred:
        return preferred  # 显式配置优先，由 libass/fontconfig 自行处理缺失
    for name in SUBTITLE_FONT_FALLBACKS:
        if name in available:
            return name
    if available:
        return available[0]
    return "sans-serif"


def _ass_time(seconds: float) -> str:
    """秒 → ASS 时间码 H:MM:SS.cc（厘秒，向下取整避免条目重叠）。"""
    cs = max(0, int(round(seconds * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _wrap_cue(text: str, chars_per_line: int) -> list[str]:
    """把一条字幕折成多行：优先在标点后断行（至少占行宽 60%），其次按字数硬断。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chars_per_line:
        return [text]
    lines: list[str] = []
    rest = text
    min_fill = max(1, int(chars_per_line * 0.6))
    while len(rest) > chars_per_line:
        window = rest[:chars_per_line]
        cut = chars_per_line
        for i in range(len(window) - 1, min_fill - 1, -1):
            if window[i] in _PUNCT:
                cut = i + 1
                break
        lines.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        lines.append(rest)
    return lines


def _sentences(text: str) -> list[str]:
    """按句末标点切句（保留标点），逗号留在句内。"""
    sentences: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？!?\n":
            if buf.strip():
                sentences.append(buf.strip())
            buf = ""
    if buf.strip():
        sentences.append(buf.strip())
    return sentences


def _display_cues(text: str, chars_per_line: int,
                  max_lines: int = SUBTITLE_MAX_LINES) -> list[list[str]]:
    """台词 → 字幕条目（每条 1–2 行）：整句独立成条，超长句先按标点切分再折行。"""
    cues: list[list[str]] = []
    for sentence in _sentences(text):
        for part in _split_dialogue(sentence, chars_per_line * max_lines):
            lines = _wrap_cue(part, chars_per_line)
            for i in range(0, len(lines), max_lines):
                cues.append(lines[i:i + max_lines])
    return cues


def _ass_escape(lines: list[str]) -> str:
    """ASS 事件文本转义：花括号会触发覆盖标签，换行用 \\N 显式指定。"""
    safe = [ln.replace("\\", "\\\\").replace("{", "(").replace("}", ")") for ln in lines]
    return "\\N".join(safe)


def build_ass(timeline: TimelineResult, *, width: int, height: int,
              font: str | None = None) -> str:
    """生成烧录用 ASS：文本来自 shot.dialogue，时间按镜头归属与字符数比例分配。

    与 generate_srt 的时间轴规则完全一致（逐镜不跨镜、末条收编到镜头边界、
    最短 1 秒软约束），差别只在排版：显式中文字体、按分辨率定字号、
    每条最多两行并按标点折行。
    """
    layout = subtitle_layout(width, height)
    font_name = resolve_subtitle_font(font)
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",              # 已显式折行，禁止 libass 二次自动换行
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
         "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
         "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"),
        (f"Style: Default,{font_name},{layout['font_size']},&H00FFFFFF,&H000000FF,"
         f"&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{layout['outline']},0,2,"
         f"{layout['margin_h']},{layout['margin_h']},{layout['margin_v']},1"),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events: list[str] = []
    chars_per_line = layout["chars_per_line"]
    for item in timeline.items:
        text = (item.shot.dialogue or "").strip()
        if not text:
            continue
        start = item.start_time
        end = start + item.target_duration
        span = end - start
        # 整句优先：每句独立成条，最多两行，避免两句话拼在一条里
        cues = _display_cues(text, chars_per_line)
        total_chars = sum(len("".join(cue)) for cue in cues) or 1
        cursor = start
        for i, cue in enumerate(cues):
            if i == len(cues) - 1:
                p_end = end  # 最后一条收编到镜头边界
            else:
                p_end = cursor + span * (len("".join(cue)) / total_chars)
            if p_end - cursor < SUBTITLE_MIN_SECONDS and i < len(cues) - 1:
                p_end = min(end, cursor + SUBTITLE_MIN_SECONDS)
            events.append(
                f"Dialogue: 0,{_ass_time(cursor)},{_ass_time(p_end)},Default,,0,0,0,,"
                f"{_ass_escape(cue)}"
            )
            cursor = p_end
    return "\n".join(header + events) + "\n"


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
    tts_mix 丢弃内嵌音轨并按时间轴混入 TTS 对白/旁白（V1 解耦方案 discard 策略）；
    none 补目标时长静音——所有片段统一带音轨以便 concat。
    """
    target = item.target_duration
    s = item.shot
    inputs = ["-i", s.video_path]
    audio_index = None
    if s.audio_source == "tts_fallback" and s.audio_path:
        inputs += ["-i", s.audio_path]
        audio_index = 1
    elif s.audio_source == "tts_mix" and s.tts_audios:
        for ta in s.tts_audios:
            inputs += ["-i", ta["path"]]
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
    elif s.audio_source == "tts_mix" and s.tts_audios:
        # TTS 混音：各片段 adelay 定位 + amix 求和（同镜对白顺序排列不重叠，normalize=0 不衰减）
        # keep_native_audio（preserve 策略）：视频内嵌音轨作为底层一并混入，实现原生音轨 + TTS 叠加
        chains = []
        labels = []
        if s.keep_native_audio:
            chains.append(
                f"[0:a]aresample={COMPOSE_AUDIO_RATE},aformat=channel_layouts=stereo,"
                f"atrim=0:{target:.3f},asetpts=PTS-STARTPTS,apad=whole_dur={target:.3f}[nat]"
            )
            labels.append("[nat]")
        for idx, ta in enumerate(s.tts_audios):
            off = int(ta.get("offset_ms") or 0)
            chains.append(
                f"[{idx + 1}:a]aresample={COMPOSE_AUDIO_RATE},aformat=channel_layouts=stereo,"
                f"adelay={off}:all=1[a{idx}]"
            )
            labels.append(f"[a{idx}]")
        chains.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0,"
            f"atrim=0:{target:.3f},asetpts=PTS-STARTPTS,"
            f"apad=whole_dur={target:.3f},aresample={COMPOSE_AUDIO_RATE}[a]"
        )
        fc = vchain + ";" + ";".join(chains)
        maps = ["-map", "[v]", "-map", "[a]"]
    elif s.audio_source == "h3_va" and not s.drop_native_audio:
        # 内嵌音轨：存在则截到目标并补齐，不存在时由调用方改为静音方案；
        # discard 策略（drop_native_audio=True）丢弃内嵌音轨，落入下方静音分支
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


def burn_subtitle_cmd(video_path: str, ass_path: str, out_path: str) -> list[str]:
    """烧入字幕（libass ass 滤镜）：排版参数全部在 ASS 里，命令侧不再带 force_style。"""
    ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")
    return (["ffmpeg", "-y", "-i", video_path,
             "-vf", f"ass='{ass_escaped}'",
             "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-c:a", "copy", out_path])


def run_cmd(cmd: list[str], timeout: float = 1800.0) -> None:
    """执行 FFmpeg 命令，失败抛含截断 stderr 的异常（不含输入路径以外的敏感信息）。"""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-500:]
        raise RuntimeError(f"命令失败({shlex.split(cmd[0])[0]}): {tail}")
