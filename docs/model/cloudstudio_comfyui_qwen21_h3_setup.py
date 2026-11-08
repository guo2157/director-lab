#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""comfyui_qwen21_h3_setup.py

在 Jupyter Notebook 或终端里一键完成：
  1. 环境检查（Python / GPU / 磁盘）
  2. 克隆 ComfyUI 并安装依赖
  3. 下载 wf_video_h3_ref2va.json 需要的 4 个 MiniMax H3 模型、
     qwen-image-2-1-turbo-image-edit 需要的 3 个 Qwen-Image-2.1 模型，
     以及 Qwen3-TTS 1.7B CustomVoice 语音模型（整仓 snapshot）
  4. 校验文件大小、启动 ComfyUI（可选 cloudflared 公网隧道）

Notebook 用法::

    import comfyui_qwen21_h3_setup as cs
    cs.print_plan(["h3", "qwen", "tts"])
    cs.check_env()
    cs.ensure_comfyui()
    cs.install_requirements()
    cs.ensure_qwen3_tts_node()      # 装 Qwen3-TTS 自定义节点，缺了会报 missing_node_type
    cs.download_models(["h3", "qwen", "tts"])
    cs.verify_models(["h3", "qwen", "tts"])
    cs.launch(wait=True)

命令行用法::

    python comfyui_qwen21_h3_setup.py plan
    python comfyui_qwen21_h3_setup.py all --groups h3 qwen
    python comfyui_qwen21_h3_setup.py download --groups h3
    python comfyui_qwen21_h3_setup.py launch --port 8188
    python comfyui_qwen21_h3_setup.py stop

模型来源（仓库内的目录名与 ComfyUI 的 models/ 子目录完全一致，
下载后按原路径落到 ComfyUI/models 下，不需要再手动移动文件）：
  * Comfy-Org/MiniMax-H3        （官方 repack，含 H3 Ref2VA / Qwen3-VL-32B / 视频VAE / 音频VAE / 4step LoRA）
  * Comfy-Org/Qwen-Image-2.1    （官方 repack，含 Turbo 主模型 / Qwen3-VL-8B / VAE）
  * taurusduan/MiniMax-H3       （H3 镜像，官方仓库不可达时自动回退）
  * Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice （TTS 整仓，保留 HF 目录结构，
    下载到 ComfyUI/models/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-CustomVoice）
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ModelFile",
    "salvage_incomplete",
    "MODELS",
    "check_workflow",
    "ensure_workflow_models",
    "human",
    "print_plan",
    "check_env",
    "ensure_comfyui",
    "install_requirements",
    "ensure_hf_hub",
    "ensure_qwen3_tts_node",
    "check_tts_nodes",
    "setup_customvoice",
    "restore_pip_snapshot",
    "set_hf_token",
    "hf_whoami",
    "ask_hf_token",
    "download_models",
    "verify_models",
    "set_work_dir",
    "free_space",
    "in_notebook",
    "cli_args",
    "colab_url",
    "launch",
    "wait_ready",
    "stop",
    "tail_log",
    "public_url",
    "start_tunnel",
    "stop_tunnel",
    "verify_public",
    "setup_all",
    "status",
]

# ---------------------------------------------------------------------------
# 配置区（都可以用同名环境变量覆盖）
# ---------------------------------------------------------------------------

COMFY_DIRNAME = os.environ.get("COMFY_DIRNAME", "ComfyUI")
__version__ = "2026.11.8.3"


def _pick_work_dir() -> Path:
    """选工作目录。

    优先 COMFY_WORK_DIR；在 Kaggle / Colab 上自动挑剩余空间最大的临时盘，
    因为 /kaggle/working 只有 20 GB 输出配额、放不下模型。
    """
    env = os.environ.get("COMFY_WORK_DIR")
    if env:
        return Path(env).expanduser().resolve()
    if Path("/kaggle/working").is_dir() or Path("/content").is_dir():
        best = None
        best_free = -1
        for candidate in ("/kaggle/temp", "/content", "/tmp", os.getcwd()):
            path = Path(candidate)
            try:
                if not path.is_dir():
                    continue
                free = shutil.disk_usage(str(path)).free
            except OSError:
                continue
            if free > best_free:
                best, best_free = path, free
        if best is not None:
            print(
                "[环境] 检测到 Notebook 临时盘，工作目录 -> "
                + str(best)
                + "（剩余 "
                + str(round(best_free / float(1024 ** 3), 1))
                + " GiB）",
                flush=True,
            )
            return best.resolve()
    return Path.cwd().resolve()


WORK_DIR = _pick_work_dir()
COMFY_DIR = WORK_DIR / COMFY_DIRNAME
MODELS_DIR = COMFY_DIR / "models"

PORT = int(os.environ.get("COMFY_PORT", "8188"))
COMFY_REPO = os.environ.get("COMFY_REPO", "")
COMFY_REF = os.environ.get("COMFY_REF", "master")
UPDATE_COMFY = os.environ.get("COMFY_UPDATE", "0") == "1"

CLONE_URLS = (
    "https://github.com/comfyanonymous/ComfyUI.git",
    "https://ghfast.top/https://github.com/comfyanonymous/ComfyUI.git",
    "https://gh-proxy.com/https://github.com/comfyanonymous/ComfyUI.git",
)

# auto: 先试官方 huggingface.co，不通再用镜像；official / mirror / 直接写 URL 也可以
HF_ENDPOINT_MODE = os.environ.get("HF_ENDPOINT_MODE", "auto")
HF_MIRROR = os.environ.get("HF_MIRROR", "https://hf-mirror.com")
HF_TOKEN = os.environ.get("HF_TOKEN", "")
HF_TOKEN_URL = "https://huggingface.co/settings/tokens"
# 1: 下载前若没有 token，停下来提示用户输入；0: 只在日志里提示
ASK_HF_TOKEN = os.environ.get("COMFY_ASK_HF_TOKEN", "1") != "0"
HF_MAX_WORKERS = int(os.environ.get("HF_MAX_WORKERS", "3"))
# 缓存目录连续这么多秒没有新增字节就判定为卡死（0 = 关闭看门狗）
HF_STALL_SECONDS = int(os.environ.get("HF_STALL_SECONDS", "300"))
HF_RETRIES = int(os.environ.get("HF_RETRIES", "3"))

# torch 的 CUDA 轮子索引，例如 https://download.pytorch.org/whl/cu128
TORCH_INDEX_URL = os.environ.get("COMFY_TORCH_INDEX_URL", "")

# auto: 启动时自动尝试 cloudflared 隧道（失败只提示，不影响 ComfyUI）
# none: 完全不开；也可以写 cloudflared
TUNNEL = os.environ.get("COMFY_TUNNEL", "auto")
# auto: 装了 sageattention 就开；0: 强制关闭
COMFY_SAGE = os.environ.get("COMFY_SAGE", "auto")
PREVIEW_METHOD = os.environ.get("COMFY_PREVIEW", "auto")

DOWNLOAD_GROUPS = ["h3", "qwen", "tts"]

GROUPS = {
    "h3": "MiniMax H3 / Ref2VA（视频+音频）",
    "qwen": "Qwen-Image-2.1 Turbo（图像编辑）",
    "tts": "Qwen3-TTS 1.7B CustomVoice（独立语音生成）",
}

TTS_REPO = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
TTS_MODEL_DIR = MODELS_DIR / "Qwen3-TTS" / "Qwen3-TTS-12Hz-1.7B-CustomVoice"
TTS_MIN_EXPECTED_BYTES = 3_500_000_000  # 仅用于磁盘预检查；实际按仓库文件下载，不按此值校验

# Qwen3-TTS 自定义节点（DarioFT/ComfyUI-Qwen3-TTS）
# ComfyUI 自带的 custom_nodes 里只有示例脚本，这个包得自己装；缺了它工作流里的
# Qwen3Loader / Qwen3CustomVoice 会在 /prompt 校验时直接 400 missing_node_type。
TTS_NODE_DIRNAME = os.environ.get("COMFY_TTS_NODE_DIR", "ComfyUI-Qwen3-TTS")
TTS_NODE_REPO = os.environ.get("COMFY_TTS_NODE_REPO", "https://github.com/DarioFT/ComfyUI-Qwen3-TTS.git")
TTS_NODE_MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/")
TTS_NODE_CLASSES = ("Qwen3Loader", "Qwen3CustomVoice")
TTS_NODE_SKIP_DEPS = os.environ.get("COMFY_TTS_NODE_SKIP_DEPS", "0") == "1"

LOG_PATH = WORK_DIR / "comfyui.log"
TUNNEL_LOG = WORK_DIR / "cloudflared.log"

_PROC = None
_LOG_HANDLE = None
_TUNNEL_PROC = None
_TUNNEL_URL = None
_PROC_ARGS = None


def set_work_dir(path):
    """切换工作目录（import 之后也能改），返回新路径。

    Kaggle 上常见用法::

        cs.set_work_dir('/kaggle/temp')
    """
    global WORK_DIR, COMFY_DIR, MODELS_DIR, TTS_MODEL_DIR, LOG_PATH, TUNNEL_LOG
    WORK_DIR = Path(path).expanduser().resolve()
    COMFY_DIR = WORK_DIR / COMFY_DIRNAME
    MODELS_DIR = COMFY_DIR / "models"
    TTS_MODEL_DIR = MODELS_DIR / "Qwen3-TTS" / "Qwen3-TTS-12Hz-1.7B-CustomVoice"
    LOG_PATH = WORK_DIR / "comfyui.log"
    TUNNEL_LOG = WORK_DIR / "cloudflared.log"
    log("[环境] 工作目录 -> " + str(WORK_DIR))
    return WORK_DIR


def free_space(path=None) -> int:
    """返回工作目录（或指定路径）所在磁盘的剩余字节数。"""
    return _disk_free(path if path is not None else WORK_DIR)


# ---------------------------------------------------------------------------
# 模型清单：path 就是仓库内路径，也是 ComfyUI/models 下的相对路径
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelFile:
    group: str
    repo: str
    path: str
    size: int
    note: str = ""


