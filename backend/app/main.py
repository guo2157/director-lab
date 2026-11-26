"""FastAPI 应用入口。

约定（API.md）：
- 统一错误格式：{"detail": {"code": "...", "message": "...", "reason": "..."}}
- 长耗时操作返回 202 + job 对象（阶段 4 实现）
"""
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import sys
import threading

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import (
    bindings,
    audio,
    voice_matching,
    agent,
    agent_loop,
    analysis,
    asset_plans,
    assets,
    characters,
    compose,
    dependencies,
    events,
    generate,
    jobs,
    progress,
    projects,
    props,
    regeneration,
    scenes,
    script,
    series,
    settings,
    shots,
    storyboard_plan,
    tasks,
    workflows,
    workspace,
)
from app.core.config import get_settings


def _startup_selfcheck() -> dict:
    """启动自检（D-12 零配置启动）：自动迁移 + 依赖检测，结果记入日志，不阻断启动。"""
    import logging
    import shutil
    from pathlib import Path

    logger = logging.getLogger("director-lab.startup")
    report: dict = {"migrated": False, "ffmpeg": bool(shutil.which("ffmpeg")), "comfyui": None}

    # 1. 自动执行 alembic 迁移（幂等；单机单进程定位下无并发风险，见 DECISIONS.md D-12）
    try:
        from alembic.config import Config

        from alembic import command

        alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
        if alembic_ini.exists():
            cfg = Config(str(alembic_ini))
            cfg.set_main_option("script_location", str(alembic_ini.parent / "alembic"))
            command.upgrade(cfg, "head")
            report["migrated"] = True
    except Exception as e:  # 迁移失败不阻断启动，由健康检查暴露
        logger.warning("自动迁移失败（不阻断启动）: %s", e)

    # 2. ComfyUI 可达性（不可达仅告警，真实生成时才硬性依赖；Mock 模式下跳过）
    cfg = get_settings()
    if not cfg.render_mock:
        try:
            from app.integrations.comfyui import ComfyUIClient, ComfyUIError
            from app.services.workflow.runtime_checker import default_base_url

            # 启用多实例后 comfyui_base_url 会被清空，真实地址在实例池里
            base_url = default_base_url()
            if not base_url:
                raise ComfyUIError("comfy_unconfigured", "未配置 ComfyUI 实例")
            ComfyUIClient(base_url=base_url).test_connection()
            report["comfyui"] = "ok"
        except ComfyUIError as e:
            logger.warning("ComfyUI 不可达（生成任务将失败，可设 DLAB_RENDER_MOCK=true 演示）: %s", e.message)
            report["comfyui"] = e.code
    else:
        report["comfyui"] = "mock"

    if not report["ffmpeg"]:
        logger.warning("未检测到 ffmpeg，合成/导出与 mock 视频生成将不可用")
    logger.info("启动自检完成: %s", report)
    return report


_embedded_worker: dict = {"thread": None, "stop": None}


def _start_embedded_worker() -> None:
    """随 API 进程自动拉起调度线程（无需再手动启动独立 worker 进程）。

    跨进程文件锁保证同一数据目录下只有一个消费者：独立 worker 或另一个 API
    实例正在消费时，本进程会等待锁释放后接管，不会重复取任务。
    迁移在 _startup_selfcheck 中已完成，此时启动调度不会与建表竞争。
    """
    if not get_settings().worker_embedded:
        return
    stop = threading.Event()

    def _run() -> None:
        from app.worker import scheduler as sched

        while not stop.is_set():
            if sched.acquire_worker_lock():
                break
            # 独立 worker 已占用（或热重载时新旧进程交替），稍后重试接管
            stop.wait(3.0)
        if stop.is_set():
            return
        try:
            sched.run_worker(install_signal_handlers=False)
        except Exception:  # noqa: BLE001
            logging.getLogger("director-lab.startup").exception("内嵌调度进程异常退出")
        finally:
            sched.release_worker_lock()

    thread = threading.Thread(target=_run, name="dlab-scheduler", daemon=True)
    thread.start()
    _embedded_worker["thread"] = thread
    _embedded_worker["stop"] = stop


def _stop_embedded_worker() -> None:
    """停止内嵌调度：给在途任务留出有界退出时间，避免被硬杀后重复执行。"""
    thread = _embedded_worker.get("thread")
    stop = _embedded_worker.get("stop")
    if stop is not None:
        stop.set()
    if thread is None:
        return
    from app.worker import scheduler as sched

    sched.request_stop()
    grace = float(getattr(get_settings(), "worker_shutdown_grace_seconds", 20.0) or 20.0)
    thread.join(timeout=grace + 5.0)
    _embedded_worker["thread"] = None
    _embedded_worker["stop"] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    _startup_selfcheck()
    _start_embedded_worker()
    try:
        yield
    finally:
        _stop_embedded_worker()


def _frontend_dist() -> Path | None:
    """前端构建产物目录：打包后位于 _MEIPASS/frontend_dist，开发时用 frontend/dist。"""
    candidates = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "frontend_dist")
    candidates.append(Path(__file__).resolve().parents[2] / "frontend" / "dist")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def _mount_frontend(app: FastAPI) -> None:
    """托管前端单页应用：静态资源直出，其余路径回退 index.html 交给 vue-router。"""
    dist = _frontend_dist()
    if dist is None:
        return
    dist_root = dist.resolve()
    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise StarletteHTTPException(
                status_code=404,
                detail={"code": "not_found", "message": "接口不存在"},
            )
        target = (dist / full_path).resolve()
        if full_path and target.is_file() and dist_root in target.parents:
            return FileResponse(target)
        return FileResponse(dist / "index.html")


def create_app() -> FastAPI:
    cfg = get_settings()
    app = FastAPI(title=cfg.app_name, debug=cfg.debug, lifespan=lifespan)
    app.include_router(projects.router)
    app.include_router(agent.router)
    app.include_router(agent_loop.router)
    app.include_router(assets.router)
    app.include_router(asset_plans.router)
    app.include_router(generate.router)
    app.include_router(regeneration.router)
    app.include_router(characters.router)
    app.include_router(props.router)
    app.include_router(scenes.router)
    app.include_router(shots.router)
    app.include_router(bindings.router)
    app.include_router(dependencies.router)
    app.include_router(analysis.router)
    app.include_router(storyboard_plan.router)
    app.include_router(script.router)
    app.include_router(series.router)
    app.include_router(jobs.router)
    app.include_router(progress.router)
    app.include_router(events.router)
    app.include_router(tasks.router)
    app.include_router(workspace.router)
    app.include_router(compose.router)
    app.include_router(workflows.router)
    app.include_router(audio.router)
    app.include_router(voice_matching.router)
    app.include_router(settings.router)
    app.include_router(settings.health_router)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        detail.setdefault("code", "http_error")
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # exc.errors() 的 ctx 可能携带 field_validator 抛出的原始 ValueError 对象（不可 JSON 序列化），
        # 统一转为字符串，避免 422 响应本身因序列化失败退化为 500
        def _safe_err(err: dict) -> dict:
            out = dict(err)
            ctx = out.get("ctx")
            if isinstance(ctx, dict):
                out["ctx"] = {k: str(v) if isinstance(v, BaseException) else v for k, v in ctx.items()}
            return out

        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "invalid_request",
                    "message": "请求参数校验失败",
                    "errors": [_safe_err(e) for e in exc.errors()],
                }
            },
        )

    # /api/health 由 settings.health_router 统一提供（含 database/comfyui/worker/disk 分项状态）

    _mount_frontend(app)

    return app


app = create_app()
