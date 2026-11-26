"""`/chat` → Agent Runtime 的路由判定（Phase B B6）。

只匹配 Phase B 支持的目标类型（B7 模板）：
- generate_assets：批量生成项目公共资产（角色/场景/道具）
- generate_keyframes：批量生成分镜图
- continue：继续上次的任务（不生成新计划，只对 active Goal tick）

判定依据（intent_router 的实际意图/子类）：
- intent.intent == "generate_request"
  - subcategory == "asset.generate" 且是批量/全量语义 → generate_assets
  - 内容含「分镜图 / keyframe」→ generate_keyframes
  - 单个实体 / 单个镜头（entities 带 entity_id / shot_id，或 subcategory 为
    shot.generate / video.generate / audio.generate）→ 不接管，仍走现有审批/引导流程
- 内容含「继续上次的任务 / 继续任务 / 接着上次」→ continue
- 开关关闭、会话无 project_id → None
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings

_ASSET_ALL_KEYWORDS = ("所有", "全部", "批量", "每个", "各个", "所有角色", "所有场景", "所有道具")
_KEYFRAME_KEYWORDS = ("分镜图", "keyframe", "分镜画面")
_CONTINUE_KEYWORDS = ("继续上次", "继续任务", "接着上次", "继续刚才")
_SCOPE_KEYWORDS = {"character": ("角色", "人物"), "scene": ("场景",), "prop": ("道具",)}
# 显式实体/镜头 ID：单个目标生成仍走既有随手生成/审批流程，不交给 Runtime
_SINGLE_TARGET_ID_RE = re.compile(r"\b(?:chr|scn|prp|sht)_[A-Za-z0-9]+")
# 「第 1 镜 / 第3集第2镜」这类单镜头表述
_SINGLE_SHOT_RE = re.compile(r"第\s*\d+\s*镜")


@dataclass
class RuntimeRoute:
    goal_type: str
    target_stage: str
    scope: str = "all"


def _detect_scope(content: str) -> str:
    for scope, words in _SCOPE_KEYWORDS.items():
        if any(w in content for w in words):
            return scope
    return "all"


def _has_single_target(intent: Any, entities: dict, text: str) -> bool:
    """单个实体/镜头目标的生成请求：不交给 Runtime（仍走现有随手生成或审批流程）。"""
    if entities.get("entity_id") or entities.get("shot_id") or entities.get("asset_id"):
        return True
    if _SINGLE_TARGET_ID_RE.search(text or ""):
        return True
    sub = getattr(intent, "subcategory", "") or ""
    return sub in ("shot.generate", "video.generate", "audio.generate")


def should_use_runtime(
    intent: Any, resolved: Any, content: str, *, project_id: str | None = None
) -> RuntimeRoute | None:
    """返回 RuntimeRoute 表示接管；None 表示保持原有 /chat 行为。"""
    if not bool(getattr(get_settings(), "agent_runtime_chat_enabled", False)):
        return None
    project_id = project_id or getattr(resolved, "project_id", None)
    if not project_id:
        return None

    text = content or ""
    if any(k in text for k in _CONTINUE_KEYWORDS):
        return RuntimeRoute(goal_type="continue", target_stage="continue", scope="")

    if getattr(intent, "intent", "") != "generate_request":
        return None

    entities = getattr(intent, "entities", None) or {}
    if _has_single_target(intent, entities, text):
        return None

    # 单镜头表述（「为第 1 镜生成关键帧」）是单目标请求，不进 Runtime；
    # 只有「所有/全部分镜图」这类批量语义才交给模板计划。
    if _SINGLE_SHOT_RE.search(text) and not any(k in text for k in _ASSET_ALL_KEYWORDS):
        return None

    if any(k in text for k in _KEYFRAME_KEYWORDS):
        scope = "all"
        return RuntimeRoute(goal_type="generate_keyframes", target_stage="keyframe", scope=scope)

    sub = getattr(intent, "subcategory", "") or ""
    if sub == "asset.generate" or any(k in text for k in _ASSET_ALL_KEYWORDS):
        return RuntimeRoute(
            goal_type="generate_assets",
            target_stage="asset",
            scope=_detect_scope(text),
        )
    return None
