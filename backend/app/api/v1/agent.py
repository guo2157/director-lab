"""导演台 Agent 会话接口：会话管理与消息持久化。

契约要点：
- 会话创建返回 201；project_id 若提供须真实存在（404 project_not_found）
- 会话删除为软删除（status=archived），消息保留，可恢复
- 消息追加：seq 会话内自增（max+1），并发安全由唯一索引 (session_id, seq) 兜底
- 消息列表按 seq 升序分页，支持 after_seq 增量加载
- 归档会话只读：追加消息返回 409 session_archived
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.models import AgentMessage, AgentSession, Project
from app.models.ids import agent_message_id, agent_session_id
from app.schemas.agent import (
    AgentMessageCreate,
    AgentMessageOut,
    AgentSessionCreate,
    AgentSessionOut,
    AgentSessionUpdate,
)
from app.services.errors import biz_error
from app.integrations.llm import LLMClient, LLMError

router = APIRouter(prefix="/api/agent/sessions", tags=["agent"])

# 聊天历史窗口：单次请求携带的最大历史消息数（上下文压缩的粗粒度保护）
_CHAT_HISTORY_WINDOW = 40

_DIRECTOR_SYSTEM_PROMPT = (
    "你是 Director Lab 漫剧生产工作台的导演台助手，帮助用户完成短剧制作。"
    "你可以：解答项目与制作流程问题、分析剧本、给出制作建议、解释任务状态与失败原因。"
    "当前系统支持的制作阶段：剧本评估/解析 → 角色/场景/道具资产 → 分镜 → 视频片段 → 合成导出。"
    "回答保持简洁专业；涉及具体项目时基于用户提供的上下文作答，不虚构不存在的资产或任务。"
    "用户明确要求执行生成类操作时，说明需要通过对应页面或后续工具调用完成，不要虚构已执行。"
)


def _session_out(db: Session, s: AgentSession) -> AgentSessionOut:
    msg_count = db.scalar(
        select(func.count(AgentMessage.id)).where(AgentMessage.session_id == s.id)
    )
    last_at = db.scalar(
        select(func.max(AgentMessage.created_at)).where(AgentMessage.session_id == s.id)
    )
    out = AgentSessionOut.model_validate(s)
    out.message_count = int(msg_count or 0)
    out.last_message_at = last_at
    return out


def _get_session(db: Session, session_id: str) -> AgentSession:
    s = db.get(AgentSession, session_id)
    if not s:
        raise biz_error(code="agent_session_not_found", http=404, message="会话不存在")
    return s


@router.post("", status_code=201)
def create_session(payload: AgentSessionCreate, db: Session = Depends(get_db)) -> AgentSessionOut:
    if payload.project_id:
        if not db.get(Project, payload.project_id):
            raise biz_error(code="project_not_found", http=404, message="关联项目不存在")
    s = AgentSession(
        id=agent_session_id(),
        title=payload.title,
        project_id=payload.project_id,
        model=payload.model,
        status="active",
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _session_out(db, s)


@router.get("")
def list_sessions(
    project_id: str | None = Query(default=None, max_length=32),
    include_archived: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    q = select(AgentSession)
    if project_id:
        q = q.where(AgentSession.project_id == project_id)
    if not include_archived:
        q = q.where(AgentSession.status == "active")
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    items = (
        db.scalars(
            q.order_by(AgentSession.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
        .unique()
        .all()
    )
    return {
        "items": [_session_out(db, s).model_dump(mode="json") for s in items],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


@router.get("/{session_id}")
def get_session(session_id: str, db: Session = Depends(get_db)) -> AgentSessionOut:
    return _session_out(db, _get_session(db, session_id))


@router.patch("/{session_id}")
def update_session(
    session_id: str, payload: AgentSessionUpdate, db: Session = Depends(get_db)
) -> AgentSessionOut:
    s = _get_session(db, session_id)
    if payload.title is not None:
        s.title = payload.title
    # 显式传 null 表示取消关联；未携带该字段则不变更（与 model 字段同规则）
    if "project_id" in payload.model_fields_set:
        if payload.project_id and not db.get(Project, payload.project_id):
            raise biz_error(code="project_not_found", http=404, message="关联项目不存在")
        s.project_id = payload.project_id
    # 显式传 null 表示清空会话级模型、回落到配置默认模型；未携带该字段则不变更
    if "model" in payload.model_fields_set:
        s.model = payload.model
    db.commit()
    db.refresh(s)
    return _session_out(db, s)


@router.delete("/{session_id}")
def archive_session(session_id: str, db: Session = Depends(get_db)) -> dict:
    """软删除（归档）：消息保留，不级联删除项目或产物。"""
    s = _get_session(db, session_id)
    if s.status == "archived":
        raise biz_error(code="session_archived", http=409, message="会话已归档")
    s.status = "archived"
    db.commit()
    return {"id": s.id, "status": s.status}


@router.post("/{session_id}/restore")
def restore_session(session_id: str, db: Session = Depends(get_db)) -> AgentSessionOut:
    s = _get_session(db, session_id)
    if s.status != "archived":
        raise biz_error(code="invalid_state_transition", http=409, message="仅归档会话可恢复")
    s.status = "active"
    db.commit()
    db.refresh(s)
    return _session_out(db, s)


@router.post("/{session_id}/messages", status_code=201)
def append_message(
    session_id: str, payload: AgentMessageCreate, db: Session = Depends(get_db)
) -> AgentMessageOut:
    s = _get_session(db, session_id)
    if s.status == "archived":
        raise biz_error(code="session_archived", http=409, message="会话已归档，不能追加消息")
    max_seq = (
        db.scalar(
            select(func.max(AgentMessage.seq)).where(AgentMessage.session_id == session_id)
        )
        or 0
    )
    m = AgentMessage(
        id=agent_message_id(),
        session_id=session_id,
        role=payload.role,
        content=payload.content,
        payload=payload.payload,
        attachment_id=payload.attachment_id,
        seq=max_seq + 1,
        created_at=datetime.now(),
    )
    db.add(m)
    s.updated_at = datetime.now()
    db.commit()
    db.refresh(m)
    return AgentMessageOut.model_validate(m)


@router.get("/{session_id}/messages")
def list_messages(
    session_id: str,
    after_seq: int = Query(default=0, ge=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    _get_session(db, session_id)
    q = select(AgentMessage).where(
        AgentMessage.session_id == session_id, AgentMessage.seq > after_seq
    )
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    items = (
        db.scalars(q.order_by(AgentMessage.seq.asc()).offset((page - 1) * page_size).limit(page_size))
        .unique()
        .all()
    )
    return {
        "items": [AgentMessageOut.model_validate(m).model_dump(mode="json") for m in items],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


class ChatRequest(BaseModel):
    """聊天请求：用户输入文本。"""

    content: str = Field(min_length=1, max_length=50_000)


@router.post("/{session_id}/chat")
def chat(session_id: str, payload: ChatRequest, db: Session = Depends(get_db)) -> dict:
    """导演台聊天：用户消息持久化 → LLM 多轮对话 → 助手回复持久化。

    - 会话 model 字段覆盖默认模型（切换模型真实生效）
    - 历史窗口：最近 _CHAT_HISTORY_WINDOW 条 user/assistant 消息
    - 意图路由（阶段 E）：unsafe 直接拒绝、generate_request 引导到对应页面，不进入主对话
    - LLM 瞬态错误（unreachable/timeout/llm_error）指数退避重试最多 3 次；
      配置类错误不重试；最终失败时用户消息已持久化，前端可重试（重试产生新请求）
    """
    s = _get_session(db, session_id)
    if s.status == "archived":
        raise biz_error(code="session_archived", http=409, message="会话已归档，不能发送消息")

    # 1. 持久化用户消息
    max_seq = (
        db.scalar(select(func.max(AgentMessage.seq)).where(AgentMessage.session_id == session_id))
        or 0
    )
    user_msg = AgentMessage(
        id=agent_message_id(),
        session_id=session_id,
        role="user",
        content=payload.content,
        seq=max_seq + 1,
        created_at=datetime.now(),
    )
    db.add(user_msg)
    db.commit()
    db.refresh(user_msg)

    # 1.4 意图路由（阶段 E：意图路由工具化，单 Agent 框架 P1-2）
    # 模型只做分类，路由策略由确定性代码执行；分类失败降级为 query 继续主对话
    from app.services.intent_router import (
        GENERATE_GUIDE_MESSAGE,
        UNSAFE_REJECT_MESSAGE,
        build_clarification_question,
        classify_intent,
        route_action,
    )

    # 加载近期对话（当前消息之前，旧→新），供意图识别消解『它』『上一个』等指代
    recent = (
        db.scalars(
            select(AgentMessage)
            .where(
                AgentMessage.session_id == session_id,
                AgentMessage.role.in_(["user", "assistant"]),
                AgentMessage.seq <= max_seq,
            )
            .order_by(AgentMessage.seq.desc())
            .limit(10)
        )
        .unique()
        .all()
    )[::-1]
    intent = classify_intent(
        payload.content,
        recent_messages=[{"role": m.role, "content": m.content} for m in recent],
    )
    action = route_action(intent.intent)
    # 结构化意图（B 档）：ambiguous 且带缺失信息时向用户澄清，不进入主对话（不虚构目标）
    if intent.intent == "ambiguous" and intent.missing_information:
        action = "clarify"
    if action in ("reject", "guide", "clarify"):
        # unsafe：直接拒绝；generate_request：不执行生成，引导到对应页面（P0：生成不在 Agent 直接执行范围）；
        # clarify：信息不足，向用户澄清缺失信息（确定性拼接，不调用模型）
        if action == "reject":
            reply = UNSAFE_REJECT_MESSAGE
        elif action == "guide":
            reply = GENERATE_GUIDE_MESSAGE
        else:
            reply = build_clarification_question(intent.missing_information)
        assistant_msg = AgentMessage(
            id=agent_message_id(),
            session_id=session_id,
            role="assistant",
            content=reply,
            payload={
                "intent": intent.intent,
                "intent_reason": intent.reason,
                "intent_classified": intent.classified,
                "route_action": action,
                "intent_goal": intent.goal,
                "risk_level": intent.risk_level,
                "confidence": intent.confidence,
            },
            seq=max_seq + 2,
            created_at=datetime.now(),
        )
        db.add(assistant_msg)
        s.updated_at = datetime.now()
        db.commit()
        db.refresh(assistant_msg)
        return {
            "user_message": AgentMessageOut.model_validate(user_msg).model_dump(mode="json"),
            "assistant_message": AgentMessageOut.model_validate(assistant_msg).model_dump(mode="json"),
        }

    # 1.5 关联项目时注入真实项目上下文（只读工具查询，不触发任何生成）
    # 审计要求（单 Agent 方案 §10/§16）：每次工具调用落一条 role=tool_event 消息，
    # payload 记录 tool_name/args/ok/error_code/duration_ms；tool_event 不进入对话上下文
    # （历史窗口仅取 user/assistant），仅作审计与追踪用途
    system_prompt = _DIRECTOR_SYSTEM_PROMPT
    tool_event_seq = max_seq + 1  # user 消息为 max_seq+1，tool_event 从 max_seq+2 起
    if s.project_id:
        from app.services.agent_tools import execute_tool, get_allowed_tools

        # 意图-工具映射（B 档）：只执行当前意图候选的工具，降低选错/越权/Token 消耗；
        # 识别失败降级为 query 时映射到全部只读工具，行为与阶段 E 一致
        allowed_tools = get_allowed_tools(intent.intent)
        tool_calls = []
        for tool_name, tool_args in (
            ("project_overview", {"project_id": s.project_id}),
            ("asset_stats", {"project_id": s.project_id}),
            ("list_jobs", {"project_id": s.project_id, "limit": 5}),
        ):
            if tool_name not in allowed_tools:
                continue
            tool_calls.append((tool_name, tool_args, execute_tool(db, tool_name, **tool_args)))
        overview = next((r for n, _, r in tool_calls if n == "project_overview"), None)
        stats = next((r for n, _, r in tool_calls if n == "asset_stats"), None)
        jobs = next((r for n, _, r in tool_calls if n == "list_jobs"), None)
        # 工具调用审计落库（LLM 失败也不丢失审计记录，故在此即提交）
        for tool_name, tool_args, result in tool_calls:
            tool_event_seq += 1
            db.add(
                AgentMessage(
                    id=agent_message_id(),
                    session_id=session_id,
                    role="tool_event",
                    content="",
                    payload={
                        "tool_name": tool_name,
                        "args": tool_args,
                        "ok": result.ok,
                        "error_code": result.error_code,
                        "duration_ms": result.duration_ms,
                    },
                    seq=tool_event_seq,
                    created_at=datetime.now(),
                )
            )
        db.commit()
        ctx_lines: list[str] = []
        if overview is not None and overview.ok and overview.data:
            d = overview.data
            ctx_lines.append(
                f"当前关联项目：{d['name']}（{d['id']}），状态 {d['status']}，"
                f"制作阶段 {d['current_stage']}，镜头进度 {d['shot_done']}/{d['shot_total']}"
            )
        if stats is not None and stats.ok and stats.data:
            by_type = ", ".join(f"{k}×{v}" for k, v in stats.data["by_type"].items()) or "无"
            ctx_lines.append(f"项目资产统计：{by_type}（共 {stats.data['total']} 项）")
        if jobs is not None and jobs.ok and jobs.data and jobs.data["count"] > 0:
            job_lines = ", ".join(
                f"{j['job_type']}({j['status']}" + (f"/{j['error_code']}" if j.get("error_code") else "") + ")"
                for j in jobs.data["items"]
            )
            ctx_lines.append(f"最近任务（新→旧）：{job_lines}")
        if ctx_lines:
            system_prompt += "\n以下是当前关联项目的真实状态（由系统查询提供，回答项目相关问题以此为准）：\n" + "\n".join(
                ctx_lines
            )

    # 2. 加载历史窗口（仅 user/assistant 参与对话上下文）
    history = (
        db.scalars(
            select(AgentMessage)
            .where(
                AgentMessage.session_id == session_id,
                AgentMessage.role.in_(["user", "assistant"]),
                AgentMessage.seq <= max_seq + 1,
            )
            .order_by(AgentMessage.seq.desc())
            .limit(_CHAT_HISTORY_WINDOW)
        )
        .unique()
        .all()
    )[::-1]
    messages = [{"role": "system", "content": system_prompt}] + [
        {"role": m.role, "content": m.content} for m in history
    ]

    # 3. 调用 LLM（会话 model 覆盖默认；网络调用在数据库事务之外）
    # 瞬态错误指数退避重试（单 Agent 框架 P2-1）：unreachable/timeout/llm_error 最多 3 次；
    # llm_unconfigured 等配置类错误不重试直接返回；用户消息已持久化，重试幂等安全
    import time as _time

    _LLM_RETRYABLE = {"llm_unreachable", "llm_timeout", "llm_error"}
    _LLM_MAX_ATTEMPTS = 3
    client = LLMClient(model=s.model) if s.model else LLMClient()
    reply: str | None = None
    last_err: LLMError | None = None
    for attempt in range(1, _LLM_MAX_ATTEMPTS + 1):
        try:
            reply = client.chat_messages(messages)
            break
        except LLMError as e:
            last_err = e
            if e.code in _LLM_RETRYABLE and attempt < _LLM_MAX_ATTEMPTS:
                _time.sleep(1.0 * (2 ** (attempt - 1)))
                continue
            break
    if reply is None:
        assert last_err is not None
        http = 409 if last_err.code == "llm_unconfigured" else 502
        raise biz_error(code=last_err.code, http=http, message=last_err.message) from last_err

    # 4. 持久化助手回复（payload 记录意图分类结果，便于审计与效果评估）
    assistant_msg = AgentMessage(
        id=agent_message_id(),
        session_id=session_id,
        role="assistant",
        content=reply,
        payload={
            "intent": intent.intent,
            "intent_reason": intent.reason,
            "intent_classified": intent.classified,
            "route_action": action,
            "intent_goal": intent.goal,
            "risk_level": intent.risk_level,
            "confidence": intent.confidence,
        },
        seq=tool_event_seq + 1,
        created_at=datetime.now(),
    )
    db.add(assistant_msg)
    s.updated_at = datetime.now()
    db.commit()
    db.refresh(assistant_msg)

    return {
        "user_message": AgentMessageOut.model_validate(user_msg).model_dump(mode="json"),
        "assistant_message": AgentMessageOut.model_validate(assistant_msg).model_dump(mode="json"),
    }
