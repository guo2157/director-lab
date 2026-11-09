"""Intent Resolver：意图融合与消歧（意图识别改造 Phase 2）。
设计约束（AGENTS.md P0 / 意图识别架构方案 §6）：
- Rule Engine 与 LLM Classifier 不允许各自直接执行工具，
  最终只能由 Intent Resolver 输出经过校验的执行请求
- 实体引用解析优先级：显式 ID（消息文本）> Focus Context（上下文焦点）> 澄清
- 所有实体 ID 必须经过数据库验证（存在 + 属于当前项目），模型编造的 ID 一律拒绝
- 置信度不是执行授权：即使高置信度也必须校验项目/实体/参数/权限
- 目标不唯一或无法定位时输出 requires_clarification，不猜测执行
输出 ResolvedIntent：
- intent / action / risk_level / confidence（继承自 IntentResult）
- target: {entity_type, entity_id, entity_name}（经数据库验证的目标实体，可空）
- resolution_path: explicit_id / focus_context / none（目标解析路径，供审计）
- requires_clarification + clarification_question（需澄清时）
- focus_updated: 是否更新了会话焦点（调用方负责落库）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.models.agent import AgentSession
from app.services import context_manager
from app.services.generate_intent import resolve_entity, resolve_shot
from app.services.intent_router import IntentResult, build_clarification_question


@dataclass
class ResolvedIntent:
    """统一标准化意图：Intent Resolver 的唯一输出。"""

    intent: str
    reason: str
    risk_level: str = "low"
    confidence: float = 1.0
    # 细粒度子类与分级置信度（Phase 1：继承自 IntentResult 的统一结构化输出）
    subcategory: str = ""
    confidence_tier: str = "execute"
    route_exit: str = "general_answer"
    # 经数据库验证的目标实体（可空：无实体目标的意图）
    target: dict | None = None
    # 目标解析路径：explicit_id / focus_context / none
    resolution_path: str = "none"
    requires_clarification: bool = False
    clarification_question: str | None = None
    # 澄清原因码：entity_unspecified / entity_ambiguous / entity_not_found /
    # entity_not_in_project / shot_ambiguous 等（供审计与测试）
    clarification_code: str | None = None
    # 是否更新了会话焦点（调用方据此持久化 context_state）
    focus_updated: bool = False
    # 原始意图识别结果（审计用）
    source: IntentResult | None = None

    def snapshot(self) -> dict:
        """经 Pydantic Schema 校验的结构化快照（Phase 1：统一结构化输出）。"""
        try:
            return ResolvedIntentSchema(
                intent=self.intent,
                subcategory=self.subcategory,
                reason=self.reason,
                risk_level=self.risk_level,
                confidence=self.confidence,
                confidence_tier=self.confidence_tier,
                route_exit=self.route_exit,
                target=self.target,
                resolution_path=self.resolution_path,
                requires_clarification=self.requires_clarification,
                clarification_question=self.clarification_question,
                clarification_code=self.clarification_code,
                focus_updated=self.focus_updated,
            ).model_dump()
        except ValidationError:
            # 校验失败不阻断主流程：降级为最小可用快照并标记 unsupported
            return {
                "intent": self.intent,
                "subcategory": self.subcategory,
                "confidence": self.confidence,
                "confidence_tier": self.confidence_tier,
                "route_exit": "unsupported",
            }


class ResolvedIntentSchema(BaseModel):
    """ResolvedIntent 的 Pydantic 校验视图（Phase 1：统一结构化意图输出）。"""

    intent: str
    subcategory: str = ""
    reason: str = ""
    risk_level: str = Field(default="low", pattern="^(low|medium|high)$")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    confidence_tier: str = Field(default="execute", pattern="^(execute|recheck|clarify)$")
    route_exit: str = Field(default="general_answer", pattern="^(general_answer|clarification|unsupported)$")
    target: dict | None = None
    resolution_path: str = Field(default="none", pattern="^(explicit_id|focus_context|none)$")
    requires_clarification: bool = False
    clarification_question: str | None = None
    clarification_code: str | None = None
    focus_updated: bool = False


# 需要目标实体定位的意图场景关键词（生成/修改/重新生成类）
_TARGET_REQUIRED_HINTS = ("重新生成", "再生成", "修改", "换", "调整", "重新做")


def _resolve_explicit_target(
    db: Session, project_id: str, text: str
) -> tuple[dict | None, str | None]:
    """从消息文本解析显式目标（镜头优先，其次实体 ID）。
    返回 (target, None) 或 (None, reason_code)。"""
    shot, shot_reason = resolve_shot(db, project_id, text)
    if shot is not None:
        return (
            {
                "entity_type": "shot",
                "entity_id": shot.id,
                "entity_name": getattr(shot, "name", None) or shot.id,
            },
            None,
        )
    if shot_reason and shot_reason != "shot_unspecified":
        # 给了镜头标识但无效：不回退焦点，交澄清
        return None, shot_reason
    entity, target_type, entity_reason = resolve_entity(db, project_id, text)
    if entity is not None:
        return (
            {
                "entity_type": target_type,
                "entity_id": entity.id,
                "entity_name": getattr(entity, "name", None) or entity.id,
            },
            None,
        )
    if entity_reason and entity_reason != "entity_unspecified":
        return None, entity_reason
    return None, None


def resolve_intent(
    db: Session,
    session: AgentSession,
    user_content: str,
    intent: IntentResult,
    *,
    current_seq: int = 0,
) -> ResolvedIntent:
    """融合意图识别结果与结构化上下文，输出统一标准化意图。

    决策顺序：
    1. unsafe / ambiguous / query 等无实体目标意图：直接透传（含澄清信息）
    2. 需要目标的意图（generate_request 等）：
       a. 显式 ID 定位（消息文本）
       b. 定位失败且消息含指代/返工关键词 → Focus Context 回退（经数据库验证）
       c. 均失败 → requires_clarification（不猜测执行）
    3. 定位成功：更新会话焦点（focus_updated=True，调用方持久化）
    """
    base = ResolvedIntent(
        intent=intent.intent,
        reason=intent.reason,
        risk_level=intent.risk_level,
        confidence=intent.confidence,
        subcategory=getattr(intent, "subcategory", "") or "",
        confidence_tier=getattr(intent, "confidence_tier", "execute") or "execute",
        route_exit=getattr(intent, "route_exit", "general_answer") or "general_answer",
        source=intent,
    )

    # 无实体目标的意图：直接透传
    if intent.intent not in {"generate_request", "pipeline_action"}:
        if intent.intent == "ambiguous":
            base.requires_clarification = True
            base.route_exit = "clarification"
            base.clarification_question = build_clarification_question(
                intent.missing_information or ["用户目标不够明确，请补充说明"]
            )
        return base

    project_id = session.project_id or ""
    if not project_id:
        # 无项目上下文：generate_request 透传（上层 _try_create_generate_approval
        # 支持自动创建项目，不拦截既有能力）；仅 pipeline_action 需先关联项目
        if intent.intent == "pipeline_action":
            base.requires_clarification = True
            base.route_exit = "clarification"
            base.clarification_code = "no_project_context"
            base.clarification_question = "当前会话未关联项目，请先选择或创建项目后再执行该操作。"
        return base

    # 1. 显式 ID 定位
    target, reason = _resolve_explicit_target(db, project_id, user_content)
    if target is not None:
        base.target = target
        base.resolution_path = "explicit_id"
        context_manager.update_focus(
            session,
            entity_type=target["entity_type"],
            entity_id=target["entity_id"],
            entity_name=target["entity_name"],
            updated_seq=current_seq,
        )
        base.focus_updated = True
        return base
    if reason is not None:
        # 给了标识但无效（不存在/不在项目内/歧义）：不回退焦点，交澄清
        base.requires_clarification = True
        base.route_exit = "clarification"
        base.clarification_code = reason
        base.clarification_question = _clarify_message(reason)
        return base

    # 2. Focus Context 回退：消息含指代/返工关键词且焦点有效
    needs_target = any(k in user_content for k in _TARGET_REQUIRED_HINTS)
    if needs_target:
        focus = context_manager.validate_focus(db, session, current_seq=current_seq)
        if focus is not None:
            base.target = {
                "entity_type": focus.entity_type,
                "entity_id": focus.entity_id,
                "entity_name": focus.entity_name,
            }
            base.resolution_path = "focus_context"
            # 焦点复用：刷新 updated_seq 延长有效期
            context_manager.update_focus(
                session,
                entity_type=focus.entity_type,
                entity_id=focus.entity_id,
                entity_name=focus.entity_name,
                updated_seq=current_seq,
            )
            base.focus_updated = True
            return base
        # 无有效焦点：交澄清（不猜测执行）
        base.requires_clarification = True
        base.route_exit = "clarification"
        base.clarification_code = "entity_unspecified"
        base.clarification_question = (
            "你想操作哪个对象？请提供对象 ID（如 chr_/scn_/prp_/sht_ 前缀），"
            "或在资产管理页/分镜看板中选中对象后再操作。"
        )
        return base

    # 3. 无目标关键词的生成/流程意图：保持原路由行为（GUIDE/PIPELINE 由上层处理）
    return base


def _clarify_message(code: str) -> str:
    """澄清原因码 → 用户可读消息（确定性映射，不经过 LLM）。"""
    messages = {
        "shot_not_found": "消息中提到的镜头不存在，请确认镜头 ID 或镜号。",
        "shot_not_in_project": "该镜头不属于当前项目，请确认目标镜头。",
        "shot_ambiguous": "镜号在项目内命中多个镜头，请提供具体镜头 ID。",
        "entity_not_found": "消息中提到的实体不存在，请确认实体 ID。",
        "entity_not_in_project": "该实体不属于当前项目，请确认目标对象。",
        "entity_unspecified": "你想操作哪个对象？请提供对象 ID（如 chr_/scn_/prp_/sht_ 前缀），"
        "或在资产管理页/分镜看板中选中对象后再操作。",
    }
    return messages.get(code, "无法确定操作对象，请补充说明。")
