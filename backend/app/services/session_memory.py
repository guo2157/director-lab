"""会话记忆服务：短期记忆（近期完整对话）+ 长期记忆（LLM 压缩的会话摘要）。

设计约束（AGENTS.md P0）：
- 模型只负责摘要压缩，压缩时机与窗口划分由确定性代码控制
- LLM 压缩失败不阻断对话：保留旧摘要，短期窗口照常工作
- 摘要只覆盖 user/assistant 消息（tool_event/approval 不进入对话上下文）
- summary_upto_seq 记录摘要已覆盖到的消息 seq，短期窗口从该 seq 之后开始

流程：
1. 主对话加载短期窗口（近 _CHAT_HISTORY_WINDOW 条）后，检查窗口外是否还有
   未摘要的消息（seq < 窗口首条 seq 且 seq > summary_upto_seq）
2. 未摘要消息数达到 SUMMARY_TRIGGER_THRESHOLD 时，触发 LLM 压缩：
   旧摘要 + 未摘要旧消息 → 新摘要，写入 session.summary，
   更新 session.summary_upto_seq 为本批最后一条消息的 seq
3. 上下文构建时携带『摘要 + 近期对话』，保证语义判定与主对话上下文齐全
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.llm import LLMClient, LLMError
from app.models import AgentMessage, AgentSession

# 窗口外未摘要消息达到该数量时触发 LLM 压缩（避免每条消息都压缩，控制成本）
SUMMARY_TRIGGER_THRESHOLD = 10
# 单条消息进入压缩 prompt 的最大字符数（超长剧本正文截断，保留关键信息）
_SUMMARY_MSG_MAX_CHARS = 1000
# 摘要最大字符数（LLM 输出超长时截断保护）
_SUMMARY_MAX_CHARS = 4000

_SUMMARY_SYSTEM_PROMPT = (
    "你是会话摘要助手。请将提供的旧会话摘要（如有）与历史对话压缩为一份新的会话摘要，"
    "供后续对话作为长期记忆使用。要求：\n"
    "1. 保留关键事实：用户目标、项目/故事名称、已确认的决策（风格、画幅、剧本来源等）、"
    "工作流进展、重要结论与未决问题\n"
    "2. 丢弃寒暄、重复内容与无关细节\n"
    "3. 用简洁的中文条目式输出，不超过 600 字\n"
    "4. 只输出摘要正文，不要任何前后缀说明"
)


def _load_unsummarized(
    db: Session, session_id: str, before_seq: int, upto_seq: int
) -> list[AgentMessage]:
    """加载窗口外未摘要的 user/assistant 消息（旧→新）。

    before_seq：短期窗口首条消息 seq（仅压缩 seq < before_seq 的消息）
    upto_seq：摘要已覆盖到的 seq（仅压缩 seq > upto_seq 的消息）
    """
    q = (
        select(AgentMessage)
        .where(
            AgentMessage.session_id == session_id,
            AgentMessage.role.in_(["user", "assistant"]),
            AgentMessage.seq < before_seq,
            AgentMessage.seq > upto_seq,
        )
        .order_by(AgentMessage.seq.asc())
    )
    return list(db.scalars(q).unique().all())


def _build_summary_prompt(
    old_summary: str | None, messages: list[AgentMessage]
) -> str:
    parts: list[str] = []
    if old_summary:
        parts.append("【旧会话摘要】\n" + old_summary)
    lines = [
        f"{m.role == 'user' and '用户' or '助手'}：{m.content[:_SUMMARY_MSG_MAX_CHARS]}"
        for m in messages
    ]
    parts.append("【待压缩的历史对话（旧→新）】\n" + "\n".join(lines))
    parts.append("请输出新的会话摘要。")
    return "\n\n".join(parts)


def maybe_compress_session(
    db: Session, session: AgentSession, window_first_seq: int
) -> bool:
    """窗口外未摘要消息达到阈值时，LLM 压缩为摘要并落库。

    返回是否触发了压缩。LLM 失败时保留旧摘要不阻断对话（返回 False）。
    调用方负责 commit（与主流程同一事务）。
    """
    upto_seq = session.summary_upto_seq or 0
    pending = _load_unsummarized(db, session.id, window_first_seq, upto_seq)
    if len(pending) < SUMMARY_TRIGGER_THRESHOLD:
        return False
    prompt = _build_summary_prompt(session.summary, pending)
    try:
        summary = LLMClient().chat(_SUMMARY_SYSTEM_PROMPT, prompt, json_mode=False)
    except LLMError:
        # 压缩失败不阻断对话：短期窗口照常工作，下次达到条件再重试
        return False
    if not summary or not summary.strip():
        return False
    session.summary = summary.strip()[:_SUMMARY_MAX_CHARS]
    # 覆盖到本批最后一条消息（窗口内消息保持完整原文）
    session.summary_upto_seq = max(m.seq for m in pending)
    return True


def build_summary_context(session: AgentSession) -> str | None:
    """构建注入对话上下文的摘要文本（无摘要时返回 None）。"""
    if not session.summary:
        return None
    return "以下是本会话更早历史的摘要（长期记忆，供理解上下文使用）：\n" + session.summary