MODELS = (
    ModelFile(
        "h3",
        "Comfy-Org/MiniMax-H3",
        "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        20_970_379_616,
        "H3 Ref2VA 扩散模型 INT8(pruned)",
    ),
    ModelFile(
        "h3",
        "Comfy-Org/MiniMax-H3",
        "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
        27_141_342_152,
        "Qwen3-VL-32B 文本编码器 INT8",
    ),
    ModelFile(
        "h3",
        "Comfy-Org/MiniMax-H3",
        "vae/minimax_h3_audio_vae_fp32.safetensors",
        605_254_808,
        "H3 音频 VAE FP32",
    ),
    ModelFile(
        "h3",
        "Comfy-Org/MiniMax-H3",
        "vae/minimax_h3_video_vae_int8_convrot.safetensors",
        2_811_065_184,
        "H3 视频 VAE INT8(convrot)",
    ),
    ModelFile(
        "h3",
        "Comfy-Org/MiniMax-H3",
        "loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
        1_956_193_000,
        "H3 Ref2V Turbo 4-step LoRA",
    ),
    ModelFile(
        "qwen",
        "Comfy-Org/Qwen-Image-2.1",
        "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors",
        7_256_787_829,
        "Qwen-Image-2.1 Turbo 扩散模型 INT8",
    ),
    ModelFile(
        "qwen",
        "Comfy-Org/Qwen-Image-2.1",
        "text_encoders/qwen3vl_8b_int8_convrot.safetensors",
        9_350_798_360,
        "Qwen3-VL-8B 文本编码器 INT8",
    ),
    ModelFile(
        "qwen",
        "Comfy-Org/Qwen-Image-2.1",
        "vae/qwen_image_2.1_vae_bf16.safetensors",
        675_509_688,
        "Qwen-Image-2.1 VAE BF16",
    ),
)

FALLBACK_REPOS = {
    "Comfy-Org/MiniMax-H3": ("taurusduan/MiniMax-H3",),
    "Comfy-Org/Qwen-Image-2.1": (),
}


def _workflow_refs(path):
    """取工作流 JSON 里写死的 .safetensors 引用；文件不存在返回 None。"""
    wf = Path(path)
    if not wf.exists():
        return None
    text = wf.read_text(encoding="utf-8", errors="replace")
    return sorted(set(re.findall(r'"([^"\s]+\.safetensors)"', text)))


def _models_index() -> dict:
    """models 目录下现有文件（按文件名索引）。"""
    actual = {}
    if MODELS_DIR.exists():
        for item in MODELS_DIR.rglob("*.safetensors"):
            actual.setdefault(item.name, item)
    return actual


def check_workflow(path) -> bool:
    """核对工作流 JSON 里写死的模型文件名是否都在 ComfyUI/models 下。

    提交失败最常见的根因就是这里：工作流里写的文件名和实际下载的不一致
    （例如工作流要 minimax_h3_video_vae_fp16.safetensors，实际只有 int8 版本），
    ComfyUI 会在校验阶段以 value_not_in_list 直接拒收。
    """
    refs = _workflow_refs(path)
    if refs is None:
        log("[对齐] 找不到工作流文件: " + str(path))
        return False
    if not refs:
        log("[对齐] 工作流里没有 .safetensors 引用")
        return True

    actual = _models_index()
    missing = []
    for ref in refs:
        name = ref.replace("\\", "/").split("/")[-1]
        if name in actual:
            log("[对齐] OK    " + ref)
        else:
            missing.append(ref)
            log("[对齐] 缺失  " + ref)

    if missing:
        log("")
        log("[对齐] 共缺 %d 个文件。当前 models 下相近的可用文件：" % len(missing))
        for name in sorted(actual):
            log("        " + name)
        log("[对齐] 修复方式二选一：改工作流里的文件名，或补下载对应模型。")
        return False
    log("[对齐] 全部存在，工作流可以正常提交")
    return True


WORKFLOW_MODEL_REPOS = (
    "Comfy-Org/MiniMax-H3",
    "Comfy-Org/Qwen-Image-2.1",
)


def ensure_workflow_models(workflow, repos=None, workers=None) -> bool:
    """补齐工作流里引用、但本地缺失的模型。

    做法是拿缺失的文件名去已知仓库里找同名文件，找到就只下那几个文件
    （按仓库内的原目录落到 models/vae、models/loras 等），不用整仓拉取。
    """
    refs = _workflow_refs(workflow)
    if refs is None:
        log("[补齐] 找不到工作流文件: " + str(workflow))
        return False
    if not refs:
        log("[补齐] 工作流里没有 .safetensors 引用")
        return True

    actual = _models_index()
    missing = [r for r in refs if r.replace("\\", "/").split("/")[-1] not in actual]
    if not missing:
        log("[补齐] 模型齐全，无需下载")
        return True

    log("[补齐] 缺 " + str(len(missing)) + " 个模型，开始在仓库里查找同名文件")
    candidates = list(repos or WORKFLOW_MODEL_REPOS)
    plan = {}
    unknown = []
    for ref in missing:
        base = ref.replace("\\", "/").split("/")[-1]
        hit = None
        for repo in candidates:
            try:
                tree = _repo_tree(repo)
            except Exception as exc:
                log("[补齐] 读取仓库失败 " + repo + ": " + str(exc))
                continue
            for path in tree:
                if path.replace("\\", "/").split("/")[-1] == base:
                    hit = (repo, path)
                    break
            if hit:
                break
        if hit:
            repo, path = hit
            plan.setdefault(repo, []).append(path)
            log("[补齐] 命中 " + base + " -> " + repo + "/" + path)
        else:
            unknown.append(base)
            log("[补齐] 未找到 " + base)

    for repo, patterns in plan.items():
        _download_repo(repo, patterns, workers or HF_MAX_WORKERS)

    ok = check_workflow(workflow)
    if unknown:
        log("")
        log("[补齐] 这些文件不在已搜索的仓库里，需要手动指定来源：")
        for name in unknown:
            log("        " + name)
        log("[补齐] 用法: ensure_workflow_models(wf, repos=['你的/仓库名'])")
    return ok and not unknown


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def log(message: str = "") -> None:
    print(message, flush=True)


def human(num: float) -> str:
    """字节数转可读字符串（1024 进制）。"""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            if unit == "B":
                return str(int(value)) + " B"
            return str(round(value, 2)) + " " + unit
        value /= 1024.0
    return str(num) + " B"


def _run(cmd, cwd=None, check=True, capture=False):
    log("$ " + " ".join(str(part) for part in cmd))
    return subprocess.run(
        [str(part) for part in cmd],
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        capture_output=capture,
    )


def _disk_free(path) -> int:
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        return shutil.disk_usage(str(target)).free
    except OSError:
        return 0


