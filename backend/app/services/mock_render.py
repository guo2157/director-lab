"""Mock 渲染服务：render_mock 开启时替代真实 ComfyUI 渲染，生成可辨识的占位产物。

设计约定（DECISIONS.md D-11）：
- 拦截点在 worker 执行器内部（上传/提交/轮询/下载整体跳过），任务照常走
  queued → running → succeeded 状态机，产物照常入库，SSE 与前端零改动。
- 占位产物按输出名区分类型：cover → PNG 图（Pillow 绘制任务标识），
  clip → MP4 测试视频（ffmpeg testsrc），voice/bgm/sfx → WAV 测试音频（ffmpeg 正弦波）。
- 产物内容必须可辨识（含 job id 与输出名），避免与真实产物混淆。
- ffmpeg 不可用时视频/音频生成直接报错，与真实模式行为一致，不静默伪造。
"""
import io
import subprocess

# 输出名 → mock 产物类型（与 executor 的 asset_type_map 对齐）
_MOCK_KIND = {
    "cover": "image",
    "clip": "video",
    "voice": "audio",
    "bgm": "audio",
    "sfx": "audio",
}


def _mock_image(job_id: str, out_name: str, width: int = 512, height: int = 288) -> bytes:
    """生成带任务标识的占位 PNG。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), color=(36, 41, 51))
    draw = ImageDraw.Draw(img)
    # 边框与角标，肉眼可辨识为 mock 产物
    draw.rectangle([2, 2, width - 3, height - 3], outline=(255, 170, 0), width=4)
    draw.text((16, 16), "[MOCK]", fill=(255, 170, 0))
    draw.text((16, 48), f"job: {job_id[-8:]}", fill=(230, 230, 230))
    draw.text((16, 68), f"out: {out_name}", fill=(230, 230, 230))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _run_ffmpeg(args: list[str], fmt: str) -> bytes:
    """执行 ffmpeg 生成到 stdout 管道，返回文件内容。"""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, "-f", fmt, "pipe:1"]
    proc = subprocess.run(cmd, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 生成 mock 产物失败: {proc.stderr.decode(errors='replace')[:300]}")
    return proc.stdout


def _mock_video(job_id: str, out_name: str) -> bytes:
    """生成 1 秒 testsrc 测试视频（MP4）。"""
    label = f"job_{job_id[-8:]}_{out_name}"
    vf = f"drawtext=text='[MOCK] {label}':x=8:y=8:fontsize=20:fontcolor=orange"
    return _run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=12:duration=1",
        "-vf", vf,
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264",
        # MP4 写入管道必须分片，否则因不可 seek 失败
        "-movflags", "frag_keyframe+empty_moov",
    ], "mp4")


def _mock_audio(job_id: str, out_name: str) -> bytes:
    """生成 1 秒 440Hz 正弦波测试音频（WAV）。"""
    return _run_ffmpeg([
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-ar", "16000", "-ac", "1",
        "-c:a", "pcm_s16le",
    ], "wav")


def mock_outputs(output_map: dict[str, str], job_id: str) -> list[dict]:
    """按输出名映射生成 mock 产物列表，结构与 executor 的 downloaded 记录一致。

    返回：[{"out_name": ..., "item": {"filename": ..., "subfolder": "", "type": "output"}, "content": bytes}]
    """
    records = []
    for out_name in output_map:
        kind = _MOCK_KIND.get(out_name, "image")
        if kind == "image":
            content, ext = _mock_image(job_id, out_name), "png"
        elif kind == "video":
            content, ext = _mock_video(job_id, out_name), "mp4"
        else:
            content, ext = _mock_audio(job_id, out_name), "wav"
        records.append({
            "out_name": out_name,
            "item": {
                "filename": f"mock_{job_id[-8:]}_{out_name}.{ext}",
                "subfolder": "",
                "type": "output",
            },
            "content": content,
        })
    return records
