"""跨集末帧提取服务：从合成视频中提取最后一个有效视频帧。

设计约定（对应跨集连续性改造第四节）：
- 使用 FFmpeg/FFprobe 确认视频可解码、获取实际时长和帧率
- 从最终成片中提取最后一个有效视频帧
- 检测黑屏/损坏帧并回退到最近的有效画面
- 保存为 PNG 到项目媒体目录
- 计算文件 SHA256 校验值用于版本追踪
"""
import hashlib
import logging
import os
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class FrameExtractionResult:
    """末帧提取结果。"""
    ok: bool = False
    frame_path: str | None = None
    frame_hash: str | None = None
    timestamp: float | None = None
    video_duration: float | None = None
    video_fps: float | None = None
    width: int | None = None
    height: int | None = None
    error: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None


def probe_video(path: str) -> dict:
    """用 ffprobe 获取视频流信息：时长、帧率、宽高。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration",
        "-show_entries", "format=duration",
        "-of", "json", path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {proc.stderr[:300]}")
    import json
    info = json.loads(proc.stdout)
    stream = (info.get("streams") or [{}])[0]
    fmt = info.get("format", {})
    duration = float(fmt.get("duration") or stream.get("duration") or 0)
    fps_str = stream.get("r_frame_rate", "0/1")
    num, _, den = fps_str.partition("/")
    fps = float(num) / float(den) if float(den) != 0 else 0.0
    return {
        "duration": duration,
        "fps": fps,
        "width": int(stream.get("width", 0)),
        "height": int(stream.get("height", 0)),
        "has_video": bool(stream.get("width")),
    }


def _is_black_frame(path: str) -> bool:
    """检测图片是否为黑屏。"""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", path, "-vf", "blackdetect=d=0:pix_th=0.01",
        "-an", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return "black_duration" in (proc.stderr or "")


def _compute_sha256(path: str) -> str:
    """计算文件 SHA256 校验值。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_frame_at(video_path: str, timestamp: float, output_path: str) -> bool:
    """在指定时间戳提取一帧。返回是否成功。"""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(max(0, timestamp)),
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "2",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=30)
    return proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0


def extract_last_frame(
    video_path: str,
    output_path: str,
    min_width: int = 256,
    min_height: int = 256,
) -> FrameExtractionResult:
    """从视频提取最后一个有效帧。

    流程：
    1. ffprobe 确认视频可解码并获取时长/帧率
    2. 尝试提取最后一帧（duration - 1/fps）
    3. 检测黑屏，若黑屏则逐步回退寻找最近的有效画面
    4. 验证图片尺寸和可解码性
    5. 计算 SHA256 校验值
    """
    result = FrameExtractionResult()

    if not os.path.exists(video_path):
        result.error = f"视频文件不存在: {video_path}"
        return result

    try:
        info = probe_video(video_path)
    except Exception as e:
        result.error = f"视频探测失败: {e}"
        return result

    if not info["has_video"]:
        result.error = "视频文件无视频流"
        return result

    result.video_duration = info["duration"]
    result.video_fps = info["fps"]
    result.width = info["width"]
    result.height = info["height"]

    if info["duration"] <= 0:
        result.error = "视频时长为零或无效"
        return result

    fps = info["fps"] if info["fps"] > 0 else 24.0
    last_ts = max(0, info["duration"] - 1.0 / fps)

    max_fallback_steps = 10
    fallback_interval = 0.5

    for attempt in range(max_fallback_steps + 1):
        ts = max(0, last_ts - attempt * fallback_interval)
        if not _extract_frame_at(video_path, ts, output_path):
            continue

        if _is_black_frame(output_path):
            if attempt == 0:
                result.fallback_reason = "末帧检测为黑屏"
            logger.warning("帧在 ts=%.3f 处为黑屏，尝试回退", ts)
            continue

        try:
            from PIL import Image
            img = Image.open(output_path)
            w, h = img.size
            if w < min_width or h < min_height:
                logger.warning("帧尺寸 %dx%d 小于最小要求 %dx%d", w, h, min_width, min_height)
                continue
            img.close()
        except ImportError:
            pass
        except Exception as e:
            logger.warning("帧图片验证异常: %s", e)
            continue

        result.ok = True
        result.frame_path = output_path
        result.timestamp = ts
        result.fallback_used = attempt > 0
        result.frame_hash = _compute_sha256(output_path)
        return result

    result.error = f"无法提取有效末帧（已尝试 {max_fallback_steps + 1} 次）"
    return result


def validate_frame_image(path: str, min_width: int = 256, min_height: int = 256) -> tuple[bool, str | None]:
    """验证帧图片是否可用：文件存在、非空、可解码、尺寸符合要求。"""
    if not os.path.exists(path):
        return False, "文件不存在"
    if os.path.getsize(path) == 0:
        return False, "文件为空"
    try:
        from PIL import Image
        img = Image.open(path)
        w, h = img.size
        img.close()
        if w < min_width or h < min_height:
            return False, f"尺寸 {w}x{h} 小于最小要求 {min_width}x{min_height}"
        return True, None
    except ImportError:
        return True, None
    except Exception as e:
        return False, f"图片解码失败: {e}"