def _http_ok(url: str, timeout: float = 6.0) -> bool:
    request = urllib.request.Request(url, headers={"User-Agent": "comfyui-setup/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 400
    except Exception:
        return False


def _gpu_list():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _torch_info() -> str:
    try:
        import torch
    except Exception:
        return "未安装（安装 ComfyUI 依赖时会自动安装）"
    text = "torch " + str(torch.__version__)
    try:
        if torch.cuda.is_available():
            text += " | CUDA " + str(torch.version.cuda) + " | " + str(torch.cuda.get_device_name(0))
        else:
            text += " | CUDA 不可用"
    except Exception as exc:
        text += " | 查询失败: " + str(exc)
    return text


# ---------------------------------------------------------------------------
# 0. 计划
# ---------------------------------------------------------------------------


def print_plan(groups=None) -> None:
    selected = [g for g in (groups or DOWNLOAD_GROUPS) if g in GROUPS]
    log("=" * 74)
    log("ComfyUI 部署计划")
    log("=" * 74)
    log("工作目录 : " + str(WORK_DIR))
    log("ComfyUI  : " + str(COMFY_DIR))
    log("模型目录 : " + str(MODELS_DIR))
    log("下载组   : " + (", ".join(selected) if selected else "(空)"))
    log("")
    for group in selected:
        if group == "tts":
            log("[tts] " + GROUPS["tts"] + " —— 整仓 snapshot（预估 " + human(TTS_MIN_EXPECTED_BYTES) + "）")
            log("    " + TTS_REPO + " -> models/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-CustomVoice")
            log("    自定义节点: " + TTS_NODE_REPO + "（cs.ensure_qwen3_tts_node()）")
            continue
        items = [m for m in MODELS if m.group == group]
        total = sum(m.size for m in items)
        log("[" + group + "] " + GROUPS[group] + " —— " + str(len(items)) + " 个文件, " + human(total))
        for item in items:
            log("    " + item.path)
            log("        " + human(item.size) + "  " + item.note)
    total_all = sum(m.size for m in MODELS if m.group in selected)
    if "tts" in selected:
        total_all += TTS_MIN_EXPECTED_BYTES
    log("")
    log("合计: " + human(total_all) + "  (" + str(round(total_all / 1e9, 1)) + " GB"
        + ("，TTS 为预估值" if "tts" in selected else "") + ")")
    log("=" * 74)


# ---------------------------------------------------------------------------
# 1. 环境检查
# ---------------------------------------------------------------------------


def check_env() -> None:
    log("=" * 74)
    log("环境检查")
    log("=" * 74)
    log("Python   : " + platform.python_version() + "  (" + platform.system() + " " + platform.machine() + ")")
    log("脚本版本 : " + __version__)
    log("解释器   : " + sys.executable)
    log("工作目录 : " + str(WORK_DIR))
    log("模型目录 : " + str(MODELS_DIR))
    if sys.version_info < (3, 9):
        log("[警告] Python 版本偏低，建议 3.10+")
    free = _disk_free(WORK_DIR)
    need = sum(m.size for m in MODELS if m.group in DOWNLOAD_GROUPS)
    log("磁盘剩余 : " + human(free) + " / 本次需要 " + human(need))
    if free and free < need * 1.05:
        log("[警告] 磁盘可能不够：建议只下其中一组，或把工作目录换到更大的盘")
    if str(WORK_DIR).startswith("/kaggle/working"):
        log("[提示] /kaggle/working 只有 20 GB 输出配额；模型请放 /kaggle/temp")
        log("[提示] 执行 cs.set_work_dir('/kaggle/temp') 后重新下载即可")
    gpus = _gpu_list()
    if gpus:
        for index, gpu in enumerate(gpus):
            log("GPU " + str(index) + "     : " + gpu)
    else:
        log("GPU      : 未检测到 nvidia-smi")
    log("PyTorch  : " + _torch_info())
    if HF_TOKEN:
        log("HF Token : 已设置（尾号 " + HF_TOKEN[-4:] + "）")
    else:
        log("HF Token : 未设置，下载时会提示输入；获取地址 " + HF_TOKEN_URL)
    log("=" * 74)


# ---------------------------------------------------------------------------
# 2. ComfyUI 代码 + 依赖
# ---------------------------------------------------------------------------


def ensure_comfyui():
    """克隆（或复用）ComfyUI，返回 ComfyUI 目录。"""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if (COMFY_DIR / "main.py").exists():
        log("[ComfyUI] 已存在: " + str(COMFY_DIR))
        if UPDATE_COMFY:
            _run(["git", "-C", str(COMFY_DIR), "pull", "--ff-only"], check=False)
        _check_node_support()
        return COMFY_DIR

    if shutil.which("git") is None:
        raise RuntimeError("找不到 git，请先安装：apt-get update && apt-get install -y git")

    urls = [COMFY_REPO] if COMFY_REPO else list(CLONE_URLS)
    last_error = None
    for url in urls:
        log("[ComfyUI] 克隆 " + url)
        try:
            _run(["git", "clone", "--depth", "1", url, str(COMFY_DIR)], check=True)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            log("[ComfyUI] 克隆失败: " + str(exc))
            if COMFY_DIR.exists():
                shutil.rmtree(str(COMFY_DIR), ignore_errors=True)
    if last_error is not None:
        raise RuntimeError("ComfyUI 克隆失败，请检查网络或设置 COMFY_REPO 环境变量")

    if COMFY_REF and COMFY_REF not in ("master", "main"):
        log("[ComfyUI] 切换到 " + COMFY_REF)
        _run(["git", "-C", str(COMFY_DIR), "fetch", "--depth", "1", "origin", COMFY_REF], check=False)
        _run(["git", "-C", str(COMFY_DIR), "checkout", COMFY_REF], check=False)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    _check_node_support()
    return COMFY_DIR


def _contains(symbol: str) -> bool:
    for pattern in ("comfy_extras/*.py", "comfy/*.py", "comfy/*/*.py"):
        for path in COMFY_DIR.glob(pattern):
            try:
                if symbol in path.read_text(encoding="utf-8", errors="ignore"):
                    return True
            except OSError:
                continue
    return False


def _check_node_support() -> None:
    """确认当前 ComfyUI 版本带 MiniMax H3 / Qwen-Image-2.1 原生节点。"""
    missing = []
    if not _contains("MiniMaxH3ReferenceToVideo"):
        missing.append("MiniMaxH3ReferenceToVideo（H3 节点）")
    if not _contains("TextEncodeQwenImage21"):
        missing.append("TextEncodeQwenImage21（Qwen-Image-2.1 节点）")
    if missing:
        log("[警告] 当前 ComfyUI 版本缺少: " + "；".join(missing))
        log("[警告] 请升级到最新 master：cd ComfyUI && git pull")


def _installed_hub_version():
    """读已安装的 huggingface_hub 版本（走 metadata，pip 降级后也能读到新值）。"""
    try:
        from importlib import metadata

        return metadata.version("huggingface_hub")
    except Exception:
        pass
    try:
        import huggingface_hub

        return str(getattr(huggingface_hub, "__version__", "unknown"))
    except Exception:
        return None


def _hub_major(version) -> int:
    try:
        return int(str(version).split(".")[0])
    except Exception:
        return 1


def _transformers_probe():
    """在子进程里尝试 import transformers（不把大模型式依赖塞进当前进程）。"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import transformers; print(transformers.__version__)"],
            capture_output=True,
            text=True,
            timeout=240,
        )
    except Exception as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, (result.stdout or "").strip()
    lines = [line for line in (result.stderr or "").strip().splitlines() if line.strip()]
    return False, (lines[-1] if lines else "unknown error")


def set_hf_token(token=None) -> str:
    """设置 HuggingFace Token（可选）。

    Notebook 里直接 ``cs.set_hf_token()`` 会弹隐藏输入框，输入不回显；
    也可以 ``cs.set_hf_token('hf_xxx')`` 或 ``os.environ['HF_TOKEN']='hf_xxx'``。

    不登录也能下载，只是 Hub 会提示
    ``You are sending unauthenticated requests ... set a HF_TOKEN`` 并限流。
    """
    global HF_TOKEN
    if token is None:
        if HF_TOKEN:
            log("[HF] 已有 token（尾号 " + HF_TOKEN[-4:] + "），跳过输入")
            return HF_TOKEN
        try:
            from getpass import getpass

            token = getpass("粘贴 HuggingFace Token（输入不显示，直接回车跳过）: ")
        except Exception as exc:
            log("[HF] 无法读取输入: " + str(exc) + "；请改用 cs.set_hf_token('hf_xxx')")
            return ""

    token = str(token or "").strip()
    if not token:
        if HF_TOKEN:
            log("[HF] 输入为空，保持已有 token（尾号 " + HF_TOKEN[-4:] + "）")
            return HF_TOKEN
        log("[HF] 未设置 token，继续匿名下载（会被限速，但可用）")
        return ""

    HF_TOKEN = token
    os.environ["HF_TOKEN"] = token
    try:
        from huggingface_hub import login

        login(token=token, add_to_git_credential=False)
        log("[HF] 已登录，token 已缓存到本地（下次运行自动复用）")
    except Exception as exc:
        log("[HF] login 调用失败，仍在本次进程内使用: " + str(exc))
    hf_whoami()
    return token


def hf_whoami():
    """验证当前 token 是否生效，返回 whoami 结果或 None。"""
    token = HF_TOKEN or os.environ.get("HF_TOKEN", "")
    if not token:
        log("[HF] 当前未设置 HF_TOKEN（匿名模式，会限流）")
        return None
    try:
        from huggingface_hub import HfApi

        info = HfApi().whoami(token=token)
        name = info.get("name") if isinstance(info, dict) else str(info)
        log("[HF] 已认证: " + str(name))
        return info
    except Exception as exc:
        log("[HF] token 校验失败（可能是网络或权限问题）: " + str(exc))
        return None


def ask_hf_token(force: bool = False) -> str:
    """没有 token 时提示用户输入，并打印获取地址。

    Notebook 里会停下来等输入（直接回车 = 跳过，继续匿名下载）。
    环境变量 COMFY_ASK_HF_TOKEN=0 可关闭询问。
    """
    if HF_TOKEN:
        return HF_TOKEN

    log("")
    log("-" * 74)
    log("[HF] 未设置 HF_TOKEN：HuggingFace 会对匿名请求限流（下载更慢）")
    log("[HF] Token 获取地址: " + HF_TOKEN_URL)
    log("[HF] 步骤: 打开链接 -> New token -> 权限选 Read -> 复制 hf_ 开头的字符串")
    log("[HF] 也可以先设环境变量: os.environ['HF_TOKEN'] = 'hf_xxx'")
    log("-" * 74)

    if not ASK_HF_TOKEN:
        log("[HF] COMFY_ASK_HF_TOKEN=0，跳过输入，继续匿名下载")
        return ""
    if not (force or in_notebook() or sys.stdin.isatty()):
        log("[HF] 非交互环境，跳过输入，继续匿名下载")
        return ""

    prompt = "[HF] 粘贴 token 后回车（直接回车 = 跳过）: "
    token = ""
    try:
        from getpass import getpass

        token = getpass(prompt)
    except Exception:
        try:
            token = input(prompt)
        except Exception:
            token = ""

    token = str(token or "").strip()
    if not token:
        log("[HF] 已跳过，继续匿名下载（速度可能受限）")
        return ""
    return set_hf_token(token)


def ensure_hf_hub() -> str:
    """确保 huggingface_hub 可用且和 transformers / ComfyUI 兼容。

    踩过的坑：``pip install -U "huggingface_hub[hf_transfer]"``
      1. 2.x 删除了 hf-transfer extra，会报 does not provide the extra；
      2. 强升 2.x 会让 transformers / gradio / datasets / diffusers 的
         ``huggingface-hub<2.0`` 约束失效，ComfyUI 启动时
         ``import transformers`` 直接 ImportError。

    策略：缺失才装；已装 2.x 但 transformers 导入失败时自动降级到 <2。
    """
    version = _installed_hub_version()

    if version is None:
        log("[依赖] 未检测到 huggingface_hub，安装 1.x（与 ComfyUI / transformers 兼容）")
        _run([sys.executable, "-m", "pip", "install", "huggingface_hub<2"], check=False)
        version = _installed_hub_version() or "安装失败"

    if _hub_major(version) >= 2:
        ok, detail = _transformers_probe()
        if ok:
            log("[依赖] huggingface_hub " + version + "，transformers " + detail + " 可用")
        else:
            log("[依赖] huggingface_hub " + version + " 与 transformers 不兼容：")
            log("       " + detail[:200])
            log("[依赖] 自动降级到 huggingface_hub<2 ...")
            _run([sys.executable, "-m", "pip", "install", "huggingface_hub<2"], check=False)
            version = _installed_hub_version() or version
            ok, detail = _transformers_probe()
            if ok:
                log("[依赖] 已降级到 " + version + "，transformers " + detail + " 可用了")
                log("[依赖] 本进程内的 hub 仍是旧版本，继续下载前建议重启内核")
            else:
                log("[警告] 降级后 transformers 仍不可用：" + detail[:200])
                log("[警告] 手动修复：pip install 'huggingface-hub<2' 'transformers<5'")
    else:
        log("[依赖] huggingface_hub " + str(version))
    return str(version)


def install_requirements() -> None:
    """安装 ComfyUI 依赖 + huggingface_hub。"""
    ensure_comfyui()
    requirements = COMFY_DIR / "requirements.txt"
    if not requirements.exists():
        raise RuntimeError("找不到 " + str(requirements))

    cmd = [sys.executable, "-m", "pip", "install", "-r", str(requirements)]
    if TORCH_INDEX_URL:
        cmd += ["--extra-index-url", TORCH_INDEX_URL]
    _run(cmd, check=True)

    ensure_hf_hub()

    if COMFY_SAGE == "1":
        log("[可选] 安装 SageAttention（约 2 倍加速，需要编译环境）")
        _run([sys.executable, "-m", "pip", "install", "-U", "sageattention"], check=False)


# ---------------------------------------------------------------------------
# 2.5 自定义节点（Qwen3-TTS）
# ---------------------------------------------------------------------------


def _tts_node_dir():
    """custom_nodes 下 Qwen3-TTS 节点包的位置。"""
    return COMFY_DIR / "custom_nodes" / TTS_NODE_DIRNAME


def _tts_node_installed() -> bool:
    return (_tts_node_dir() / "__init__.py").is_file()


def _pkg_version(name):
    """读已安装包版本（走 metadata，pip 改完当前进程也能读到新值）。"""
    try:
        from importlib import metadata

        return metadata.version(name)
    except Exception:
        return None


def _clone_tts_node_pack(dest):
    """克隆节点包，官方地址不通时按顺序换 GitHub 镜像。"""
    urls = [TTS_NODE_REPO]
    if TTS_NODE_REPO.startswith("https://"):
        urls += [mirror.rstrip("/") + "/" + TTS_NODE_REPO for mirror in TTS_NODE_MIRRORS]

    last_error = None
    for url in urls:
        log("[节点] 克隆 " + url)
        try:
            _run(["git", "clone", "--depth", "1", url, str(dest)], check=True)
            return
        except Exception as exc:
            last_error = exc
            log("[节点] 克隆失败: " + str(exc))
            shutil.rmtree(str(dest), ignore_errors=True)
    raise RuntimeError(
        "Qwen3-TTS 节点克隆失败，请检查网络或设置 COMFY_TTS_NODE_REPO: " + str(last_error)
    )


def _install_tts_node_requirements(target) -> bool:
    """装节点包的 requirements.txt；返回是否动了环境。"""
    requirements = target / "requirements.txt"
    if not requirements.exists():
        log("[节点] 没有 requirements.txt，跳过依赖安装")
        return False

    before_tf = _pkg_version("transformers")
    if before_tf:
        try:
            major = int(str(before_tf).split(".")[0])
        except Exception:
            major = 0
        if major >= 5:
            log("[节点] 注意：当前 transformers " + str(before_tf) + " 会被 qwen-tts 降到 4.57.3")
            log("[节点] 如果你的 Qwen3-VL / H3 节点依赖 5.x，先跳过依赖：")
            log("       cs.ensure_qwen3_tts_node(install_deps=False)   # 或 COMFY_TTS_NODE_SKIP_DEPS=1")
    snapshot = WORK_DIR / "pip-freeze-before-tts-node.txt"
    try:
        freeze = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, timeout=600
        )
        if freeze.returncode == 0:
            snapshot.write_text(freeze.stdout or "", encoding="utf-8")
            log("[节点] 当前环境已快照: " + str(snapshot))
            log("[节点] 依赖出问题的回滚命令: " + sys.executable + " -m pip install -r " + str(snapshot))
    except Exception as exc:
        log("[节点] 环境快照失败（不影响继续）: " + str(exc))

    # 最小依赖优先：完整 requirements 会拖进 gradio（30MB+，版本解析反复回溯）
    # 和 librosa → numba/llvmlite（60MB 还要编译），内存小的实例极易被打满 OOM，
    # 连 sshd 都被杀掉。节点运行时其实只需要 qwen_tts / transformers / soundfile / librosa / accelerate。
    extra = ["--extra-index-url", TORCH_INDEX_URL] if TORCH_INDEX_URL else []
    py = _comfy_python()
    if py != sys.executable:
        log("[节点] ComfyUI 用的是 " + py)
        log("[节点] 当前脚本是 " + sys.executable + "，依赖将装进 ComfyUI 那个解释器")
    ok = False
    try:
        log("[节点] 第一步：最小依赖安装（跳过 gradio / tensorboard）")
        # qwen_tts 的 speech_vq 会 import sox：既要有系统 sox 命令，也要有 python 包 sox
        if shutil.which("sox") is None:
            log("[节点] 系统缺少 sox 命令，尝试 apt-get 安装（失败请手动: apt-get install -y sox）")
            _run(["apt-get", "update"], check=False)
            _run(["apt-get", "install", "-y", "sox"], check=False)
        _run([py, "-m", "pip", "install", "--no-deps", "qwen-tts"] + extra, check=True)
        _run(
            [
                py, "-m", "pip", "install",
                "numpy", "safetensors", "soundfile", "accelerate", "librosa", "modelscope",
                "sox", "transformers==4.57.3",
            ]
            + extra,
            check=True,
        )
        ok = _probe_tts_node_import(target)
    except Exception as exc:
        log("[节点] 最小依赖安装失败: " + str(exc))

    if not ok:
        log("[节点] 最小依赖不足，回退到 requirements.txt 全量安装（较慢、占内存）")
        cmd = [py, "-m", "pip", "install", "-r", str(requirements)]
        if TORCH_INDEX_URL:
            cmd += ["--extra-index-url", TORCH_INDEX_URL]
        _run(cmd, check=True)

    after_tf = _pkg_version("transformers")
    if before_tf and after_tf and before_tf != after_tf:
        log("[节点] transformers " + str(before_tf) + " -> " + str(after_tf) + "（qwen-tts 要求 4.57.3）")
        log("[节点] 若 Qwen-Image-2.1 / H3 工作流随后报错，回滚: ")
        log("       " + sys.executable + " -m pip install -r " + str(snapshot))
    elif after_tf:
        log("[节点] transformers 保持 " + str(after_tf) + "，未受节点包影响")
    _report_pip_check()
    return True


def _report_pip_check() -> None:
    """把 pip check 的冲突列出来——很多平台镜像里包本来就不互相兼容。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "check"], capture_output=True, text=True, timeout=300
        )
    except Exception as exc:
        log("[节点] pip check 跳过: " + str(exc))
        return
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    lines = [line for line in lines if "No broken requirements found" not in line]
    if result.returncode == 0 or not lines:
        log("[节点] pip check: 没有依赖冲突")
        return
    log("[节点] pip check 有 " + str(len(lines)) + " 条冲突（与 ComfyUI 无关的可以忽略）:")
    for line in lines[:10]:
        log("       " + line)
    log("[节点] 判定标准：ComfyUI 能启动 + /object_info 里 Qwen3Loader 在 = 这些冲突不影响你")


