"""
Workspace Navigation（Phase 2 导航与状态）

职责：
- 定义导航目标白名单（NAV_PAGE_WHITELIST）
- 关键词 → 路由名映射（与 intent_router 规则共享同一数据源）
- 校验请求参数合法性
- 提供错误码枚举

注意：不再维护两套散落的内联字典（原本 agent.py 里一份、规则里一份），
统一收口到此模块，任何需要导航映射或校验的地方只引这里。
"""

from enum import StrEnum


# ── 错误码枚举 ──

class NavErrorCode(StrEnum):
    """导航错误码，前端据此做用户提示"""
    OK = "nav_ok"
    INVALID_PAGE = "nav_invalid_page"
    MISSING_PROJECT = "nav_missing_project"
    INVALID_PARAMS = "nav_invalid_params"
    BLOCKED = "nav_blocked"
    FAILED = "nav_failed"


# ── 前端路由白名单 ──

NAV_PAGE_WHITELIST: set[str] = {
    "projects",
    "director",
    "script",
    "estimate",
    "parse",
    "storyboard-images",
    "storyboard",
    "assets",
    "compose",
    "jobs",
    "settings",
    "series-list",
}

# 需要项目上下文才能导航的路由名
PROJECT_REQUIRED_PAGES: set[str] = {
    "script",
    "estimate",
    "parse",
    "storyboard-images",
    "storyboard",
    "assets",
    "compose",
}


# ── 关键词 → 路由名映射（规则快路径与 chat 导航共用） ──

NAVIGATE_KEYWORD_TO_ROUTE: dict[str, str] = {
    "剧本": "script",
    "评估": "estimate",
    "解析": "parse",
    "分镜图": "storyboard-images",
    "分镜": "storyboard",
    "资产": "assets",
    "合成": "compose",
    "导出": "compose",
    "项目列表": "projects",
    "设置": "settings",
    "工作台": "director",
    "任务": "jobs",
    "系列": "series-list",
    "系列列表": "series-list",
}


def validate_route(route_name: str) -> NavErrorCode:
    """校验路由名是否在白名单内"""
    return NavErrorCode.OK if route_name in NAV_PAGE_WHITELIST else NavErrorCode.INVALID_PAGE


def needs_project(route_name: str) -> bool:
    """是否需要项目 id 参数"""
    return route_name in PROJECT_REQUIRED_PAGES


def resolve_from_text(text: str) -> tuple[str, str | None]:
    """从用户消息中解析导航关键词 → 路由名。
    未匹配时返回引导列表。
    """
    for keyword, route_name in NAVIGATE_KEYWORD_TO_ROUTE.items():
        if keyword in text:
            return keyword, route_name
    return "", None


def build_navigate_payload(text: str) -> tuple[str, dict]:
    """解析用户导航请求，返回结构化导航 payload（route_name + 确认文本）。

    规则：从消息文本中匹配第一个已知关键词，映射为前端路由名。
    未匹配时返回引导列表。
    """
    keyword, route_name = resolve_from_text(text)
    if route_name is not None:
        return (
            f"正在为您切换到『{keyword}』页面。",
            {
                "ok": True,
                "route_action": "navigate_workspace",
                "route_name": route_name,
                "route_label": keyword,
            },
        )
    # 未匹配：列出所有可导航页面
    pages = "、".join(sorted(NAV_PAGE_WHITELIST))
    return (
        f"可导航的页面：{pages}。请告诉我想去哪个页面，例如『去分镜页』或『打开资产库』。",
        {
            "ok": False,
            "route_action": "navigate_workspace",
            "route_name": None,
            "available_routes": sorted(NAV_PAGE_WHITELIST),
        },
    )
