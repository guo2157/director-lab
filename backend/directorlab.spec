# -*- mode: python ; coding: utf-8 -*-
"""Director Lab Windows 单文件 exe 打包配置。

构建入口：scripts/build_windows.ps1
产物：dist_desktop/DirectorLab.exe（首次运行数据写入 %APPDATA%\\DirectorLab）
"""
from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_submodules

# DLAB_DESKTOP_CONSOLE=1 时输出带控制台的调试版本，便于排查启动问题
CONSOLE = os.environ.get("DLAB_DESKTOP_CONSOLE") == "1"

BACKEND = Path(SPECPATH).resolve()
REPO = BACKEND.parent
FRONTEND_DIST = REPO / "frontend" / "dist"
BIN_DIR = BACKEND / "packaging" / "bin"

datas = [
    (str(FRONTEND_DIST), "frontend_dist"),
    (str(BACKEND / "alembic.ini"), "."),
    (str(BACKEND / "alembic"), "alembic"),
    (str(BACKEND / "data" / "workflows"), "data/workflows"),
]
if BIN_DIR.is_dir():
    datas.append((str(BIN_DIR), "bin"))

hiddenimports = collect_submodules("app") + [
    "alembic",
    "sqlalchemy.dialects.sqlite",
    "pydantic_settings",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

a = Analysis(
    [str(BACKEND / "app" / "desktop.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DirectorLabConsole" if CONSOLE else "DirectorLab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    disable_windowed_traceback=False,
)
