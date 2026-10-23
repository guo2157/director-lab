"""业务错误：与 API.md §2 错误码表对齐。

路由层用 raise biz_error(code=..., http=..., message=...) 抛出，
全局异常处理器统一转换为 {"detail": {"code", "message", ...}} 结构。
"""
from fastapi import HTTPException


def biz_error(*, code: str, http: int, message: str, **extra) -> HTTPException:
    detail = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=http, detail=detail)


# --- 常用错误快捷构造 ---

def project_not_found() -> HTTPException:
    return biz_error(code="project_not_found", http=404, message="项目不存在")


def scene_not_found() -> HTTPException:
    return biz_error(code="scene_not_found", http=404, message="场景不存在")


def shot_not_found() -> HTTPException:
    return biz_error(code="shot_not_found", http=404, message="镜头不存在")


def character_not_found() -> HTTPException:
    return biz_error(code="character_not_found", http=404, message="角色不存在")


def prop_not_found() -> HTTPException:
    return biz_error(code="prop_not_found", http=404, message="道具不存在")


def invalid_state_transition(message: str) -> HTTPException:
    return biz_error(code="invalid_state_transition", http=409, message=message)


def invalid_request(message: str) -> HTTPException:
    return biz_error(code="invalid_request", http=400, message=message)


def asset_in_use(message: str) -> HTTPException:
    return biz_error(code="asset_in_use", http=409, message=message)