def restore_pip_snapshot(path=None) -> None:
    """回滚到装 Qwen3-TTS 节点之前的包版本（用安装时留的 pip freeze 快照）。

    qwen-tts 把 transformers 钉在 4.57.3，而它要求 huggingface-hub<1.0；
    如果你还要 diffusers（要求 hub>=1.32）这类包，两者无法共存，只能二选一：
    要么回滚，要么放弃那批包。
    """
    snapshot = Path(path) if path else WORK_DIR / "pip-freeze-before-tts-node.txt"
    if not snapshot.exists():
        raise RuntimeError(
            "找不到快照文件 " + str(snapshot) + "；它只在 ensure_qwen3_tts_node() 装依赖时生成"
        )
    log("[回滚] 按快照恢复包版本: " + str(snapshot))
    _run([sys.executable, "-m", "pip", "install", "-r", str(snapshot)], check=False)
    log("[回滚] 完成。注意：qwen-tts 会因 transformers 版本不匹配而加载失败，需要重新取舍")


def _comfy_python() -> str:
    """取正在跑的 ComfyUI 进程所用的解释器。

    平台（如 CloudStudio）常用 pyenv / conda 里的 python 启动 main.py，
    和当前脚本的 sys.executable 可能不是同一个；装错环境等于没装。
    """
    try:
        pids = subprocess.run(
            ["pgrep", "-f", "main.py"], capture_output=True, text=True, timeout=5
        ).stdout.split()
    except Exception:
        return sys.executable
    for pid in pids:
        try:
            raw = Path("/proc/" + pid + "/cmdline").read_bytes().split(b"\0")
        except Exception:
            continue
        if not raw:
            continue
        exe = raw[0].decode(errors="replace").strip()
        if "python" in exe:
            return exe
    return sys.executable


