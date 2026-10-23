"""FastAPI 应用入口。

约定（API.md）：
- 统一错误格式：{"detail": {"code": "...", "message": "...", "reason": "..."}}
- 长耗时操作返回 202 + job 对象（阶段 4 实现）
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings

from app.api.v1 import characters, compose, events, jobs, projects, props, scenes, script, settings, shots, workflows


def create_app() -> FastAPI:
    cfg = get_settings()
    app = FastAPI(title=cfg.app_name, debug=cfg.debug)

    app.include_router(projects.router)
    app.include_router(characters.router)
    app.include_router(props.router)
    app.include_router(scenes.router)
    app.include_router(shots.router)
    app.include_router(script.router)
    app.include_router(jobs.router)
    app.include_router(events.router)
    app.include_router(compose.router)
    app.include_router(workflows.router)
    app.include_router(settings.router)
    app.include_router(settings.health_router)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        detail.setdefault("code", "http_error")
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "invalid_request",
                    "message": "请求参数校验失败",
                    "errors": exc.errors(),
                }
            },
        )

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "app": cfg.app_name, "version": "0.1.0"}

    return app


app = create_app()
