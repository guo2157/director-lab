"""FastAPI 应用入口。

约定（API.md）：
- 统一错误格式：{"detail": {"code": "...", "message": "...", "reason": "..."}}
- 长耗时操作返回 202 + job 对象（阶段 4 实现）
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import (
    bindings,
    audio,
    agent,
    asset_plans,
    assets,
    characters,
    compose,
    events,
    generate,
    jobs,
    progress,
    projects,
    props,
    regeneration,
    scenes,
    script,
    settings,
    shots,
    tasks,
    workflows,
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

            ComfyUIClient().test_connection()
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    _startup_selfcheck()
    yield


def create_app() -> FastAPI:
    cfg = get_settings()
    app = FastAPI(title=cfg.app_name, debug=cfg.debug, lifespan=lifespan)

    app.include_router(projects.router)
    app.include_router(agent.router)
    app.include_router(assets.router)
    app.include_router(asset_plans.router)
    app.include_router(generate.router)
    app.include_router(regeneration.router)
    app.include_router(characters.router)
    app.include_router(props.router)
    app.include_router(scenes.router)
    app.include_router(shots.router)
    app.include_router(bindings.router)
    app.include_router(script.router)
    app.include_router(jobs.router)
    app.include_router(progress.router)
    app.include_router(events.router)
    app.include_router(tasks.router)
    app.include_router(compose.router)
    app.include_router(workflows.router)
    app.include_router(audio.router)
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

    return app


app = create_app()
