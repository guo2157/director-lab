"""业务错误：与 API.md §2 错误码表对齐。

路由层用 raise biz_error(code=..., http=..., message=...) 抛出，
全局异常处理器统一转换为 {"detail": {"code", "message", ...}} 结构。
"""
from fastapi import HTTPException


def biz_error(*, code: str, http: int, message: str, **extra) -> HTTPException:
    detail = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=http, detail=detail)


def AppError(code: str, message: str, http: int, **extra) -> HTTPException:
    """兼容构造：位置参数 (code, message, http)，等价 biz_error。"""
    return biz_error(code=code, http=http, message=message, **extra)


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
def script_version_not_found() -> HTTPException:
    return biz_error(code="script_version_not_found", http=404, message="剧本版本不存在")
def script_version_conflict(message: str = "剧本版本与确认版本不一致") -> HTTPException:
    return biz_error(code="script_version_conflict", http=409, message=message)
def estimate_not_found() -> HTTPException:
    return biz_error(code="estimate_not_found", http=404, message="评估记录不存在")
def parse_version_not_found() -> HTTPException:
    return biz_error(code="parse_version_not_found", http=404, message="解析版本不存在")
def parse_version_conflict(message: str = "当前解析版本已变化，请刷新差异后重试") -> HTTPException:
    return biz_error(code="parse_version_conflict", http=409, message=message)
def job_not_found() -> HTTPException:
    return biz_error(code="job_not_found", http=404, message="任务不存在")
def invalid_request(message: str) -> HTTPException:
    return biz_error(code="invalid_request", http=400, message=message)


def invalid_request(message: str) -> HTTPException:
    return biz_error(code="invalid_request", http=400, message=message)


def asset_in_use(message: str) -> HTTPException:
    return biz_error(code="asset_in_use", http=409, message=message)