def _probe_tts_node_import(target) -> bool:
    """在子进程里 import 一遍依赖，确认不是装了个空壳。"""
    py = _comfy_python()
    try:
        result = subprocess.run(
            [py, "-c", "import qwen_tts, soundfile, accelerate; print('ok')"],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except Exception as exc:
        log("[节点] 依赖自检异常: " + str(exc))
        return False
    if result.returncode == 0:
        log("[节点] 依赖自检通过（qwen_tts / soundfile / accelerate）")
        return True
    lines = [line for line in (result.stderr or "").strip().splitlines() if line.strip()]
    log("[节点] 依赖自检失败: " + (lines[-1] if lines else "unknown"))
    log("[节点] 手动修复: " + py + " -m pip install -r " + str(target / "requirements.txt"))
    return False


def ensure_qwen3_tts_node(force=False, install_deps=None, restart=True):
    """安装 DarioFT/ComfyUI-Qwen3-TTS（Qwen3Loader / Qwen3CustomVoice 等节点）。

    不装这个包，工作流里任何 Qwen3-TTS 节点都会在 /prompt 校验阶段被拒：
    HTTP 400 missing_node_type: Node '...' not found. The custom node may not be installed.

    参数：
      force        —— 已存在时也 git pull 更新
      install_deps —— None 表示按 COMFY_TTS_NODE_SKIP_DEPS 决定；False 只克隆不装依赖
      restart      —— 新装了节点且 ComfyUI 正在跑时自动停掉，等 launch() 重启
    """
    ensure_comfyui()
    target = _tts_node_dir()
    changed = False

    if _tts_node_installed():
        log("[节点] 已存在: " + str(target))
        if force:
            log("[节点] 更新到最新版本")
            _run(["git", "-C", str(target), "pull", "--ff-only"], check=False)
            changed = True
    else:
        if shutil.which("git") is None:
            raise RuntimeError("找不到 git，请先安装：apt-get update && apt-get install -y git")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(str(target), ignore_errors=True)
        _clone_tts_node_pack(target)
        changed = True
        log("[节点] 已克隆到 " + str(target))

    if install_deps is None:
        install_deps = not TTS_NODE_SKIP_DEPS

    if install_deps:
        changed = _install_tts_node_requirements(target) or changed
        _probe_tts_node_import(target)
    else:
        log("[节点] 已跳过依赖安装（install_deps=False）")
        log("[节点] 需要时手动执行: " + sys.executable + " -m pip install -r " + str(target / "requirements.txt"))

    if changed and restart:
        if _PROC is not None and _PROC.poll() is None:
            log("[节点] 自定义节点只在启动时加载，当前进程不会识别新节点；正在停止 ...")
            stop()
            log("[节点] 下一步: cs.launch() 重启 ComfyUI")
        elif _http_ok("http://127.0.0.1:" + str(PORT) + "/system_stats", 3.0):
            log("[节点] 端口 " + str(PORT) + " 上还有旧的 ComfyUI 在跑（不是本 kernel 启动的）")
            log("[节点] 那个进程不会加载新节点，正在结束它 ...")
            stop()
            log("[节点] 下一步: cs.launch() 重启 ComfyUI")

    return target


def _node_loaded(node_type, port=None) -> bool:
    """问 /object_info/<node_type> 是否已注册（返回空对象表示没有）。"""
    port = int(port or PORT)
    url = "http://127.0.0.1:" + str(port) + "/object_info/" + node_type
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return bool(json.loads(response.read().decode("utf-8") or "{}"))
    except Exception:
        return False


def check_tts_nodes(port=None) -> bool:
    """确认 Qwen3-TTS 节点真的被 ComfyUI 加载了（而不是只克隆了目录）。"""
    missing = []
    for node_type in TTS_NODE_CLASSES:
        if _node_loaded(node_type, port):
            log("[节点] 已加载: " + node_type)
        else:
            log("[节点] 未加载: " + node_type)
            missing.append(node_type)
    if missing:
        log("[节点] 缺少 " + ", ".join(missing))
        log("[节点] 依次确认: cs.ensure_qwen3_tts_node() 跑过 -> ComfyUI 重启过 -> 日志里没有 IMPORT FAILED")
        return False


def setup_customvoice(port=None, download=True, restart=True, force=False, install_deps=None, wait=True) -> bool:
    """一条命令搞定 Qwen3-TTS CustomVoice：装节点 -> 下模型 -> 重启 -> 验证。

    等价于原来手动在终端跑的这几步：

        git clone ... custom_nodes/ComfyUI-Qwen3-TTS   # 走镜像
        pip install --no-deps qwen-tts
        pip install numpy safetensors soundfile accelerate librosa modelscope transformers
        pkill main.py && python main.py ...
        curl /object_info/Qwen3Loader

    返回 True 表示「节点已加载 且 模型完整」，可以提交工作流。
    """
    log("=" * 62)
    log("Qwen3-TTS CustomVoice 一键安装")
    log("=" * 62)

    # 1) 自定义节点（已装过则复用；restart=True 时会把跑着的 ComfyUI 停掉）
    ensure_qwen3_tts_node(force=force, install_deps=install_deps, restart=restart)

    # 2) 模型
    if download:
        download_models(["tts"])
        verify_models(["tts"])
    else:
        ok, lines = _tts_check()
        log("[模型] 跳过下载，当前状态: " + ("完整" if ok else "不完整"))
        for line in lines:
            log("  " + line)

    # 3) 重启 + 验证（节点只在启动时加载，所以必须重启才算数）
    if restart:
        launch(port=port, wait=wait)

    ok_node = check_tts_nodes(port)
    ok_model = _tts_ready()

    log("-" * 62)
    if ok_node and ok_model:
        log("✅ 全部就绪：节点已加载 + 模型完整，现在提交工作流不会再报 missing_node_type")
        return True
    if ok_node:
        log("⚠️  节点已加载，但模型不完整 -> 执行 cs.download_models(['tts'])")
        return False
    log("❌ 节点未加载 -> 执行 cs.tail_log() 看日志里 ComfyUI-Qwen3-TTS 是否 IMPORT FAILED")
    return False


# ---------------------------------------------------------------------------
# 3. 下载
# ---------------------------------------------------------------------------


def resolve_endpoint() -> str:
    """选择 HuggingFace 端点并写入环境变量，返回端点 URL。"""
    mode = str(HF_ENDPOINT_MODE).strip()
    lower = mode.lower()
    if lower in ("official", "huggingface", "hf"):
        endpoint = "https://huggingface.co"
    elif lower in ("mirror", "hf-mirror"):
        endpoint = HF_MIRROR
    elif lower.startswith("http"):
        endpoint = mode.rstrip("/")
    else:
        if _http_ok("https://huggingface.co/api/models?limit=1", 5.0):
            endpoint = "https://huggingface.co"
        else:
            endpoint = HF_MIRROR
            log("[网络] huggingface.co 不可达，改用镜像 " + endpoint)

    os.environ["HF_ENDPOINT"] = endpoint
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    try:
        import hf_transfer  # noqa: F401
        import huggingface_hub as _hub

        # hf_transfer 只对 1.x 有意义，2.x 走 Xet
        if int(str(getattr(_hub, "__version__", "1")).split(".")[0]) < 2:
            os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
    except Exception:
        pass

    try:
        import huggingface_hub.constants as constants

        constants.ENDPOINT = endpoint
        from huggingface_hub import file_download

        if hasattr(file_download, "ENDPOINT"):
            file_download.ENDPOINT = endpoint
    except Exception:
        pass

    log("[下载] HF 端点: " + endpoint)
    return endpoint


class DownloadStalled(RuntimeError):
    """下载线程卡死（不是普通网络错误，进程内重试无效）。"""


def _cache_bytes() -> int:
    """统计 models/.cache 下已落盘的字节数（看门狗用）。"""
    root = MODELS_DIR / ".cache"
    total = 0
    for item in root.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _sha256_file(path, chunk: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _run_with_watchdog(fn, repo: str, stall_seconds=None):
    """跑下载调用，同时盯着缓存目录的字节增长。

    hf 卡死时不会抛异常，只靠 ``except Exception`` 永远等不到，所以这里自己看门：
    连续 ``stall_seconds`` 秒一个字节都没涨就判定为死锁并放弃等待。
    """
    limit = int(HF_STALL_SECONDS if stall_seconds is None else stall_seconds)
    if limit <= 0:
        return fn()

    box = {}

    def worker():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 原样抛回主线程
            box["error"] = exc

    thread = threading.Thread(target=worker, name="hf-download", daemon=True)
    thread.start()

    baseline = _cache_bytes()
    last_change = time.monotonic()
    while thread.is_alive():
        thread.join(10)
        if not thread.is_alive():
            break
        current = _cache_bytes()
        if current != baseline:
            baseline = current
            last_change = time.monotonic()
            continue
        if time.monotonic() - last_change >= limit:
            raise DownloadStalled(
                "下载停滞 "
                + str(limit)
                + " 秒没有任何字节增长（连接被中断后卡在文件锁上）："
                + repo
            )
    if "error" in box:
        raise box["error"]
    return box.get("value")


def salvage_incomplete(dry_run: bool = False) -> list:
    """把「已经下完、但没来得及 rename」的 .incomplete 直接归位。

    huggingface_hub 在 local_dir 模式下先写
        models/.cache/huggingface/download/<子目录>/<etag>.<sha256>.incomplete
    再改名成正式文件。进程若在收尾阶段卡死，就会留下内容完整、却永远不落盘的
    .incomplete —— 27GB 白下。这里按同目录的 *.lock 还原目标文件名，并用文件名
    里的 sha256 自检，只有对得上才移动。
    """
    download_root = MODELS_DIR / ".cache" / "huggingface" / "download"
    if not download_root.is_dir():
        return []

    moved = []
    for lock in sorted(download_root.rglob("*.lock")):
        name = lock.name[: -len(".lock")]
        if not name:
            continue
        relative = lock.parent.relative_to(download_root) / name
        target = MODELS_DIR / relative
        if target.exists():
            continue
        for candidate in sorted(lock.parent.glob("*.incomplete")):
            match = re.search(r"([0-9a-f]{64})\.incomplete$", candidate.name)
            if not match:
                continue
            size = candidate.stat().st_size
            log(
                "[自愈] 发现疑似完整的分片 "
                + str(candidate.relative_to(MODELS_DIR))
                + "（"
                + human(size)
                + "），校验 sha256 ..."
            )
            if _sha256_file(candidate) != match.group(1):
                log("[自愈] sha256 不匹配，跳过（会重新下载）")
                continue
            if dry_run:
                moved.append((str(candidate), str(target)))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidate), str(target))
            try:
                lock.unlink()
            except OSError:
                pass
            log(
                "[自愈] 已归位 "
                + str(relative)
                + "（"
                + human(size)
                + "，sha256 一致）"
            )
            moved.append((str(candidate), str(target)))
            break
    return moved


_TREE_CACHE = {}


def _repo_tree(repo: str) -> dict:
    """取仓库文件的官方 lfs sha256（path -> sha256），带进程内缓存。"""
    if repo not in _TREE_CACHE:
        endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
        url = endpoint + "/api/models/" + repo + "/tree/main?recursive=true"
        request = urllib.request.Request(url, headers={"User-Agent": "comfyui-setup/1.0"})
        if HF_TOKEN:
            request.add_header("Authorization", "Bearer " + HF_TOKEN)
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        _TREE_CACHE[repo] = {
            entry["path"]: entry.get("lfs", {}).get("oid")
            for entry in payload
            if entry.get("type") == "file"
        }
    return _TREE_CACHE[repo]


def _snapshot(repo: str, patterns, workers: int):
    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:
        raise RuntimeError("缺少 huggingface_hub，请先 pip install huggingface_hub（" + str(exc) + "）")

    wanted = [
        ("repo_id", repo),
        ("allow_patterns", list(patterns)),
        ("local_dir", str(MODELS_DIR)),
        ("max_workers", int(workers)),
        ("local_dir_use_symlinks", False),
    ]
    if HF_TOKEN:
        wanted.append(("token", HF_TOKEN))
    # 只传目标版本真的支持的参数，1.x / 2.x 都能跑
    params = inspect.signature(snapshot_download).parameters
    kwargs = {key: value for key, value in wanted if key in params}
    kwargs["repo_id"] = repo
    return _run_with_watchdog(lambda: snapshot_download(**kwargs), repo)


def _download_repo(repo: str, patterns, workers: int) -> None:
    candidates = [repo] + list(FALLBACK_REPOS.get(repo, ()))
    errors = []
    for candidate in candidates:
        for attempt in range(1, HF_RETRIES + 1):
            log("[下载] " + candidate + " —— " + str(len(patterns)) + " 个文件，第 " + str(attempt) + " 次尝试")
            try:
                _snapshot(candidate, patterns, workers)
                log("[下载] 完成: " + candidate)
                return
            except KeyboardInterrupt:
                raise
            except DownloadStalled as exc:
                log("[下载] 卡死: " + str(exc))
                log("[下载] 线程已无响应，同一进程内重试无效；重新运行脚本即可（已下数据保留，分片会自动归位）")
                raise
            except Exception as exc:
                log("[下载] 失败: " + type(exc).__name__ + ": " + str(exc))
                errors.append(str(exc))
                if attempt < HF_RETRIES:
                    time.sleep(5 * attempt)
        log("[下载] 换源重试: " + candidate + " 不可用")
    raise RuntimeError("下载失败：" + " / ".join(errors[-3:]))


def _snapshot_to(repo: str, local_dir: Path, workers: int) -> None:
    """将完整 HF 仓库下载到指定目录，保留 config/tokenizer/权重等目录结构。"""
    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:
        raise RuntimeError("缺少 huggingface_hub，请先 pip install huggingface_hub（" + str(exc) + "）")

    wanted = [
        ("repo_id", repo),
        ("local_dir", str(local_dir)),
        ("max_workers", int(workers)),
        ("local_dir_use_symlinks", False),
    ]
    if HF_TOKEN:
        wanted.append(("token", HF_TOKEN))
    params = inspect.signature(snapshot_download).parameters
    kwargs = {key: value for key, value in wanted if key in params}
    kwargs["repo_id"] = repo
    return _run_with_watchdog(lambda: snapshot_download(**kwargs), repo)


TTS_REQUIRED = (
    # (相对路径, 最小字节数)；小于最小值的直接判为没下完
    ("config.json", 1),
    ("model.safetensors", 3_500_000_000),
    ("tokenizer_config.json", 1),
    ("vocab.json", 1),
    ("speech_tokenizer/model.safetensors", 650_000_000),
)


def _tts_check():
    """逐个核对 TTS 模型文件，返回 (是否完整, 明细行)。"""
    lines = []
    ok = True
    for rel, min_size in TTS_REQUIRED:
        path = TTS_MODEL_DIR / rel
        if not path.is_file():
            lines.append("缺失      " + rel)
            ok = False
            continue
        size = path.stat().st_size
        good = size >= min_size
        lines.append(("OK   " if good else "不完整 ") + human(size).rjust(9) + "  " + rel)
        ok = ok and good
    return ok, lines


