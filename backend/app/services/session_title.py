"""会话标题自动生成：首轮对话后由 LLM 归纳一句短语作为会话名。

设计约束：
- 只在「标题仍是默认值」时触发，用户手动改过的标题不覆盖
- 失败必须静默降级到本地摘要（截断首条用户消息），绝不阻塞聊天主链路
- LLM 调用在数据库事务之外；写回标题时用独立的短事务
- 结果做后处理：去引号/书名号、去标点结尾、截断到 24 字符
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import database as db_module
from app.integrations.llm import LLMClient, LLMError
from app.models.agent import AgentMessage, AgentSession

# 视为「未命名」的标题：与模型默认值、前端创建值保持一致
DEFAULT_TITLES = {"", "新会话", "未命名会话", "新对话"}

# 标题长度上限（字符）：下拉框宽度有限，超过 24 字在 392px 面板里会被截断
MAX_TITLE_LEN = 24

_NAMING_PROMPT = """你是一个会话命名器。根据下面这轮对话，给会话起一个简短标题。

要求：
1. 只输出标题本身，不要引号、不要书名号、不要句号、不要解释
2. 4–12 个汉字，最长不超过 16 个汉字
3. 概括用户想做的事，不要照抄原句
4. 用中文，动词开头更自然（如「解析剧本」「排查生图失败」）

用户：{user}
助手：{assistant}"""


def is_default_title(title: str | None) -> bool:
    """标题是否仍为系统默认值（即用户没有手动命名过）。"""
    return (title or "").strip() in DEFAULT_TITLES


def _clean_title(raw: str) -> str:
    """清洗 LLM 输出：去包裹符号、去首尾标点、压平换行、截断。"""
    text = (raw or "").strip()
    # 只取第一行（LLM 有时会多给一行解释）
    text = text.splitlines()[0].strip() if text else ""
    # 去掉成对的包裹符号：引号、书名号、方括号、星号
    text = text.strip("\"'“”‘’《》〈〉【】[]（）()「」『』*` ")
    # 去掉结尾标点
    text = re.sub(r"[。．.！!？?，,、；;：:\s]+$", "", text)
    # 去掉常见前缀噪音
    text = re.sub(r"^(标题|会话标题|标题[:：])\s*[:：]?\s*", "", text)
    if len(text) > MAX_TITLE_LEN:
        text = text[:MAX_TITLE_LEN].rstrip()
    return text


def _fallback_title(user_text: str) -> str:
    """LLM 不可用时的本地摘要：取首行有效内容截断。"""
    for line in (user_text or "").splitlines():
        line = line.strip()
        if line:
            return line[:20] + ("…" if len(line) > 20 else "")
    return "新会话"


def _build_llm_title(user_text: str, assistant_text: str) -> str:
    """调 LLM 生成标题；任何异常都降级到本地摘要。"""
    prompt = _NAMING_PROMPT.format(
        user=user_text[:600],
        assistant=(assistant_text or "")[:400],
    )
    try:
        # 标题很短，max_tokens 压到 64，避免浪费配额与延迟
        raw = LLMClient().chat(
            "你是一个精确的会话标题生成器，只输出标题文本。",
            prompt,
            json_mode=False,
            max_tokens=64,
        )
    except LLMError:
        return _fallback_title(user_text)
    except Exception:  # noqa: BLE001 - 命名失败不得影响聊天主链路
        return _fallback_title(user_text)
    title = _clean_title(raw)
    # LLM 返回空、或返回了明显非标题内容（过长/含冒号解释）时降级
    if not title or len(title) > MAX_TITLE_LEN:
        return _fallback_title(user_text)
    return title


def generate_session_title(session_id: str) -> str | None:
    """为会话生成并写回标题。

    返回新标题；不需要命名（标题已被用户改过 / 会话不存在）时返回 None。
    使用独立 DB 会话：调用方通常已提交并关闭自己的事务。
    """
    db: Session = db_module.SessionLocal()
    try:
        s = db.get(AgentSession, session_id)
        if s is None or not is_default_title(s.title):
            return None

        # 取首条 user 消息与其后的首条 assistant 消息作为命名依据
        user_msg = db.scalar(
            select(AgentMessage)
            .where(AgentMessage.session_id == session_id, AgentMessage.role == "user")
            .order_by(AgentMessage.seq)
            .limit(1)
        )
        if user_msg is None or not (user_msg.content or "").strip():
            return None
        assistant_msg = db.scalar(
            select(AgentMessage)
            .where(
                AgentMessage.session_id == session_id,
                AgentMessage.role == "assistant",
                AgentMessage.seq > user_msg.seq,
            )
            .order_by(AgentMessage.seq)
            .limit(1)
        )

        title = _build_llm_title(user_msg.content, assistant_msg.content if assistant_msg else "")
        if not title or not is_default_title(s.title):
            # 期间被用户改名了，放弃覆盖
            return None
        s.title = title
        db.commit()
        return title
    except Exception:  # noqa: BLE001 - 命名是尽力而为，任何失败都不外抛
        db.rollback()
        return None
    finally:
        db.close()
