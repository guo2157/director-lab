"""缩略图服务（D-17）：产物落库时生成缩略图，列表页只加载缩略图。

设计约定：
- 图片产物：Pillow 生成最长边 400px 的 webp 缩略图
- 视频产物：ffmpeg 抽取第 1 秒封面帧并缩放至 400px
- 音频产物：不生成缩略图（thumb_path 保持 None）
- 生成失败不阻断产物落库：记日志、thumb_path 置 None，原图原速加载
- 缩略图命名：{asset_stem}_thumb.webp，与产物同目录
"""
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("director-lab.thumbnail")

THUMB_MAX_SIZE = 400


def make_image_thumb(src: Path, dst: Path) -> bool:
    """生成图片缩略图（最长边 400px webp）。失败返回 False。"""
    try:
        from PIL import Image

        with Image.open(src) as img:
            img.thumbnail((THUMB_MAX_SIZE, THUMB_MAX_SIZE))
            img.save(dst, format="WEBP", quality=80)
        return True
    except Exception as e:
        logger.warning("图片缩略图生成失败 %s: %s", src, e)
        return False


def make_video_thumb(src: Path, dst: Path) -> bool:
    """抽取视频第 1 秒封面帧并缩放至 400px webp。失败返回 False。"""
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", "1", "-i", str(src),
                "-frames:v", "1",
                "-vf", f"scale='min(400,iw)':-2",
                "-f", "image2", "-c:v", "webp", "-update", "1", str(dst),
            ],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0 or not dst.exists():
            logger.warning("视频封面帧生成失败 %s: %s", src, proc.stderr.decode(errors="replace")[:200])
            return False
        return True
    except Exception as e:
        logger.warning("视频缩略图生成失败 %s: %s", src, e)
        return False


def make_thumb(file_path: str, asset_stem: str) -> str | None:
    """按产物类型生成缩略图，返回缩略图路径或 None（失败/不适用）。"""
    src = Path(file_path)
    if not src.exists():
        return None
    ext = src.suffix.lower()
    dst = src.parent / f"{asset_stem}_thumb.webp"
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        ok = make_image_thumb(src, dst)
    elif ext in (".mp4", ".webm", ".mov", ".mkv", ".gif"):
        ok = make_video_thumb(src, dst)
    else:
        return None
    return str(dst) if ok else None