def _tts_ready() -> bool:
    """文件齐全且大小达标才算就绪（只看 config.json 会把半成品误判成可用）。"""
    return _tts_check()[0]


def _download_tts(workers: int) -> None:
    """下载 Qwen3-TTS 整仓（HF snapshot，保留目录结构）。"""
    TTS_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    last_error = None
    for attempt in range(1, HF_RETRIES + 1):
        log("[下载] " + TTS_REPO + " · 整仓，第 " + str(attempt) + " 次尝试")
        try:
            _snapshot_to(TTS_REPO, TTS_MODEL_DIR, workers)
            if _tts_ready():
                log("[下载] TTS 模型下载完成: " + str(TTS_MODEL_DIR))
                return
            raise RuntimeError("下载后缺少 config.json 或 safetensors 权重")
        except KeyboardInterrupt:
            raise
        except DownloadStalled:
            raise
        except Exception as exc:
            last_error = exc
            log("[下载] 失败: " + type(exc).__name__ + ": " + str(exc))
            if attempt < HF_RETRIES:
                time.sleep(5 * attempt)
    raise RuntimeError("TTS 模型下载失败: " + str(last_error))


def download_models(groups=None, workers=None, force=False, ask_token=None) -> None:
    """按组下载模型，已完整存在的文件自动跳过，支持断点续传。

    下载前会先算磁盘：放不下就直接报错并给出能放下的组，
    避免下到一半把盘写满（Kaggle/Colab 上尤其常见）。

    没设 HF_TOKEN 时会停下提示输入，token 获取地址
    https://huggingface.co/settings/tokens ；ask_token=False 可跳过询问。
    """
    selected = [g for g in (groups or DOWNLOAD_GROUPS) if g in GROUPS]
    if not selected:
        log("[下载] 没有选中任何组")
        return
    items = [m for m in MODELS if m.group in selected]
    total = sum(m.size for m in items)
    tts_ready = "tts" in selected and _tts_ready()
    if "tts" in selected and not tts_ready:
        total += TTS_MIN_EXPECTED_BYTES
    free = _disk_free(WORK_DIR)
    log("")
    log("[下载] 共 " + str(len(items)) + " 个文件"
        + (" + TTS 整仓" if "tts" in selected and not tts_ready else "")
        + ", " + human(total) + ("（TTS 为预估值）" if "tts" in selected and not tts_ready else "")
        + "；磁盘剩余 " + human(free))

    if free and total > free * 0.98 and not force:
        feasible = []
        for group in selected:
            if group == "tts":
                size = 0 if tts_ready else TTS_MIN_EXPECTED_BYTES
            else:
                size = sum(m.size for m in MODELS if m.group == group)
            if size <= free * 0.95:
                feasible.append(group + "（" + human(size) + "）")
        hint = ("磁盘只够: " + "、".join(feasible)) if feasible else "连单独一组都放不下"
        raise RuntimeError(
            "磁盘不足：需要 "
            + human(total)
            + "，可用 "
            + human(free)
            + "；"
            + hint
            + "。可以 cs.set_work_dir('/kaggle/temp') 换大盘，或 download_models(groups, force=True) 强行尝试。"
        )

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    salvage_incomplete()

    pending = []
    for item in items:
        target = MODELS_DIR / item.path
        if target.exists() and target.stat().st_size == item.size:
            continue
        if target.exists():
            log("[修复] 大小不匹配，删除 " + item.path)
            try:
                target.unlink()
            except OSError:
                pass
        pending.append(item)

    if not pending and ("tts" not in selected or tts_ready):
        log("[下载] 所有文件已就绪，无需下载")
        return

    resolve_endpoint()
    if not HF_TOKEN and ask_token is not False:
        ask_hf_token(force=bool(ask_token))
    if not HF_TOKEN:
        log("[提示] 仍是匿名下载，速度可能受限；token 地址 " + HF_TOKEN_URL)

    by_repo = {}
    for item in pending:
        by_repo.setdefault(item.repo, []).append(item)

    for repo, repo_items in by_repo.items():
        patterns = [m.path for m in repo_items]
        log("[下载] " + repo + " -> " + str(len(patterns)) + " 个文件")
        _download_repo(repo, patterns, int(workers or HF_MAX_WORKERS))

    if "tts" in selected and not tts_ready:
        log("[下载] " + TTS_REPO + " -> 完整模型目录")
        _download_tts(int(workers or HF_MAX_WORKERS))

    log("[下载] 全部完成")


# ---------------------------------------------------------------------------
# 4. 校验
# ---------------------------------------------------------------------------


def verify_models(groups=None, check_hash=False):
    """校验文件是否存在且大小正确，返回不合格的 ModelFile 列表。

    check_hash=True 时还会对照官方 lfs sha256（需要能访问 HF API）。
    """
    selected = [g for g in (groups or DOWNLOAD_GROUPS) if g in GROUPS]
    items = [m for m in MODELS if m.group in selected]
    bad = []
    log("")
    log("[校验] " + str(len(items)) + " 个文件")
    log("-" * 74)
    for item in items:
        target = MODELS_DIR / item.path
        if target.exists():
            actual = target.stat().st_size
            ok = actual == item.size
            shown = human(actual)
            if ok and check_hash:
                want = None
                try:
                    want = _repo_tree(item.repo).get(item.path)
                except Exception as exc:  # 离线时退回只比大小
                    log("[校验] 取不到官方 sha256（" + str(exc)[:80] + "），只比大小")
                if want:
                    got = _sha256_file(target)
                    ok = got == want
                    shown += "  sha256 " + ("一致" if ok else "不一致")
        else:
            ok = False
            shown = "缺失"
        flag = "OK   " if ok else "ERROR"
        log(flag + "  " + shown.rjust(9) + "  " + item.path)
        if not ok:
            bad.append(item)
    tts_ok = True
    if "tts" in selected:
        tts_ok, tts_lines = _tts_check()
        log("TTS 模型目录: " + str(TTS_MODEL_DIR))
        for line in tts_lines:
            log("  " + line)
        if not tts_ok:
            log("[校验] TTS 模型不完整（有缺文件或大小不足）")
    log("-" * 74)
    if bad:
        log("[校验] 有 " + str(len(bad)) + " 个文件不完整，请重新运行 download_models() 继续下载")
    elif "tts" in selected and not tts_ok:
        log("[校验] TTS 模型不完整，请重新运行 download_models(['tts'])")
    else:
        log("[校验] 全部通过")
    return bad


# ---------------------------------------------------------------------------
# 5. 启动 / 停止
# ---------------------------------------------------------------------------


def _sage_ready() -> bool:
    if str(COMFY_SAGE).lower() in ("0", "false", "no", "off"):
        return False
    try:
        import sageattention  # noqa: F401

        return True
    except Exception:
        return False


def _preflight_transformers() -> None:
    """启动前确认 transformers 能导入，避免 ComfyUI 起来就 ImportError 退出。"""
    ok, detail = _transformers_probe()
    if ok:
        log("[检查] transformers " + detail)
        return
    log("[检查] transformers 不可用：" + detail[:200])
    log("[检查] 尝试修复依赖版本 ...")
    ensure_hf_hub()
    ok, detail = _transformers_probe()
    if not ok:
        raise RuntimeError(
            "transformers 无法导入，ComfyUI 起不来：" + detail[:300] + "；请执行 pip install 'huggingface-hub<2'"
        )
    log("[检查] 修复后 transformers " + detail)


