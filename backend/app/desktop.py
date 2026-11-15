"""Windows 桌面版启动器（PyInstaller 打包入口）。

职责：
- 数据目录固定到 %APPDATA%\\DirectorLab，避免写入只读的程序目录
- 把打包内的 ffmpeg/ffprobe 目录注入 PATH（业务代码用 shutil.which 探测）
- 以子进程方式启动 worker（worker 在主线程注册信号，不能放进线程）
- 启动 uvicorn 并自动打开浏览器；未配置 ComfyUI 时直接进入设置页
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_DIR_NAME = "DirectorLab"
DEFAULT_PORT = 8765
WORKER_ARG = "--worker"

_LOG_STREAM = None


def _is_frozen() -> bool:
    return bool(getattr(sys, "_MEIPASS", None))


def _bundle_dir() -> Path:
    """打包后的资源根目录；开发态为 backend/ 目录。"""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle)
    return Path(__file__).resolve().parents[1]


def _app_root() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    root = Path(base) / APP_DIR_NAME
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return root


def _redirect_output(root: Path) -> None:
    """窗口模式（无控制台）下 sys.stdout/stderr 为 None，统一重定向到日志文件。"""
    global _LOG_STREAM
    if sys.stdout is not None and sys.stderr is not None:
        return
    _LOG_STREAM = open(root / "logs" / "desktop.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _LOG_STREAM
    if sys.stderr is None:
        sys.stderr = _LOG_STREAM


def _prepare_environment() -> Path:
    root = _app_root()
    data_dir = root / "data"
    bundle = _bundle_dir()

    # ./data 类相对路径（runtime_settings.json、默认 SQLite 路径）统一落到 APPDATA
    os.chdir(root)

    bin_dir = bundle / "bin"
    if bin_dir.is_dir():
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    os.environ.setdefault("DLAB_DATA_DIR", str(data_dir))
    os.environ.setdefault("DLAB_DATABASE_URL", f"sqlite:///{(data_dir / 'director_lab.db').as_posix()}")
    os.environ.setdefault("DLAB_MEDIA_DIR", str(data_dir / "media"))
    workflows = bundle / "data" / "workflows"
    if workflows.is_dir():
        os.environ.setdefault("DLAB_WORKFLOW_DIR", str(workflows))
    return root


def _pick_port(start: int = DEFAULT_PORT, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"端口 {start}-{start + tries - 1} 均被占用，请关闭占用程序后重试")


def _start_worker() -> subprocess.Popen:
    cmd = [sys.executable, WORKER_ARG] if _is_frozen() else [sys.executable, "-m", "app.worker.scheduler"]
    return subprocess.Popen(cmd, cwd=os.getcwd(), stdout=sys.stdout, stderr=sys.stderr)


def _worker_after_ready(port: int, holder: dict) -> None:
    """等 API 端口可连接后再拉起 worker。

    首次运行需要执行全部数据库迁移，worker 若在迁移期间启动会抢先建库/查表，
    造成首次启动失败，因此必须等服务真正就绪。
    """
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                holder["proc"] = _start_worker()
                return
        time.sleep(0.5)


def _fatal(message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Director Lab 启动失败", 0x10)
    except Exception:  # noqa: BLE001
        print(message, file=sys.stderr)


def _open_url(port: int) -> str:
    from app.core.config import get_settings

    cfg = get_settings()
    if not (cfg.comfyui_base_url or cfg.comfyui_instances_json):
        return f"http://127.0.0.1:{port}/settings"
    return f"http://127.0.0.1:{port}/"


def main() -> int:
    root = _prepare_environment()
    _redirect_output(root)

    if WORKER_ARG in sys.argv:
        from app.worker.scheduler import run_worker

        run_worker()
        return 0

    import uvicorn

    from app.main import app

    port = _pick_port()
    holder: dict = {}
    threading.Thread(target=_worker_after_ready, args=(port, holder), daemon=True).start()
    threading.Timer(1.8, lambda: webbrowser.open(_open_url(port))).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info", access_log=False)
    finally:
        worker = holder.get("proc")
        if worker is not None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        _fatal(f"应用启动失败：{exc}")
        raise