def launch(port=None, wait=True, timeout=1800, extra_args=None, tunnel=None):
    """启动 ComfyUI（后台子进程 + 日志文件），返回 Popen 对象。"""
    global _PROC, _LOG_HANDLE, _PROC_ARGS
    port = int(port or PORT)
    if not (COMFY_DIR / "main.py").exists():
        raise RuntimeError("ComfyUI 未安装，请先调用 ensure_comfyui()")
    _preflight_transformers()

    if tunnel is None:
        tunnel = TUNNEL
    mode = str(tunnel).strip().lower()
    want_tunnel = mode not in ("none", "0", "off", "false", "")

    cmd = [
        sys.executable,
        "main.py",
        "--listen",
        "0.0.0.0",
        "--port",
        str(port),
        "--disable-auto-launch",
    ]
    if PREVIEW_METHOD:
        cmd += ["--preview-method", PREVIEW_METHOD]
    if _sage_ready():
        cmd += ["--use-sage-attention"]
        log("[启动] 已启用 SageAttention")
    if want_tunnel and os.environ.get("COMFY_CORS", "1") != "0":
        # ComfyUI 默认的 origin 中间件会把 tunnel 域名下"点击链接"发起的跨站请求
        # 直接 403（Access denied）；开这个头才能正常打开页面。
        cmd += ["--enable-cors-header"]
        log("[启动] 已加 --enable-cors-header（隧道访问需要；COMFY_CORS=0 可关闭）")
    if extra_args:
        cmd += [str(arg) for arg in extra_args]

    if _PROC is not None and _PROC.poll() is None:
        if _PROC_ARGS and list(cmd) != list(_PROC_ARGS):
            log("[启动] 已有 ComfyUI 在跑，但启动参数变了；先 cs.stop() 再 launch() 才会生效")
        else:
            log("[启动] 已在运行，PID=" + str(_PROC.pid))
        return _PROC
    _PROC_ARGS = list(cmd)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("HF_ENDPOINT", "https://huggingface.co")

    _LOG_HANDLE = open(str(LOG_PATH), "ab", buffering=0)
    kwargs = {
        "stdout": _LOG_HANDLE,
        "stderr": subprocess.STDOUT,
        "cwd": str(COMFY_DIR),
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True

    log("$ " + " ".join(cmd))
    _PROC = subprocess.Popen(cmd, **kwargs)
    log("[启动] PID=" + str(_PROC.pid) + "，日志: " + str(LOG_PATH))

    ready = True
    if wait:
        ready = wait_ready(port, timeout)

    if tunnel is None:
        tunnel = TUNNEL
    want_tunnel = str(tunnel).strip().lower() not in ("none", "0", "off", "false", "")
    tunnel_url = start_tunnel(port, mode=tunnel) if want_tunnel else None

    log("")
    log("-" * 74)
    log("本机   : http://127.0.0.1:" + str(port))
    log("局域网 : " + public_url(port))
    _proxy = colab_url(port)
    if _proxy:
        log("Colab  : " + _proxy)
    log("显存   : 不够就重跑 cs.launch(extra_args=['--lowvram'])（或 --normalvram / --highvram）")
    if tunnel_url:
        log("公网   : " + tunnel_url + "   <-- 最终访问地址（隧道）")
    elif want_tunnel:
        log("公网   : 隧道未成功，原因见 " + str(TUNNEL_LOG) + " 末尾")
        log("         SSH 转发: ssh -L " + str(port) + ":127.0.0.1:" + str(port) + " 用户名@服务器")
    else:
        log("公网   : 已关闭（COMFY_TUNNEL=none）；要开就 cs.launch(tunnel='cloudflared')")
    log("-" * 74)
    log("")
    if not ready:
        log("[提示] 尚未就绪，稍后用 tail_log() 查看日志")
    return _PROC


def wait_ready(port=None, timeout=1800) -> bool:
    """轮询 /system_stats 直到 ComfyUI 就绪。"""
    port = int(port or PORT)
    url = "http://127.0.0.1:" + str(port) + "/system_stats"
    deadline = time.time() + timeout
    started = time.time()
    log("[等待] ComfyUI 启动中 ...")
    while time.time() < deadline:
        if _PROC is not None and _PROC.poll() is not None:
            log("[错误] ComfyUI 进程已退出，日志末尾：")
            tail_log(60)
            return False
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    log("[就绪] 用时 " + str(int(time.time() - started)) + "s -> http://127.0.0.1:" + str(port))
                    return True
        except Exception:
            pass
        time.sleep(2)
    log("[超时] " + str(timeout) + "s 内未就绪，日志末尾：")
    tail_log(40)
    return False


def tail_log(lines=40) -> None:
    """打印 comfyui.log 末尾若干行。"""
    if not LOG_PATH.exists():
        log("(还没有日志: " + str(LOG_PATH) + ")")
        return
    content = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    log("-" * 74)
    for line in content[-int(lines):]:
        log(line)
    log("-" * 74)


def stop_tunnel() -> None:
    """只关掉隧道，保留 ComfyUI（换后端时用）。"""
    global _TUNNEL_PROC, _TUNNEL_URL
    if _TUNNEL_PROC is not None and _TUNNEL_PROC.poll() is None:
        _TUNNEL_PROC.terminate()
        try:
            _TUNNEL_PROC.wait(10)
        except subprocess.TimeoutExpired:
            _TUNNEL_PROC.kill()
        log("[隧道] 已关闭")
    else:
        log("[隧道] 当前没有运行中的隧道")
    _TUNNEL_PROC = None
    _TUNNEL_URL = None


def stop() -> None:
    """停止由本进程启动的 ComfyUI（含隧道）。"""
    global _PROC
    stop_tunnel()

    if _PROC is None:
        log("[停止] 本进程没有启动过 ComfyUI")
        return
    if _PROC.poll() is not None:
        log("[停止] ComfyUI 进程已退出")
        return
    _PROC.terminate()
    try:
        _PROC.wait(15)
    except subprocess.TimeoutExpired:
        _PROC.kill()
    log("[停止] ComfyUI 已结束 PID=" + str(_PROC.pid))


def public_url(port=None) -> str:
    """推断局域网访问地址。"""
    port = int(port or PORT)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return "http://" + str(ip) + ":" + str(port)
    except Exception:
        return "http://<本机IP>:" + str(port)


def colab_url(port=None, timeout=5.0):
    """Colab 环境下返回 kernel 端口代理地址，非 Colab / 超时返回 None。

    Colab 的 eval_js 偶尔会卡住（代理还没准备好时），所以放进线程里限时等待，
    不能让它把后面的访问地址打印堵死。
    """
    port = int(port or PORT)
    try:
        from google.colab.output import eval_js
    except Exception:
        return None

    expr = "google.colab.kernel.proxyPort(" + str(port) + ")"
    box = {}

    def worker():
        try:
            box["value"] = str(eval_js(expr))
        except Exception:
            try:
                box["value"] = str(eval_js(expr, await_js=False))
            except Exception:
                box["value"] = None

    try:
        import threading

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(float(timeout))
        if thread.is_alive():
            log("[访问] Colab 代理探测超时，跳过（不影响其它地址）")
            return None
    except Exception:
        # 少数环境不支持线程（如 Pyodide），退化为直接调用
        worker()
    return box.get("value") or None


def _download_cloudflared() -> str:
    """下载 cloudflared 可执行文件（GitHub 直连失败时走代理镜像）。"""
    bin_dir = WORK_DIR / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        name = "cloudflared-windows-amd64.exe"
        target = bin_dir / "cloudflared.exe"
    elif sys.platform == "darwin":
        raise RuntimeError("macOS 请先 brew install cloudflared")
    else:
        arch = "arm64" if platform.machine().lower() in ("aarch64", "arm64") else "amd64"
        name = "cloudflared-linux-" + arch
        target = bin_dir / "cloudflared"

    release = "https://github.com/cloudflare/cloudflared/releases/latest/download/" + name
    if target.exists() and target.stat().st_size > 1024 * 1024:
        log("[隧道] 复用已下载的 " + str(target))
        return str(target)
    urls = [
        os.environ.get("COMFY_CLOUDFLARED_URL", ""),
        release,
        "https://ghfast.top/" + release,
        "https://gh-proxy.com/" + release,
    ]
    last_error = None
    for url in urls:
        if not url:
            continue
        log("[隧道] 下载 " + url)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "comfyui-setup/1.0"})
            with urllib.request.urlopen(request, timeout=180) as response, open(str(target), "wb") as handle:
                shutil.copyfileobj(response, handle)
            if target.stat().st_size < 1024 * 1024:
                raise RuntimeError("文件过小，可能是错误页")
            target.chmod(0o755)
            return str(target)
        except Exception as exc:
            last_error = exc
            log("[隧道] 失败: " + str(exc))
    raise RuntimeError("cloudflared 下载失败: " + str(last_error))


def _read_log_since(path: Path, start: int) -> str:
    try:
        with open(str(path), "rb") as handle:
            handle.seek(start)
            return handle.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _log_tail(text: str, lines: int = 6) -> str:
    rows = [row for row in text.strip().splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


def _spawn_tunnel(exe: str, port: int, protocol, timeout: int):
    """起一个 cloudflared 进程并从新日志里解析 trycloudflare 地址。"""
    global _TUNNEL_PROC
    start = TUNNEL_LOG.stat().st_size if TUNNEL_LOG.exists() else 0
    cmd = [exe, "tunnel", "--url", "http://127.0.0.1:" + str(port), "--no-autoupdate"]
    if protocol:
        cmd += ["--protocol", protocol]
    log("[隧道] " + " ".join(cmd))
    try:
        with open(str(TUNNEL_LOG), "ab", buffering=0) as handle:
            handle.write(("\n=== " + time.strftime("%H:%M:%S") + " " + " ".join(cmd) + " ===\n").encode("utf-8"))
            _TUNNEL_PROC = subprocess.Popen(
                cmd,
                stdout=handle,
                stderr=subprocess.STDOUT,
                cwd=str(WORK_DIR),
            )
    except Exception as exc:
        log("[隧道] 无法启动 cloudflared: " + str(exc))
        return None

    deadline = time.time() + timeout
    pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    while time.time() < deadline:
        text = _read_log_since(TUNNEL_LOG, start)
        match = pattern.search(text)
        if match:
            return match.group(0)
        if _TUNNEL_PROC.poll() is not None:
            log("[隧道] cloudflared 已退出（exit " + str(_TUNNEL_PROC.returncode) + "），日志末尾：")
            for row in _log_tail(text).splitlines():
                log("       " + row)
            return None
        time.sleep(2)

    log("[隧道] " + str(timeout) + "s 内没拿到地址，日志末尾：")
    for row in _log_tail(_read_log_since(TUNNEL_LOG, start)).splitlines():
        log("       " + row)
    return None


def verify_public(url: str, timeout: float = 12.0):
    """从本机请求一次公网地址，返回状态码或错误名。

    用来区分是谁在拒绝：200 = 通；403/401 = 隧道服务商/Cloudflare 拦的；
    其它错误 = 地址不可达。
    """
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (comfyui-setup)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:
        return type(exc).__name__


def _wait_public(url: str, timeout: float = 60.0, interval: float = 5.0):
    """反复探测公网地址，直到 200/302 或超时，返回最后一次状态。"""
    deadline = time.time() + timeout
    status = "未探测"
    while time.time() < deadline:
        status = verify_public(url, timeout=15)
        if str(status) in ("200", "302"):
            return status
        time.sleep(interval)
    return status


SSH_TUNNELS = {
    "pinggy": {
        "args": [
            "-p", "443",
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=30",
            "-R0:localhost:{port}",
            "a.pinggy.io",
        ],
        "pattern": r"https://[a-z0-9.-]+\.pinggy\.link",
        "hint": "pinggy（443 端口出网，国内一般可用）",
    },
    "localhost.run": {
        "args": [
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=30",
            "-R", "80:localhost:{port}",
            "nokey@localhost.run",
        ],
        "pattern": r"https://[a-z0-9.-]+\.lhr\.life",
        "hint": "localhost.run（SSH 反向隧道）",
    },
}


def _start_ssh_tunnel(name: str, port: int, timeout: int):
    """用 ssh -R 反向隧道开公网地址（pinggy / localhost.run）。"""
    global _TUNNEL_PROC
    spec = SSH_TUNNELS[name]
    exe = shutil.which("ssh")
    if exe is None:
        log("[隧道] 找不到 ssh 客户端，无法用 " + name)
        return None

    cmd = [exe] + [part.format(port=port) for part in spec["args"]]
    start = TUNNEL_LOG.stat().st_size if TUNNEL_LOG.exists() else 0
    log("[隧道] " + spec["hint"])
    log("[隧道] " + " ".join(cmd))
    try:
        with open(str(TUNNEL_LOG), "ab", buffering=0) as handle:
            handle.write(("\n=== " + time.strftime("%H:%M:%S") + " " + " ".join(cmd) + " ===\n").encode("utf-8"))
            _TUNNEL_PROC = subprocess.Popen(
                cmd,
                stdout=handle,
                stderr=subprocess.STDOUT,
                cwd=str(WORK_DIR),
            )
    except Exception as exc:
        log("[隧道] 启动 ssh 失败: " + str(exc))
        return None

    deadline = time.time() + timeout
    pattern = re.compile(spec["pattern"])
    while time.time() < deadline:
        text = _read_log_since(TUNNEL_LOG, start)
        match = pattern.search(text)
        if match:
            return match.group(0)
        if _TUNNEL_PROC.poll() is not None:
            log("[隧道] ssh 已退出（exit " + str(_TUNNEL_PROC.returncode) + "），日志末尾：")
            for row in _log_tail(text).splitlines():
                log("       " + row)
            return None
        time.sleep(2)

    log("[隧道] " + str(timeout) + "s 内没拿到地址，日志末尾：")
    for row in _log_tail(_read_log_since(TUNNEL_LOG, start)).splitlines():
        log("       " + row)
    return None


def _start_cloudflared(port: int, timeout: int):
    exe = shutil.which("cloudflared")
    try:
        if exe is None:
            exe = _download_cloudflared()
        probe = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
        version = (probe.stdout or probe.stderr or "").strip().splitlines()
        log("[隧道] 使用 " + exe + " " + (version[0] if version else ""))
    except Exception as exc:
        log("[隧道] cloudflared 不可用: " + str(exc))
        return None

    for protocol in (None, "http2"):
        url = _spawn_tunnel(exe, port, protocol, timeout if protocol is None else max(45, timeout // 2))
        if url:
            return url
        log("[隧道] 换协议重试（protocol=http2）...")
    return None


def start_tunnel(port=None, timeout=90, mode=None):
    """开一条公网隧道，返回 URL（失败返回 None）。

    mode: cloudflared（默认）| pinggy | localhost.run | none
      * cloudflared —— trycloudflare.com，部分网络会被 Cloudflare 403 拦；
      * pinggy —— ssh -R 443，国内更容易通；
      * localhost.run —— ssh -R 80。
    """
    global _TUNNEL_URL
    port = int(port or PORT)
    if _TUNNEL_URL:
        log("[隧道] 已在运行: " + _TUNNEL_URL)
        return _TUNNEL_URL

    mode = str(mode or TUNNEL or "cloudflared").strip().lower()
    if mode in ("none", "0", "off", "false", ""):
        log("[隧道] 已按配置关闭（none）")
        return None
    if mode in ("auto", "yes", "true", "1", "cloudflared", "cf"):
        url = _start_cloudflared(port, timeout)
    elif mode.startswith("pinggy"):
        url = _start_ssh_tunnel("pinggy", port, timeout)
    elif mode.startswith(("localhost.run", "localhostrun", "lhr")):
        url = _start_ssh_tunnel("localhost.run", port, timeout)
    else:
        log("[隧道] 未知模式 " + mode + "，可用 cloudflared / pinggy / localhost.run")
        return None

    if not url:
        log("[隧道] 没能拿到公网地址，详见 " + str(TUNNEL_LOG))
        log("[隧道] 备选 SSH 端口转发: ssh -L " + str(port) + ":127.0.0.1:" + str(port) + " 用户名@服务器")
        return None

    _TUNNEL_URL = url
    status = _wait_public(url, timeout=float(os.environ.get("COMFY_TUNNEL_CHECK", "60")))
    log("[隧道] 公网地址: " + url)
    log("[隧道] 自检: HTTP " + str(status))
    if str(status) in ("403", "401"):
        log("[隧道] 403 有两种可能：")
        log("[隧道]   1) ComfyUI 的 origin 校验在拒绝跨站请求 -> 启动时加 --enable-cors-header")
        log("[隧道]   2) 隧道服务商/Cloudflare 按地区拦截 -> 换后端 tunnel='pinggy' / 'localhost.run'")
        log("[隧道] 先试：把地址直接粘贴到地址栏（不要点击链接），或 cs.launch(tunnel='cloudflared') 重开")
    elif str(status) not in ("200", "302"):
        log("[隧道] 地址暂不可达，稍等几秒再刷新；也可能本机到公网的回环被限制")
    log("[隧道] 注意：临时公开地址，ComfyUI 没有登录鉴权，用完及时 cs.stop()")
    return url


# ---------------------------------------------------------------------------
# 6. 组合入口
# ---------------------------------------------------------------------------


def setup_all(groups=None, do_download=True, do_launch=True, port=None, force=False, tunnel=None, ask_token=None) -> None:
    """一键：检查环境 -> 装 ComfyUI -> 下模型 -> 校验 -> 启动。"""
    selected = [g for g in (groups or DOWNLOAD_GROUPS) if g in GROUPS]
    print_plan(selected)
    check_env()
    ensure_comfyui()
    install_requirements()
    if "tts" in selected:
        # CustomVoice 是独立自定义节点，不装的话工作流提交必定 400 missing_node_type(Qwen3Loader)
        ensure_qwen3_tts_node(restart=do_launch)
    if do_download:
        download_models(selected, force=force, ask_token=ask_token)
        verify_models(selected)
    if do_launch:
        launch(port=port, wait=True, tunnel=tunnel)


def status() -> None:
    """打印当前启动状态。"""
    alive = _PROC is not None and _PROC.poll() is None
    log("ComfyUI 进程: " + ("运行中 PID=" + str(_PROC.pid) if alive else "未运行"))
    port = PORT
    log("端口探测  : http://127.0.0.1:" + str(port) + "/system_stats -> " + ("OK" if _http_ok(
        "http://127.0.0.1:" + str(port) + "/system_stats", 3.0) else "不可达"))
    log("公网隧道  : " + (_TUNNEL_URL if _TUNNEL_URL else "未启动（cs.start_tunnel()）"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_COMMANDS = ("plan", "all", "download", "verify", "tts", "check", "launch", "stop", "log", "status")


def in_notebook() -> bool:
    """是否运行在 IPython / Jupyter / Colab 内核里。"""
    try:
        from IPython import get_ipython

        return get_ipython() is not None
    except Exception:
        return False


def cli_args():
    """取命令行参数。

    Notebook 内核的 sys.argv 是它自己的启动参数（如
    ``colab_kernel_launcher.py -f /root/.local/share/jupyter/runtime/kernel-xxx.json``），
    直接交给 argparse 会报 invalid choice，所以这里只在第一个参数是已知子命令时才透传。
    """
    raw = [str(arg) for arg in sys.argv[1:]]
    if raw and raw[0] in _COMMANDS:
        return raw
    if in_notebook():
        return []
    return raw


def main(argv=None) -> int:
    if argv is None:
        argv = cli_args()
    parser = argparse.ArgumentParser(description="ComfyUI + Qwen-Image-2.1 + MiniMax H3 一键部署")
    sub = parser.add_subparsers(dest="cmd")

    p_plan = sub.add_parser("plan", help="查看下载计划")
    p_plan.add_argument("--groups", nargs="+", default=DOWNLOAD_GROUPS)

    p_all = sub.add_parser("all", help="检查+安装+下载+启动")
    p_all.add_argument("--groups", nargs="+", default=DOWNLOAD_GROUPS)
    p_all.add_argument("--no-download", action="store_true")
    p_all.add_argument("--no-launch", action="store_true")
    p_all.add_argument("--port", type=int, default=PORT)
    p_all.add_argument("--force", action="store_true", help="磁盘不够也强行下载")
    p_all.add_argument("--no-ask-token", action="store_true", help="不提示输入 HF token")

    p_dl = sub.add_parser("download", help="只下载模型")
    p_dl.add_argument("--groups", nargs="+", default=DOWNLOAD_GROUPS)
    p_dl.add_argument("--workers", type=int, default=HF_MAX_WORKERS)
    p_dl.add_argument("--force", action="store_true", help="磁盘不够也强行下载")
    p_dl.add_argument("--no-ask-token", action="store_true", help="不提示输入 HF token")

    p_verify = sub.add_parser("verify", help="只校验模型")
    p_verify.add_argument("--groups", nargs="+", default=DOWNLOAD_GROUPS)

    p_tts = sub.add_parser("tts", help="一键装 Qwen3-TTS CustomVoice（节点+模型+重启+验证）")
    p_tts.add_argument("--port", type=int, default=PORT)
    p_tts.add_argument("--no-download", action="store_true", help="只装节点，不下模型")
    p_tts.add_argument("--no-restart", action="store_true", help="装完不重启也不验证节点")
    p_tts.add_argument("--force", action="store_true", help="已存在时也 git pull 更新")
    p_tts.add_argument("--no-wait", action="store_true", help="启动后不等就绪")

    p_check = sub.add_parser("check", help="核对工作流引用的模型文件是否齐全")
    p_check.add_argument("workflow", help="工作流 json 路径")
    p_check.add_argument("--download", action="store_true", help="缺失的自动从已知仓库补齐")
    p_check.add_argument("--repo", nargs="+", default=None, help="指定搜索/下载的仓库")

    p_launch = sub.add_parser("launch", help="启动 ComfyUI")
    p_launch.add_argument("--port", type=int, default=PORT)
    p_launch.add_argument("--no-wait", action="store_true")
    p_launch.add_argument("--timeout", type=int, default=1800)
    p_launch.add_argument("--tunnel", default=TUNNEL)
    p_launch.add_argument("--args", nargs="*", default=None, help="透传给 main.py 的额外参数")

    sub.add_parser("stop", help="停止 ComfyUI")

    p_log = sub.add_parser("log", help="查看日志尾部")
    p_log.add_argument("--lines", type=int, default=60)

    sub.add_parser("status", help="运行状态")

    args = parser.parse_args(argv)
    cmd = args.cmd or "all"

    if cmd == "plan":
        print_plan(args.groups)
    elif cmd == "all":
        setup_all(
            groups=getattr(args, "groups", None),
            do_download=not getattr(args, "no_download", False),
            do_launch=not getattr(args, "no_launch", False),
            port=getattr(args, "port", PORT),
            force=getattr(args, "force", False),
            ask_token=False if getattr(args, "no_ask_token", False) else None,
        )
    elif cmd == "download":
        download_models(
            args.groups,
            workers=args.workers,
            force=args.force,
            ask_token=False if args.no_ask_token else None,
        )
        verify_models(args.groups)
    elif cmd == "verify":
        verify_models(args.groups)
    elif cmd == "tts":
        setup_customvoice(
            port=getattr(args, "port", PORT),
            download=not getattr(args, "no_download", False),
            restart=not getattr(args, "no_restart", False),
            force=getattr(args, "force", False),
            wait=not getattr(args, "no_wait", False),
        )
    elif cmd == "check":
        if getattr(args, "download", False):
            return 0 if ensure_workflow_models(args.workflow, repos=args.repo) else 1
        return 0 if check_workflow(args.workflow) else 1
    elif cmd == "launch":
        launch(port=args.port, wait=not args.no_wait, timeout=args.timeout, extra_args=args.args, tunnel=args.tunnel)
    elif cmd == "stop":
        stop()
    elif cmd == "log":
        tail_log(args.lines)
    elif cmd == "status":
        status()
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    _argv = cli_args()
    if not _argv and in_notebook():
        log("=" * 74)
        log("检测到在 Notebook 单元格里直接运行本文件（没有命令行参数）")
        log("按默认流程执行：环境检查 -> 安装 ComfyUI -> 下载模型 -> 校验 -> 启动")
        log("只要其中一步时，请 import 本模块后用对应函数，例如：")
        log("    import comfyui_qwen21_h3_setup as cs")
        log("    cs.download_models(['h3'])")
        log("=" * 74)
        setup_all(groups=DOWNLOAD_GROUPS, do_download=True, do_launch=True)
    else:
        sys.exit(main(_argv))