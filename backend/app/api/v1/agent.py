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
from app.models import AgentConfirmation, AgentMessage, AgentSession, AgentTask, Project
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
    "用户询问项目/生成「进度」时，直接依据系统提供的进度数据回答（总数、已完成、进行中、排队、"
    "失败、预计剩余时间），不要反问要生成哪个镜头或实体——那是发起新生成时才需要的信息。"
    "需要用户补充信息时，先基于项目上下文思考可行方案，以编号选项形式让用户选择"
    "（如镜头/实体清单），能选项的先让用户选，不要让用户自由输入。"
    "【联网搜索规则】"
    "1. 用户明确要求联网搜索时，系统会执行真实搜索并在上下文中提供搜索结果，只能基于该结果作答。"
    "2. 不允许根据模型内部知识伪装联网搜索；未提供搜索结果时必须明确说明没有执行联网搜索。"
    "3. 搜索结果为空时不能编造结果，应如实告知并建议调整关键词。"
    "4. 只能根据实际返回的搜索结果总结，重要事实保留来源链接（URL）。"
    "5. 搜索结果不完整时明确标记信息缺口，不隐瞒搜索或网页读取失败。"
    "【任务状态纪律】"
    "1. 任务（解析/生成/合成/导出）的进度、状态与结果只能引用系统提供的任务卡片数据（task_card）或事件流中的真实状态，不得编造或推测。"
    "2. 未提供任务状态数据时，如实说明当前没有该任务的实时状态，建议用户稍后查看任务卡片，不要虚构进度或结果。"
    "3. 任务失败时基于真实错误码与错误信息解释原因，不猜测未提供的失败细节。"
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
        # 项目切换时清空结构化焦点/操作上下文（Context Manager），
        # 防止旧项目实体 ID 被用于新项目的『重新生成』等指代消解（跨项目上下文污染）
        if payload.project_id != s.project_id:
            from app.services import context_manager

            context_manager.clear_on_project_switch(s)
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


class ApprovalResolveRequest(BaseModel):
    """审批裁决请求。"""

    approve: bool = Field(description="true 批准 / false 拒绝")
    comment: str | None = Field(default=None, max_length=500, description="可选：审批意见")


def _confirmation_out(c: AgentConfirmation) -> dict:
    """审批记录对外结构（不含内部任务详情）。"""
    return {
        "confirmation_id": c.id,
        "task_id": c.task_id,
        "tool_name": c.tool_name,
        "args": c.args,
        "status": c.status,
        "comment": c.comment,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _confirmation_in_session(
    db: Session, session_id: str, confirmation_id: str
) -> AgentConfirmation:
    """校验审批记录存在且属于该会话，防止跨会话操作他人审批。"""
    confirmation = db.get(AgentConfirmation, confirmation_id)
    if confirmation is None:
        raise biz_error(code="confirmation_not_found", http=404, message="审批记录不存在")
    task = db.get(AgentTask, confirmation.task_id)
    if task is None or task.session_id != session_id:
        raise biz_error(code="confirmation_not_found", http=404, message="审批记录不属于该会话")
    return confirmation


@router.get("/{session_id}/approvals")
def list_approvals(session_id: str, db: Session = Depends(get_db)) -> dict:
    """会话内待审批的生成申请（供审批面板展示）。"""
    _get_session(db, session_id)
    from app.services.approvals import list_pending

    items = [_confirmation_out(c) for c in list_pending(db, session_id)]
    return {"items": items, "count": len(items)}


@router.post("/{session_id}/approvals/{confirmation_id}/resolve")
def resolve_approval(
    session_id: str,
    confirmation_id: str,
    payload: ApprovalResolveRequest,
    db: Session = Depends(get_db),
) -> dict:
    """裁决审批：批准或拒绝。仅处理待审记录，重复裁决返回 409。

    批准本身不触发生成：执行需再调 execute 接口，审批与执行严格分离。
    """
    s = _get_session(db, session_id)
    if s.status == "archived":
        raise biz_error(code="session_archived", http=409, message="会话已归档，不能审批")
    from app.services.approvals import resolve_confirmation

    _confirmation_in_session(db, session_id, confirmation_id)
    resolved = resolve_confirmation(
        db, confirmation_id=confirmation_id, approve=payload.approve, comment=payload.comment
    )
    return _confirmation_out(resolved)


@router.post("/{session_id}/approvals/{confirmation_id}/execute")
def execute_approval(
    session_id: str,
    confirmation_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """执行已批准的生成申请：创建任务并入队（幂等，重复调用复用同一任务）。"""
    s = _get_session(db, session_id)
    if s.status == "archived":
        raise biz_error(code="session_archived", http=409, message="会话已归档，不能执行")
    from app.services.agent_tools import execute_tool
    from app.services.pipeline_tools import PIPELINE_TOOL_REGISTRY, execute_pipeline_tool
    from app.services.regen_tools import REGEN_TOOL_REGISTRY, execute_regen_tool

    _confirmation_in_session(db, session_id, confirmation_id)
    confirmation = db.get(AgentConfirmation, confirmation_id)
    # 流程工具（合成/导出等）：批准后经流程工具执行通道执行（含状态机门禁）
    if confirmation is not None and confirmation.tool_name in PIPELINE_TOOL_REGISTRY:
        result = execute_pipeline_tool(db, confirmation.tool_name, approved=True, **(confirmation.args or {}))
    # 返工工具（重新生成等）：批准后经返工工具执行通道执行（含状态机门禁）
    elif confirmation is not None and confirmation.tool_name in REGEN_TOOL_REGISTRY:
        result = execute_regen_tool(db, confirmation.tool_name, approved=True, **(confirmation.args or {}))
    else:
        result = execute_tool(db, "run_generate", approved=True, confirmation_id=confirmation_id)
    if not result.ok:
        http = 409 if result.error_code == "approval_required" else 400
        raise biz_error(
            code=result.error_code or "tool_internal_error",
            http=http,
            message=result.error_message or "执行生成失败",
        )
    # Execution Verifier（意图识别改造 Phase 3）：不信任工具返回成功消息，
    # 异步 Job 类结果（含 job_id）确定性验证库中真实 Job（存在/状态合法/项目归属），
    # 同步状态变更类结果（create_project/save_script 等）直接透传不误报
    from app.services.execution_verifier import verify_execution_result

    verification = verify_execution_result(
        db,
        result.data,
        expected_project_id=(confirmation.args or {}).get("project_id") if confirmation else None,
    )
    if not verification.verified:
        raise biz_error(
            code=verification.reason_code or "verification_failed",
            http=500,
            message=verification.message or "执行结果验证失败",
        )
    return {
        "confirmation_id": confirmation_id,
        "verified": True,
        "job_status": verification.job_status,
        **(result.data or {}),
    }


class ReviewResolveRequest(BaseModel):
    '''业务对象审核裁决请求（前端按钮提交）。'''

    action: str = Field(description='approve / reject / revise / defer')
    comment: str | None = Field(default=None, max_length=2000, description='可选：修改意见')


@router.get('/{session_id}/reviews')
def list_reviews(session_id: str, db: Session = Depends(get_db)) -> dict:
    '''会话内确认事项（审核卡片）：按版本返回，刷新页面后仍可重建。'''
    _get_session(db, session_id)
    from app.services.review_confirmation import list_reviews as _list_reviews, to_card

    items = [to_card(c) for c in _list_reviews(db, session_id)]
    return {'items': items, 'count': len(items)}


@router.post('/{session_id}/reviews/{confirmation_id}/resolve')
def resolve_review(
    session_id: str,
    confirmation_id: str,
    payload: ReviewResolveRequest,
    db: Session = Depends(get_db),
) -> dict:
    '''裁决业务对象审核：通过 / 不通过 / 修改意见 / 暂缓。

    - 裁决结果写回业务数据（项目剧本审核状态）并落审计消息；
    - 仅处理 pending，重复裁决返回 409（幂等保护）；
    - 拒绝 / 修改后由后端门禁阻断依赖审核通过的后续任务。
    '''
    s = _get_session(db, session_id)
    if s.status == 'archived':
        raise biz_error(code='session_archived', http=409, message='会话已归档，不能审核')
    from app.services import review_confirmation as rc

    confirmation = db.get(AgentConfirmation, confirmation_id)
    if confirmation is None or confirmation.session_id != session_id:
        raise biz_error(code='confirmation_not_found', http=404, message='确认事项不存在')
    if confirmation.status != rc.STATUS_PENDING:
        raise biz_error(
            code='confirmation_already_resolved',
            http=409,
            message=f'该确认事项已处理（{confirmation.status}），不能重复裁决',
        )
    action = (payload.action or '').strip()
    if action not in rc.RESOLUTION_STATUS:
        raise biz_error(
            code='invalid_resolution',
            http=400,
            message='action 须为 approve / reject / revise / defer 之一',
        )
    resolved = rc.resolve(
        db,
        confirmation,
        resolution=action,
        user_response=payload.comment or action,
        comment=payload.comment,
        matched_by='button',
    )
    return rc.to_card(resolved)


# 制作阶段 → 下一步指引（derive_progress 的 current_stage 映射；
# 生成请求定位不到目标时按项目实际阶段引导，让 Agent 成为流程向导）
_STAGE_GUIDANCE = {
    "estimate": "项目还没有进入分析阶段：请先在剧本页保存剧本（也可用剧本创作辅助生成），再发起剧本一体化分析，解析出场景、镜头与资产定义。",
    "select_range": "剧本已保存但尚未分析：请先发起剧本一体化分析，解析出镜头后再生成。",
    "parse": "剧本分析尚未产出镜头：请先完成剧本分析（或等待进行中的分析任务完成）。",
    "characters": "项目还有角色缺少定妆图：请先在资产库生成角色定妆图（对话中给出角色 ID，chr_ 开头，即可提交生成申请）。",
    "scenes": "项目还有场景缺少基准图：请先在资产库生成场景基准图（对话中给出场景 ID，scn_ 开头，即可提交生成申请）。",
    "assets_review": "公共资产尚未通过人工审核：请先在资产库确认资产无误并通过审核，之后才能生成分镜图。",
    "keyframes": "项目还有分镜图未生成或未人工确认：可以先对我说『为第 N 镜生成关键帧』（或给出镜头 ID，sht_ 开头）生成分镜图，人工确认后再生成视频。",
    "clips": "分镜图已就绪：可以直接对我说『为第 N 镜生成视频』，或给出镜头 ID（sht_ 开头）。",
    "audio": "视频已生成，还有对白/旁白镜头缺少音频：可对我说『为第 N 镜生成配音』。",
    "compose": "镜头产物已就绪：可到合成页进行成片合成。",
    "export": "合成已完成：可到导出页导出成片。",
    "done": "项目制作流程已完成。",
}


def _stage_guidance(db: Session, project_id: str) -> str:
    """按项目当前制作阶段返回下一步指引（阶段取 derive_progress，不另行判定）。"""
    from app.models import Project
    from app.services.progress import derive_progress

    project = db.get(Project, project_id)
    if project is None:
        return ""
    _total, _done, stage = derive_progress(db, project)
    return _STAGE_GUIDANCE.get(stage, "")


def _try_create_generate_approval(
    db: Session, session: AgentSession, session_id: str, text: str
) -> tuple[str, dict | None]:
    """把对话中的生成请求落地为审批申请（只建申请，绝不执行生成）。

    镜头目标：消息明确给出镜头标识（sht_ ID 或『第 N 镜』）时创建申请；
    实体目标（角色/场景/道具公共资产）：消息明确给出实体 ID（chr_/scn_/prp_）时创建申请；
    定位不到时返回 (None, None) 交回主流程回落主对话，由大模型结合项目上下文兜底
    （规则不兜底：不猜测目标，猜错会生成错对象，浪费算力与成本）。
    """
    from app.services.agent_tools import execute_tool
    from app.services.generate_intent import resolve_entity, resolve_shot, resolve_target

    if not session.project_id:
        # 未关联项目时自动创建，避免用户需要手动到前端操作
        from app.services.pipeline_tools import tool_create_project
        import re as _re
        # 尝试从消息中提取项目名称（如『生成一部科幻视频』→『科幻项目』）
        m = _re.search(r'(?:生成|制作|做)(?:一部|个)?(.+?)(?:视频|短片|动画|影片)', text)
        project_name = (m.group(1) + "项目") if m else "新项目"
        try:
            create_data = tool_create_project(
                db,
                name=project_name,
                aspect_ratio="9:16",
                creation_type="novel",
            )
        except Exception as e:
            return (
                f"自动创建项目失败：{e}。请先在右侧关联项目后再发起。",
                None,
            )
        new_project_id = create_data["project_id"]
        session.project_id = new_project_id
        db.commit()
        return (
            f"已为您自动创建项目『{project_name}』（画幅 9:16，项目 ID: {new_project_id}）。\n"
            "项目已就绪，接下来请提供剧本内容，我会帮您保存并分析剧本，"
            "然后就可以生成分镜图和视频了。",
            {"ok": True, "auto_created_project": True, "project_id": new_project_id},
        )

    # 实体目标优先：消息中给出明确的实体 ID（chr_/scn_/prp_）时按公共资产生成处理
    entity, entity_target, entity_reason = resolve_entity(db, session.project_id, text)
    if entity is not None:
        result = execute_tool(
            db,
            "request_generate",
            session_id=session_id,
            project_id=session.project_id,
            entity_id=entity.id,
            target=entity_target,
        )
        if not result.ok:
            return f"生成申请提交失败：{result.error_message}", {
                "ok": False,
                "error_code": result.error_code,
            }
        data = result.data or {}
        reply = (
            f"已提交生成申请（{entity_target} 公共资产 · 实体 {entity.id}），"
            f"审批编号 {data.get('confirmation_id')}。"
            "请在右侧『待审批生成』中批准后点执行，任务才会真正创建；我不会在对话中直接触发生成。"
        )
        return reply, {"ok": True, **data}
    if entity_reason in ("entity_not_found", "entity_not_in_project"):
        # 实体定位不到：不落固定文案，回落主对话由大模型结合上下文兜底
        return None, None

    shot, reason = resolve_shot(db, session.project_id, text)
    if shot is None:
        # 镜头定位不到（未指定/不存在/不属于项目/歧义）：不落固定文案，
        # 回落主对话由大模型结合项目上下文给出可选项（规则不兜底）
        return None, None

    target = resolve_target(text)
    result = execute_tool(
        db,
        "request_generate",
        session_id=session_id,
        project_id=session.project_id,
        shot_id=shot.id,
        target=target,
    )
    if not result.ok:
        return f"生成申请提交失败：{result.error_message}", {
            "ok": False,
            "error_code": result.error_code,
        }

    data = result.data or {}
    reply = (
        f"已提交生成申请（{target} · 镜头 {shot.id}），审批编号 {data.get('confirmation_id')}。"
        "请在右侧『待审批生成』中批准后点执行，任务才会真正创建；我不会在对话中直接触发生成。"
    )
    return reply, {"ok": True, **data}


def _resolve_regen_target(db: Session, project_id: str, text: str) -> tuple[dict, str | None]:
    """从消息中解析返工操作的目标（资产 ast_ / 镜头 sht_ 或『第 N 镜』）。

    返回 (目标参数, 错误消息)：解析成功时错误消息为 None。
    不猜测目标：定位不到时返回澄清提示（猜错会返工错对象，浪费算力与成本）。
    """
    import re as _re
    from app.models import Asset, Shot, Scene
    from app.services.generate_intent import resolve_shot
    # 资产 ID：ast_ 前缀
    m = _re.search(r"\bast_[a-z0-9]+\b", text)
    if m:
        asset = db.get(Asset, m.group(0))
        if asset is None:
            return {}, f"资产 {m.group(0)} 不存在，请确认资产 ID。"
        if asset.project_id != project_id:
            return {}, "该资产不属于当前项目，请确认后重试。"
        return {"asset_id": asset.id}, None
    # 镜头 ID 或『第 N 镜』
    m = _re.search(r"\bsht_[a-z0-9]+\b", text)
    if m:
        shot = db.get(Shot, m.group(0))
        if shot is None:
            return {}, f"镜头 {m.group(0)} 不存在，请确认镜头 ID。"
        scene = db.get(Scene, shot.scene_id)
        if scene is None or scene.project_id != project_id:
            return {}, "该镜头不属于当前项目，请确认后重试。"
        return {"shot_id": shot.id}, None
    shot, reason = resolve_shot(db, project_id, text)
    if shot is not None:
        return {"shot_id": shot.id}, None
    hints = {
        "shot_not_found": "没有找到你提到的镜头，请确认镜头编号是否存在。",
        "shot_not_in_project": "该镜头不属于当前项目，请确认后重试。",
        "shot_ambiguous": "『第 N 镜』在本项目内命中多个镜头，请补充场景或直接给出镜头 ID。",
    }
    if reason in hints:
        return {}, hints[reason]
    return {}, (
        "请明确要返工的目标：给出资产 ID（ast_ 开头）或镜头（『第 N 镜』或镜头 ID，sht_ 开头）。"
    )


def _handle_pipeline_action(
    db: Session, session: AgentSession, session_id: str, intent, text: str
) -> tuple[str, dict | None]:
    """流程操作处理：意图识别流程节点 → 状态机门禁 → 执行流程工具。

    - 动作键来自意图识别（规则快路径或 LLM entities.pipeline_action）
    - review_current：泛化审核确认，按当前流程节点解析为具体审核动作
    - 高风险工具（合成/导出）不直接执行：只创建审批申请，批准后经 execute 接口执行
    - 状态机门禁阻断时返回当前节点与下一步指引（不猜测、不降级、不跳过审核闸口）
    """
    from app.services.pipeline_tools import PIPELINE_TOOL_REGISTRY, execute_pipeline_tool
    from app.services.pipeline_state import get_pipeline_status

    action = (intent.entities or {}).get("pipeline_action") if intent else None
    if not action:
        return (
            "未能识别出要执行的流程操作。可以对我说：新建项目、保存剧本、分析剧本、"
            "审核资产库、确认分镜图、合成成片、导出成片，或直接描述你的需求。",
            None,
        )
    # 泛化审核确认：按当前流程节点解析为具体审核动作
    if action == "review_current":
        if not session.project_id:
            return ("当前会话未关联项目，无法执行审核操作。请先在右侧关联项目。", None)
        try:
            st = get_pipeline_status(db, session.project_id)
        except Exception:
            return ("项目状态查询失败，请确认项目是否存在。", None)
        node_review_map = {
            "review_assets": "review_assets",
            "review_keyframes": "confirm_keyframes",
            # 分析完成节点：泛化确认（没问题/确认无误）落为确认解析结果
            "analyze_script": "confirm_parse",
        }
        action = node_review_map.get(st.node_key)
        if action is None:
            return (
                f"当前流程节点为『{st.node_name}』，不是审核闸口，无需审核。下一步：{st.next_hint}",
                None,
            )
    # 新建项目：无需关联项目；其余操作须会话已关联项目
    if action != "create_project" and not session.project_id:
        return (
            "当前会话未关联项目，无法执行该流程操作。请先在右侧关联项目"
            "（或先对我说『新建项目』创建一个新项目）。",
            None,
        )
    spec = PIPELINE_TOOL_REGISTRY.get(action)
    if spec is None:
        # 返工工具（重新生成/替换/采纳）：目标从消息中解析（资产 ast_/镜头 sht_ 或『第 N 镜』）
        from app.services.regen_tools import REGEN_TOOL_REGISTRY, execute_regen_tool
        spec = REGEN_TOOL_REGISTRY.get(action)
        if spec is None:
            return (f"未注册的流程操作：{action}，请确认操作名称。", None)
        if not session.project_id:
            return ("当前会话未关联项目，无法执行返工操作。请先在右侧关联项目。", None)
        target_args, target_err = _resolve_regen_target(db, session.project_id, text)
        if target_err:
            return (target_err, None)
        # 生成类返工（重新生成）：只创建审批申请，不直接执行
        if spec.get("requires_approval"):
            from app.services.approvals import create_confirmation
            confirmation = create_confirmation(
                db,
                session_id=session_id,
                tool_name=action,
                args={"project_id": session.project_id, **target_args},
                goal=intent.goal or f"执行返工操作 {action}",
                risk_level=spec.get("risk_level", "high"),
            )
            return (
                f"已提交『{spec['description'].split('（')[0]}』审批申请（审批编号 {confirmation.id}）。"
                "请在右侧『待审批生成』中批准后点执行，任务才会真正创建；我不会在对话中直接执行生成类操作。",
                {"ok": True, "confirmation_id": confirmation.id, "tool": action, **target_args},
            )
        # 替换/采纳/查询类返工：直接执行
        result = execute_regen_tool(db, action, project_id=session.project_id, **target_args)
        if not result.ok:
            return f"返工操作执行失败：{result.error_message}", {
                "ok": False,
                "error_code": result.error_code,
                "tool": action,
            }
        data = result.data or {}
        note = data.get("note", "")
        reply = f"返工操作已执行：{spec['description'].split('（')[0]}。"
        if note:
            reply += f"\n{note}"
        return reply, {"ok": True, "tool": action, **{k: v for k, v in data.items() if k != "note"}}
    # 高风险工具（需审批）：只创建审批申请，不直接执行
    if spec.get("requires_approval"):
        from app.services.approvals import create_confirmation
        confirmation = create_confirmation(
            db,
            session_id=session_id,
            tool_name=action,
            args={"project_id": session.project_id},
            goal=intent.goal or f"执行流程操作 {action}",
            risk_level=spec.get("risk_level", "high"),
        )
        return (
            f"已提交『{spec['description'].split('（')[0]}』审批申请（审批编号 {confirmation.id}）。"
            "请在右侧『待审批生成』中批准后点执行，任务才会真正创建；我不会在对话中直接执行高风险操作。",
            {"ok": True, "confirmation_id": confirmation.id, "tool": action},
        )
    # 普通流程操作：经状态机门禁直接执行
    kwargs: dict = {"project_id": session.project_id} if action != "create_project" else {}
    if action == "analyze_script":
        # 解析是异步耗时任务：把发起方会话带进去，完成后据此把结果回推到本对话
        kwargs["session_id"] = session_id
    if action == "review_assets":
        # 资产库审核：ReviewAssetsInput 必填 action（approve/reject）。
        # 意图路由命中『资产审核通过/资产没问题』即视为通过；
        # 消息含驳回表述时落 reject，避免缺参报 tool_invalid_args。
        _neg = any(w in text for w in ("驳回", "不通过", "有问题", "打回", "返工"))
        kwargs["action"] = "reject" if _neg else "approve"
    if action == "save_script":
        # 保存剧本：从消息中提取剧本文本（『保存剧本』指令之后的部分，允许冒号/换行分隔）。
        # 无剧本文本时不猜测执行，明确要求用户提供（工具入参 text 必填）。
        import re as _re_script

        m = _re_script.search(r"(?:保存|存|提交|写入|更新)剧本\s*[:：]?\s*", text)
        script_text = text[m.end():].strip() if m else ""
        if len(script_text) < 10:
            return (
                "未检测到剧本内容。请把剧本全文发给我，例如：『保存剧本：』后跟完整剧本文本，"
                "或直接粘贴剧本后对我说『保存剧本』。",
                None,
            )
        kwargs["text"] = script_text
    result = execute_pipeline_tool(db, action, **kwargs)
    if not result.ok:
        return f"流程操作执行失败：{result.error_message}", {
            "ok": False,
            "error_code": result.error_code,
            "tool": action,
        }
    data = result.data or {}
    node_name = data.get("pipeline_node_name", "")
    next_hint = data.get("next_hint", "")
    # 异步任务不得谎称『已执行』：受理即回『正在分析』，结果由完成回推补上
    if action == "analyze_script":
        job_id = data.get("job_id", "")
        return (
            f"已开始分析剧本（任务 {job_id}），正在解析场景、镜头与角色道具定义，请稍候。\n"
            "分析完成后我会把结果直接发到这里，届时您回复『确认解析结果』即可落库并继续生成资产。",
            {
                "ok": True,
                "tool": action,
                "async": True,
                "job_id": job_id,
                "job_status": data.get("job_status"),
                "phase": "analyzing",
            },
        )
    if action == "confirm_parse":
        counts = data.get("counts") or {}
        return (
            "解析结果已确认并落库："
            f"角色 {counts.get('characters', 0)} 个、"
            f"场景 {counts.get('scenes', 0)} 个、"
            f"道具 {counts.get('props', 0)} 个、"
            f"镜头 {counts.get('shots', 0)} 个。\n"
            "下一步可以直接对我说『生成角色图』或『生成资产』，我会在本对话内继续。",
            {"ok": True, "tool": action, "counts": counts, **{k: v for k, v in data.items() if k not in ("pipeline_node", "counts", "parse_stats")}},
        )
    if action == "query_parse_status":
        # 只读进度查询：直接回传工具生成的状态话术（分析中/已完成/失败）
        message = data.get("message") or "暂无分析任务信息。"
        return (
            message,
            {
                "ok": True,
                "tool": action,
                "job_id": data.get("job_id"),
                "job_status": data.get("status"),
                "elapsed_seconds": data.get("elapsed_seconds"),
                "parse_version_id": data.get("parse_version_id"),
            },
        )
    reply = f"流程操作已执行：{node_name}。"
    if next_hint:
        reply += f"\n下一步：{next_hint}"
    return reply, {"ok": True, "tool": action, **{k: v for k, v in data.items() if k != "pipeline_node"}}


class ChatRequest(BaseModel):
    """聊天请求：用户输入文本。"""

    content: str = Field(min_length=1, max_length=50_000)


def _open_script_review(
    db: Session,
    s: AgentSession,
    state: dict,
    target_version: int | None = None,
    round_no: int = 1,
) -> dict | None:
    """剧本草稿生成后创建待确认事项（带版本号），返回前端确认卡片。

    确认事项必须持久化：页面刷新 / 会话恢复后仍能读到『正在等待确认第 N 版剧本』，
    用户的短回答才能被正确关联，不再靠 LLM 猜测。

    修改后重新提交时必须显式传入递增后的 target_version，
    否则剧本尚未落库（无 ScriptVersion 行）时版本号会倒退。
    """
    from app.services import review_confirmation as rc

    project_id = state.get("project_id")
    if not project_id:
        return None
    question = (
        "请确认这份剧本是否可以？回复『可以』我将保存剧本；"
        "如需修改，请直接提出改进建议。"
    )
    return_card = rc.to_card(
        rc.create_script_review(
            db,
            session_id=s.id,
            project_id=project_id,
            question=question,
            target_version=target_version,
            round_no=round_no,
        )
    )
    return return_card


def _handle_video_workflow(db: Session, s: AgentSession, text: str) -> tuple[str | None, dict | None]:
    """视频生成工作流（『生成XX视频』端到端流程）。

    命中工作流时返回 (回复, 附加payload)；未命中返回 (None, None)，交回主流程处理。
    流程：项目查重（存在则提示继续对话）→ 自动创建项目 → 询问剧本来源（自输/搜索网页）
    → 生成剧本 → 确认循环（可以则保存，否则按建议修改后再次确认）。
    状态存 AgentSession.workflow_state，流转由确定性代码执行。
    """
    from app.services import video_workflow as vw

    state = vw.get_workflow_state(s)
    stage = state.get("stage") or vw.STAGE_IDLE

    # 无故事名的『生成剧本』类请求（如『帮我生成剧本』）：友好询问想创作什么故事，
    # 避免落入流程操作路由报『未注册的流程操作』
    if stage in (vw.STAGE_IDLE, vw.STAGE_SCRIPT_CONFIRMED) and vw.is_bare_script_request(text):
        return (
            "好的，我可以帮您创作剧本。请告诉我：\n"
            "1. 想创作什么故事？（例如『小马过河』『龟兔赛跑』，或您自己的创意）\n"
            "您可以直接说『帮我生成一个小马过河的剧本』，我会为您安排后续流程。",
            {"workflow": "ask_story_name"},
        )

    # 新请求：『生成一个小马过河的视频』
    # 只有工作流空闲/已完结阶段才处理新项目请求（查重 + 新建）：已存在则关联，不重复创建。
    # 进行中的阶段（ask_style / ask_script_source / wait_user_script / script_draft）里
    # 含故事名的消息是对当前流程的回应（如『小马过河的剧本不可以，改成…』），
    # 必须交给下方阶段处理器，落到确认/驳回分支，不能被查重模板劫持。
    story = vw.extract_story_name(text)
    if story and stage in (vw.STAGE_IDLE, vw.STAGE_SCRIPT_CONFIRMED):
        existing = vw.find_project_by_name(db, story)
        if existing is not None:
            s.project_id = existing.id
            db.commit()
            return (
                f"项目『{existing.name}』已存在（项目 ID: {existing.id}），已为当前会话关联。"
                "您可以直接继续对话，例如完善剧本或推进制作流程。",
                {"workflow": "project_exists", "project_id": existing.id},
            )
        # 先询问剧本风格与画幅（竖屏/横屏），用户选择后再创建项目
        vw.set_workflow_state(
            db,
            s,
            {
                "stage": vw.STAGE_ASK_STYLE,
                "project_name": story,
                "revision_count": 0,
            },
        )
        return (
            f"好的，开始制作『{story}』。在创建项目前，请先告诉我您的偏好：\n"
            "1. 剧本风格：例如童话寓言、动画、真人实拍、喜剧、教育科普、冒险、温情治愈、古风国风等，也可以自由描述；\n"
            "2. 画幅：竖屏（9:16，适合手机短视频）还是横屏（16:9，适合横版播放）？\n"
            "请一并回复，例如『童话风格，竖屏』。",
            {"workflow": "ask_style"},
        )

    if stage == vw.STAGE_ASK_STYLE:
        style, orientation = vw.detect_style_and_orientation(text)
        if style is None or orientation is None:
            missing = []
            if style is None:
                missing.append("剧本风格（如童话寓言、动画、真人实拍、喜剧等，也可自由描述）")
            if orientation is None:
                missing.append("画幅（竖屏 9:16 或横屏 16:9）")
            return (
                "还需要确认以下信息：\n- " + "\n- ".join(missing) + "\n"
                "例如回复『童话风格，竖屏』。",
                {"workflow": "ask_style_again"},
            )
        from app.services.pipeline_tools import tool_create_project
        try:
            created = tool_create_project(
                db,
                name=state["project_name"],
                aspect_ratio=orientation,
                creation_type="novel",
                description=f"剧本风格：{style}",
            )
        except Exception as e:
            return f"自动创建项目失败：{e}。请稍后重试或手动创建项目。", {"workflow": "create_failed"}
        s.project_id = created["project_id"]
        state["stage"] = vw.STAGE_ASK_SOURCE
        state["project_id"] = created["project_id"]
        state["style"] = style
        state["orientation"] = orientation
        vw.set_workflow_state(db, s, state)
        return (
            f"已按您的偏好创建项目『{state['project_name']}』（风格：{style}，画幅 {orientation}，"
            f"项目 ID: {created['project_id']}）。\n"
            "接下来请选择剧本来源：\n"
            "1. 您自己输入剧本（直接把剧本发给我）；\n"
            "2. 我搜索网页资料，为您生成剧本。\n"
            "请回复 1 或 2，或直接说明您的选择。",
            {"workflow": "project_created", "project_id": created["project_id"], "style": style, "orientation": orientation},
        )

    if stage == vw.STAGE_ASK_SOURCE:
        choice = vw.detect_script_source_choice(text)
        if choice == "user":
            state["stage"] = vw.STAGE_WAIT_USER_SCRIPT
            vw.set_workflow_state(db, s, state)
            return "好的，请直接输入或粘贴您的剧本内容，我会整理后请您确认。", {"workflow": "wait_user_script"}
        if choice == "web":
            project_name = state.get("project_name") or ""
            materials, urls = vw.search_web_materials(project_name)
            if not materials:
                return (
                    f"搜索『{project_name}』网页资料失败（网络不可达或无有效资料）。"
                    "您可以改为自己提供剧本，或稍后再让我搜索。",
                    {"workflow": "search_failed"},
                )
            try:
                draft = vw.generate_script_from_materials(
                    project_name,
                    materials,
                    style=state.get("style"),
                    orientation=state.get("orientation"),
                )
            except Exception as e:
                return f"剧本生成失败：{e}。您可以重试或改为自己提供剧本。", {"workflow": "generate_failed"}
            state["stage"] = vw.STAGE_SCRIPT_DRAFT
            state["script_draft"] = draft
            state["sources"] = urls
            vw.set_workflow_state(db, s, state)
            _card = _open_script_review(db, s, state)
            return (
                f"我已根据网页资料（{len(urls)} 个来源）生成剧本草稿：\n\n{draft}\n\n"
                "请您确认这份剧本是否可以？回复『可以』我将保存剧本；"
                "如需修改，请直接提出改进建议，我会按建议修改后再次请您确认。",
                {"workflow": "script_draft", "sources": urls, "confirmation": _card},
            )
        return (
            "请选择剧本来源：1. 您自己输入剧本；2. 我搜索网页资料生成剧本。也可以直接说明您的选择。",
            {"workflow": "ask_source_again"},
        )

    if stage == vw.STAGE_WAIT_USER_SCRIPT:
        stripped = text.strip()
        if len(stripped) < 50:
            return (
                "剧本内容似乎太短了。请粘贴完整剧本（含场次标题、对白），"
                "或直接说明您想改由我搜索网页资料生成。",
                {"workflow": "script_too_short"},
            )
        state["stage"] = vw.STAGE_SCRIPT_DRAFT
        state["script_draft"] = stripped
        vw.set_workflow_state(db, s, state)
        _card = _open_script_review(db, s, state)
        return (
            "剧本已收到。请您确认这份剧本是否可以？回复『可以』我将保存剧本；"
            "如需修改，请直接提出改进建议，我会按建议修改后再次请您确认。",
            {"workflow": "script_draft", "confirmation": _card},
        )

    if stage in (vw.STAGE_SCRIPT_DRAFT, vw.STAGE_WAIT_REVISION):
        # STAGE_WAIT_REVISION：用户已驳回但未给出修改要求，本条消息即修改说明，
        # 直接进入修改流程，不重复询问同一个『是否可以』
        confirm = (
            vw.detect_script_confirmation(text)
            if stage == vw.STAGE_SCRIPT_DRAFT
            else "revise"
        )
        if confirm == "ok":
            from app.services.pipeline_tools import tool_save_script
            from app.services import review_confirmation as rc

            result = tool_save_script(db, state["project_id"], state.get("script_draft") or "")
            state["stage"] = vw.STAGE_SCRIPT_CONFIRMED
            state.pop("script_draft", None)
            vw.set_workflow_state(db, s, state)
            version = result.get("version") if isinstance(result, dict) else None
            # 审核通过必须绑定明确版本：保存成功后登记 approved_version
            if version is not None and state.get("project_id"):
                rc.record_script_approval(
                    db, project_id=state["project_id"], version=version
                )
            return (
                f"剧本已保存（版本 v{version if version is not None else '最新'}）。"
                "下一步只能先做剧本解析：解析完成后才能确定角色、场景、道具与分镜数量，"
                "之后才进入资产库环节。回复『开始解析』我将发起剧本分析。",
                {"workflow": "script_saved", "script_version": version},
            )
        # 既未明确通过，也未命中否定/修改关键词时：只要用户给出了具体意见，
        # 就按修改要求处理（不再重复抛出同一个『是否可以』）；
        # 『再看看』等暂缓表达保持待审核，避免误改
        from app.services import review_confirmation as _rc_parse

        if confirm != "revise":
            _parsed = _rc_parse.parse_confirmation_response(text)
            if _parsed["resolution"] == "defer":
                return (
                    "好的，当前剧本继续保持待审核状态，不会启动后续生成。"
                    "你可以继续提出修改意见。",
                    {"workflow": "confirm_deferred"},
                )
            if len(text.strip()) < 4:
                return (
                    "请确认剧本是否可以：回复『可以』我将保存剧本；如需修改，请直接提出改进建议。",
                    {"workflow": "confirm_again"},
                )
            confirm = "revise"
        if confirm == "revise":
            try:
                revised = vw.revise_script(
                    state.get("project_name") or "",
                    state.get("script_draft") or "",
                    text,
                    style=state.get("style"),
                    orientation=state.get("orientation"),
                )
            except Exception as e:
                return f"剧本修改失败：{e}。请重试或直接说明修改方向。", {"workflow": "revise_failed"}
            state["script_draft"] = revised
            state["revision_count"] = int(state.get("revision_count") or 0) + 1
            vw.set_workflow_state(db, s, state)
            # 新版本重新提交审核：旧确认事项标记为 revision_requested，
            # 修改后的内容作为新版本创建独立的待确认事项（旧版本结论不被覆盖）
            from app.services import review_confirmation as rc

            # 上一版确认事项可能已被裁决（rejected / revision_requested），
            # 因此版本与轮次取自最近一条确认事项（含已裁决），而不是仅查 pending
            _stale = rc.get_pending(db, s.id)
            if _stale is not None:
                rc.resolve(
                    db, _stale, resolution="revise", user_response=text, comment=text
                )
            _last = rc.latest_review(db, s.id)
            # project_id 在两个链路中的来源不同：工作流 state 由生成流程写入，
            # 会话级对话则取自 AgentSession.project_id，二者回退取值避免 KeyError
            _pid = state.get("project_id") or s.project_id
            _latest_sv = rc.latest_script_version(db, _pid)
            _base_version = max(
                int(getattr(_last, "target_version", 0) or 0),
                _latest_sv.version if _latest_sv is not None else 0,
            )
            _next_version = _base_version + 1 if _base_version else None
            _next_round = int(getattr(_last, "round", 0) or 0) + 1
            _card = _open_script_review(
                db, s, state, target_version=_next_version, round_no=_next_round
            )
            return (
                f"已按您的建议修改剧本（第 {state['revision_count']} 次修改）：\n\n{revised}\n\n"
                "请再次确认这份剧本是否可以？回复『可以』我将保存剧本；仍需修改请继续提出建议。",
                {"workflow": "script_revised", "confirmation": _card},
            )
        return (
            "请确认剧本是否可以：回复『可以』我将保存剧本；如需修改，请直接提出改进建议。",
            {"workflow": "confirm_again"},
        )

    return None, None


def _pending_label(confirmation) -> str:
    '''待确认事项的中文称谓（带版本号），不混用不同版本的审核状态。

    剧本尚未保存（无 ScriptVersion 行）时版本号为 0/空，此时称『剧本草稿』，
    不展示『第 0 版』这类不存在的版本，避免误导用户。
    '''
    base = '剧本' if (confirmation.target_type or '') == 'script' else '对象'
    version = confirmation.target_version
    if not version:
        return f'{base}草稿'
    return f'{base}第 {version} 版'


def _mentions_pending_target(text: str, pendings: list) -> bool:
    '''判断用户是否明确指代了某个待确认事项（版本号 / 对象类型）。'''
    lowered = (text or '').lower()
    for item in pendings:
        if item.target_version is not None:
            if ('第' + str(item.target_version) + '版') in lowered:
                return True
            if ('v' + str(item.target_version)) in lowered:
                return True
        head = (item.confirmation_type or '').split('_')[0]
        if head and head in lowered:
            return True
    return False


def _handle_pending_confirmation(
    db: Session, s: AgentSession, session_id: str, text: str
) -> tuple[str | None, dict | None]:
    '''确认答复解析（Confirmation Intent Resolver），优先于普通意图识别。

    - 存在有效待确认事项时，用户的简短回答先解析为该事项的答复，
      不能因为回答太短就认为缺少上下文；
    - 明确否定 -> rejected：立即更新业务状态，不再重复询问同一问题，
      并由后端门禁阻断依赖审核通过的后续任务；
    - 否定且带修改要求 -> 直接进入修改流程，不再追问改什么；
    - 暂缓 -> deferred，保持待审核；
    - 不确定或多对象歧义 -> 只澄清，绝不默认通过。
    '''
    from app.services import review_confirmation as rc

    pendings = rc.list_pending_all(db, session_id)
    if not pendings:
        return None, None

    parsed = rc.parse_confirmation_response(text)
    resolution = parsed['resolution']
    if resolution == 'unclear':
        # 不猜测：回落主对话，由上下文注入的待确认提示辅助理解
        return None, None

    if len(pendings) > 1 and not _mentions_pending_target(text, pendings):
        lines = [
            f'{i}. {_pending_label(c)}（待确认问题：{c.question or "未记录"}）'
            for i, c in enumerate(pendings, 1)
        ]
        return (
            '当前有多个待确认事项，请明确您指的是哪一项：\n' + '\n'.join(lines),
            {'route_action': 'confirmation_clarify', 'pending_count': len(pendings)},
        )

    confirmation = pendings[-1]
    label = _pending_label(confirmation)
    revision_request = parsed.get('revision_request')

    confirmation = rc.resolve(
        db,
        confirmation,
        resolution=resolution,
        user_response=text,
        comment=revision_request,
        matched_by='pending_confirmation',
    )
    base_payload = {
        'route_action': 'confirmation_resolved',
        'resolution': resolution,
        'confirmation': rc.to_card(confirmation),
    }

    if resolution == 'approve':
        # 剧本确认通过必须在此处完成『保存剧本 + 阶段推进』。
        # 若只回一句『准备进入下一阶段』就直接 return，会出现两个缺陷：
        # 1) 阶段永远停在 script_draft，_handle_video_workflow 的 script_draft
        #    分支不会再被调用，后续制作流程永不启动；
        # 2) script_draft 未被清除，下一轮对话再次命中草稿分支，
        #    导致同一份剧本被重复发送。
        from app.services import video_workflow as vw
        state = vw.get_workflow_state(s)
        if (
            (confirmation.target_type or '') == 'script'
            and state.get('stage') == vw.STAGE_SCRIPT_DRAFT
            and state.get('project_id')
        ):
            from app.services.pipeline_tools import tool_save_script
            try:
                result = tool_save_script(
                    db, state['project_id'], state.get('script_draft') or ''
                )
            except Exception as e:  # noqa: BLE001
                return (
                    f'剧本保存失败：{e}。请重试，或直接说明修改方向。',
                    {**base_payload, 'workflow': 'script_save_failed'},
                )
            version = result.get('version') if isinstance(result, dict) else None
            project_id = state['project_id']
            state['stage'] = vw.STAGE_SCRIPT_CONFIRMED
            state.pop('script_draft', None)
            vw.set_workflow_state(db, s, state)
            if version is not None:
                rc.record_script_approval(
                    db, project_id=project_id, version=version
                )
            vlabel = version if version is not None else '最新'
            base_payload['workflow'] = 'script_saved'
            base_payload['script_version'] = version
            return (
                f'收到，当前{label}已审核通过，剧本已保存（版本 v{vlabel}）。'
                '下一步只能先做剧本解析：解析完成后才能确定角色、场景、道具与分镜数量，'
                '之后才进入资产库环节。回复『开始解析』我将发起剧本分析。',
                base_payload,
            )
        return (
            f'收到，当前{label}已审核通过。我将检查后续依赖条件，准备进入下一阶段。',
            base_payload,
        )

    if resolution == 'defer':
        return (
            f'好的，当前{label}继续保持待审核状态，不会启动后续生成。'
            '你可以继续提出修改意见。',
            base_payload,
        )

    # reject / revise：均进入修改流程，且不启动后续依赖任务
    if revision_request:
        from app.services import video_workflow as vw

        state = vw.get_workflow_state(s)
        if state.get('stage') == vw.STAGE_SCRIPT_DRAFT and state.get('project_id'):
            try:
                revised = vw.revise_script(
                    state.get('project_name') or '',
                    state.get('script_draft') or '',
                    revision_request,
                    style=state.get('style'),
                    orientation=state.get('orientation'),
                )
            except Exception as e:  # noqa: BLE001
                return (
                    f'剧本修改失败：{e}。请重试或直接说明修改方向。',
                    {**base_payload, 'workflow': 'revise_failed'},
                )
            state['script_draft'] = revised
            state['revision_count'] = int(state.get('revision_count') or 0) + 1
            vw.set_workflow_state(db, s, state)
            # 新版本重新提交审核：版本号递增、轮次递增；旧版本的拒绝结论保留
            latest = rc.latest_script_version(db, state['project_id'])
            base_version = max(
                int(confirmation.target_version or 0),
                latest.version if latest is not None else 0,
            )
            next_version = base_version + 1
            question = (
                '请确认新版本剧本是否可以？回复『可以』我将保存；'
                '如需继续修改，请直接提出改进建议。'
            )
            new_confirmation = rc.create_script_review(
                db,
                session_id=session_id,
                project_id=state['project_id'],
                question=question,
                target_version=next_version,
                round_no=int(confirmation.round or 1) + 1,
            )
            return (
                f'收到，当前{label}审核未通过。已按您的要求调整（第 '
                f'{state["revision_count"]} 次修改），修改后剧本如下：\n\n{revised}\n\n'
                f'这是一份新版本（第 {next_version} 版）。{question}',
                {
                    **base_payload,
                    'workflow': 'script_revised',
                    'confirmation': rc.to_card(new_confirmation),
                },
            )
        return (
            f'收到，当前{label}审核未通过。我会重点按您的要求调整：{revision_request}。'
            '修改完成后重新提交审核，在此之前不会启动后续资产生成任务。',
            base_payload,
        )

    return (
        f'收到，当前{label}暂不通过。我不会继续执行后续资产生成任务。'
        '请告诉我需要修改的内容，我会修改后重新提交审核。',
        base_payload,
    )


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

    # 1. 持久化用户消息（seq 并发冲突重试：并发 chat 撞 (session_id, seq) 唯一索引时
    # 回滚并重新计算 max_seq，最多 3 次，避免直接 500）
    from sqlalchemy.exc import IntegrityError

    user_msg: AgentMessage | None = None
    for _seq_attempt in range(3):
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
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            user_msg = None
            continue
    if user_msg is None:
        raise biz_error(code="seq_conflict", http=503, message="消息序号冲突，请重试")
    db.refresh(user_msg)

    # 1.30 待确认事项优先解析（Confirmation Intent Resolver）
    # 存在有效待确认事项时，用户的『可以 / 不可以 / 退回 / 再改改 / 再看看』
    # 必须先按该事项解释，不能因回答过短就当普通闲聊；解析结论直接改业务状态。
    pending_reply, pending_payload = _handle_pending_confirmation(
        db, s, session_id, payload.content
    )
    if pending_reply is not None:
        # 裁决会额外写入 role=approval 审计消息占用 seq，助手回复须重新取号，
        # 否则同一会话 (session_id, seq) 唯一索引会冲突
        next_seq = (
            db.scalar(
                select(func.max(AgentMessage.seq)).where(
                    AgentMessage.session_id == session_id
                )
            )
            or 0
        ) + 1
        pending_msg = AgentMessage(
            id=agent_message_id(),
            session_id=session_id,
            role="assistant",
            content=pending_reply,
            payload=pending_payload or {},
            seq=next_seq,
            created_at=datetime.now(),
        )
        db.add(pending_msg)
        s.updated_at = datetime.now()
        db.commit()
        db.refresh(pending_msg)
        return {
            "user_message": AgentMessageOut.model_validate(user_msg).model_dump(mode="json"),
            "assistant_message": AgentMessageOut.model_validate(pending_msg).model_dump(mode="json"),
        }

    # 存在待确认事项且本条不是可判定的答复：跳过视频工作流，避免再次抛出
    # 同一个『请确认剧本是否可以』问题（重复询问是本次整改要消除的缺陷）
    from app.services import review_confirmation as _rc

    has_pending = _rc.get_pending(db, session_id) is not None

    # 1.35 视频生成工作流（『生成XX视频』端到端流程：项目查重/创建 → 剧本来源 → 生成 → 确认循环）
    # 命中工作流时直接落助手消息返回，不进入意图路由与主对话
    if has_pending:
        wf_reply, wf_payload = None, None
    else:
        wf_reply, wf_payload = _handle_video_workflow(db, s, payload.content)
    if wf_reply is not None:
        assistant_msg = AgentMessage(
            id=agent_message_id(),
            session_id=session_id,
            role="assistant",
            content=wf_reply,
            payload={"route_action": "video_workflow", **(wf_payload or {})},
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

    # 1.3.5 联网搜索分支（联网搜索整改 Phase 3）：
    # 确定性识别搜索请求（『搜索/搜一下/查一下/联网查』等指令前缀），
    # 执行真实搜索（SearchRun 状态机 + 日志先行 + 结果验证 + 网页读取），
    # 再由 LLM 基于真实搜索结果总结（不伪装搜索、不编造结果、保留来源 URL）
    import re as _re_search

    _SEARCH_PATTERN = _re_search.compile(
        r"(帮我|麻烦|请)?(联网|上网|在线)?(搜索|搜一下|搜下|查一下|查查|查询|查下)"
    )
    if _SEARCH_PATTERN.search(payload.content[:200]):
        from app.services.search_service import run_web_search

        search_record = run_web_search(
            db,
            payload.content,
            session_id=session_id,
            project_id=s.project_id,
        )
        if search_record["status"] in ("failed", "empty"):
            # 搜索失败/空结果：如实反馈，不编造结果
            fail_reply = (
                f"联网搜索失败（{search_record.get('error') or '无有效结果'}）。"
                "您可以调整关键词后重试，或直接描述您想了解的内容。"
                if search_record["status"] == "failed"
                else f"搜索『{search_record['query']}』没有找到有效结果。"
                "建议调整关键词后重试，或直接描述您想了解的内容。"
            )
            assistant_msg = AgentMessage(
                id=agent_message_id(),
                session_id=session_id,
                role="assistant",
                content=fail_reply,
                payload={"route_action": "web_search", "search": search_record},
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
        # 搜索成功/partial：组装真实搜索结果上下文，LLM 基于此总结
        from app.services.search_service import get_search_run_detail

        search_detail = get_search_run_detail(db, search_record["search_run_id"])
        result_lines = []
        for i, item in enumerate((search_detail or {}).get("results") or [], 1):
            content_part = f" 正文摘要：{item.get('content')[:1500]}" if item.get("content") else ""
            result_lines.append(
                f"{i}. {item['title']}（{item['domain']}，读取状态 {item['fetch_status']}）"
                f"\n   链接：{item['url']}"
                f"\n   摘要：{item['snippet'][:300]}{content_part}"
            )
        search_context = (
            f"以下是系统刚执行的真实联网搜索结果（关键词：{search_record['query']}，"
            f"引擎：{search_record['provider']}，{search_record['valid_result_count']} 条有效结果，"
            f"耗时 {search_record['duration_ms']}ms）。请只基于这些结果回答用户问题，"
            "重要事实保留来源链接，读取失败的来源不要引用其正文：\n\n" + "\n\n".join(result_lines)
        )
        from app.integrations.llm import LLMClient as _SearchLLMClient

        try:
            search_reply = _SearchLLMClient().chat_messages(
                [
                    {"role": "system", "content": _DIRECTOR_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"{payload.content}\n\n{search_context}",
                    },
                ],
                json_mode=False,
            ).strip()
        except Exception as e:
            # LLM 总结失败：直接返回搜索结果列表（真实结果仍然可用，不因总结失败丢弃）
            search_reply = (
                f"已为您完成联网搜索（关键词：{search_record['query']}，"
                f"{search_record['valid_result_count']} 条有效结果）。结果摘要总结失败（{e}），"
                "以下是来源列表：\n\n" + "\n".join(
                    f"- {item['title']}：{item['url']}"
                    for item in (search_detail or {}).get("results") or []
                )
            )
        assistant_msg = AgentMessage(
            id=agent_message_id(),
            session_id=session_id,
            role="assistant",
            content=search_reply,
            payload={"route_action": "web_search", "search": search_record},
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

    # 1.4 意图路由（阶段 E：意图路由工具化，单 Agent 框架 P1-2）
    # 模型只做分类，路由策略由确定性代码执行；分类失败降级为 query 继续主对话
    from app.services.intent_router import (
        GENERATE_GUIDE_MESSAGE,
        UNSAFE_REJECT_MESSAGE,
        build_clarification_question,
        classify_intent,
        route_action,
    )
    from app.services.agent_governance import NextAction

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
    # Context Manager（意图识别改造 Phase 2）：结构化焦点/操作上下文注入意图识别，
    # 供『重新生成』『继续修改』『它』等指代消解（焦点经数据库验证后才注入）
    from app.services import context_manager

    ctx = context_manager.load_context(s)
    focus = context_manager.validate_focus(db, s, current_seq=max_seq + 1)
    focus_hint = context_manager.build_focus_hint(focus, ctx.operation, ctx.execution)
    # Pending Confirmation 优先参与上下文解析：存在有效待确认事项时，
    # 把『正在等待确认什么』一并交给意图识别，用户的短回答不会因过短而丢失上下文
    _pending_hint = _rc.build_pending_hint(db, session_id)
    if _pending_hint:
        focus_hint = (_pending_hint + "\n" + focus_hint) if focus_hint else _pending_hint
    intent = classify_intent(
        payload.content,
        recent_messages=[{"role": m.role, "content": m.content} for m in recent],
        session_summary=s.summary,
        focus_hint=focus_hint,
        # 上下文有效性以经数据库验证的焦点实体或有效待确认事项为准，
        # 不能用 focus_hint 是否为空判断（存在执行态即非空）
        has_valid_context=bool(focus is not None or _pending_hint),
    )
    # Intent Resolver（意图识别改造 Phase 2）：融合意图结果与结构化上下文，
    # 实体引用解析优先级：显式 ID > 焦点上下文 > 澄清；实体 ID 一律数据库验证
    from app.services.intent_resolver import resolve_intent

    resolved = resolve_intent(db, s, payload.content, intent, current_seq=max_seq + 1)
    # Context Manager（Phase 1）：登记本轮执行态（意图/子类/置信度档位/目标/阶段），
    # 供下一轮指代消解与上下文重判；context_state 为 JSON 字段，无需数据库迁移
    context_manager.update_execution(
        s,
        intent=resolved.intent,
        subcategory=resolved.subcategory,
        confidence=resolved.confidence,
        confidence_tier=resolved.confidence_tier,
        target=resolved.target,
        stage="pending" if resolved.requires_clarification else "running",
        updated_seq=max_seq + 1,
    )
    if resolved.focus_updated:
        # 焦点更新随本轮事务持久化（context_state 变更）
        db.add(s)
    # Parent Agent（意图识别改造 Phase 3）：执行类意图创建任务运行时记录
    # （AgentTask 落库：goal/risk_level/confidence/success_criteria 快照），
    # 任务状态由各执行分支流转（completed/failed），query/analysis/ambiguous 不建任务
    from app.services import parent_agent

    task = parent_agent.create_task_from_intent(db, session_id, user_msg, resolved)
    # 统一任务编排层（Phase 2）：执行类意图同步创建统一 Task（幂等：同一 agent_task_id 复用已有 Task）
    from app.services import task_orchestrator
    unified_task = None
    if task is not None:
        unified_task = task_orchestrator.create_task(
            db,
            session_id=session_id,
            task_type=resolved.intent,
            title=task.intent_goal,
            agent_task_id=task.id,
            project_id=s.project_id,
        )
    action = route_action(intent.intent, intent.subcategory)
    # Intent Resolver 判定需澄清：目标无法唯一定位时不猜测执行，向用户澄清
    if resolved.requires_clarification and action in (NextAction.GUIDE, NextAction.PIPELINE):
        action = NextAction.CLARIFY
    # 结构化意图（B 档）：ambiguous 且带缺失信息时向用户澄清，不进入主对话（不虚构目标）
    if intent.intent == "ambiguous" and intent.missing_information:
        action = NextAction.CLARIFY
    # 生成请求预解析：定位不到目标时不落固定文案，回落主对话由大模型结合上下文兜底
    guide_reply, guide_approval = None, None
    if action == NextAction.GUIDE:
        guide_reply, guide_approval = _try_create_generate_approval(db, s, session_id, payload.content)
        if guide_reply is None:
            action = None  # 回落主对话：大模型上下文兜底（规则不兜底）
    if action in (NextAction.REJECT, NextAction.GUIDE, NextAction.CLARIFY, NextAction.PIPELINE):
        # unsafe：直接拒绝；generate_request：不执行生成，引导到对应页面（P0：生成不在 Agent 直接执行范围）；
        # pipeline：流程节点操作，经状态机门禁执行流程工具（高风险操作只建审批申请）；
        # clarify：信息不足，向用户澄清缺失信息（确定性拼接，不调用模型）
        approval_info: dict | None = None
        if action == NextAction.REJECT:
            reply = UNSAFE_REJECT_MESSAGE
        elif action == NextAction.GUIDE:
            # 生成请求：能唯一定位到镜头/实体则落地为审批申请（复用预解析结果）
            reply, approval_info = guide_reply, guide_approval
        elif action == NextAction.PIPELINE:
            # 流程操作：意图识别流程节点，经状态机门禁执行（审核闸口/审批纪律不跳过）
            reply, approval_info = _handle_pipeline_action(db, s, session_id, intent, payload.content)
        else:
            # Intent Resolver 判定需澄清时优先使用其确定性澄清问题（含目标定位失败原因）
            reply = (
                resolved.clarification_question
                if resolved.requires_clarification and resolved.clarification_question
                else build_clarification_question(intent.missing_information)
            )
        # Parent Agent 任务状态流转（Phase 3）：本轮同步分支结束时按结果落终态
        if task is not None:
            if action == NextAction.GUIDE and approval_info:
                # 生成请求落地为审批申请：成功则任务完成（后续由审批流接管），
                # 提交失败则任务标 failed（摘要与事实一致，不虚报成功）
                if approval_info.get("ok"):
                    parent_agent.complete_task(db, task, "已创建生成审批申请，等待人工审批")
                    if unified_task is not None:
                        task_orchestrator.complete_task(db, unified_task, summary="已创建生成审批申请，等待人工审批")
                else:
                    parent_agent.fail_task(db, task, str(reply)[:2000])
                    if unified_task is not None:
                        task_orchestrator.fail_task(db, unified_task, error_code="approval_submit_failed", error_msg=str(reply)[:2000])
            elif action == NextAction.PIPELINE:
                parent_agent.complete_task(db, task, str(reply)[:2000])
                if unified_task is not None:
                    task_orchestrator.complete_task(db, unified_task, summary=str(reply)[:2000])
            else:
                # 澄清/拒绝：任务取消（用户补充信息后将产生新任务）
                parent_agent.cancel_task(db, task, f"route={action}")
                if unified_task is not None:
                    task_orchestrator.cancel_task(db, unified_task, reason=f"route={action}")
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
                # 细粒度意图与分级置信度审计（Phase 1）：子类、档位、兜底出口
                "intent_subcategory": intent.subcategory,
                "confidence_tier": intent.confidence_tier,
                "route_exit": intent.route_exit,
                "approval": approval_info,
                # Intent Resolver 审计（意图识别改造 Phase 2）：目标解析路径与结果
                "intent_resolution": {
                    "resolution_path": resolved.resolution_path,
                    "target": resolved.target,
                    "requires_clarification": resolved.requires_clarification,
                    "clarification_code": resolved.clarification_code,
                    "subcategory": resolved.subcategory,
                    "confidence_tier": resolved.confidence_tier,
                },
                # Parent Agent 任务审计（Phase 3）：任务 ID 与终态（前端任务计划区域展示用）
                "task_id": task.id if task is not None else None,
                "task_status": task.status if task is not None else None,
                "task_card": task_orchestrator.task_payload(db, unified_task) if unified_task is not None else None,
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
    # 本轮可展示的资产引用（供前端气泡内渲染缩略图；只含 asset_id 等元数据，
    # 不含文件系统路径，前端经 /api/assets/{id}/file?thumb=true 取图）
    asset_refs: list[dict] = []
    if s.project_id:
        from app.services.agent_tools import execute_tool, get_allowed_tools

        # 意图-工具映射（B 档）：只执行当前意图候选的工具，降低选错/越权/Token 消耗；
        # 识别失败降级为 query 时映射到全部只读工具，行为与阶段 E 一致
        allowed_tools = get_allowed_tools(intent.intent)
        ctx_lines: list[str] = []
        base_tools = [
            ("project_overview", {"project_id": s.project_id}),
            ("asset_stats", {"project_id": s.project_id}),
            ("list_jobs", {"project_id": s.project_id, "limit": 5}),
        ]
        # 资产缩略图仅在用户明确想看图片/视频时注入，避免每条消息都附带缩略图（UI 噪音 +
        _asset_hint = ("图", "视频", "片段", "素材", "缩略", "画面", "看看", "长什么样")
        if any(k in payload.content for k in _asset_hint):
            base_tools.append(
                (
                    "list_assets",
                    {"project_id": s.project_id, "limit": 6},
                )
            )
        from app.services.agent_context_tools import build_read_tool_calls, format_tool_context

        tool_calls = []
        # 硬性预算（agent_governance）：单轮工具调用数/总耗时/上下文字符数上限，
        # 超限安全降级（跳过后续工具调用），违规记录进 violations 供审计
        from app.services.agent_governance import ToolBudget

        budget = ToolBudget()

        def _run(tool_name: str, tool_args: dict):
            """执行受控工具并记录（不在 allowed_tools 内或预算超限的不执行）。"""
            if tool_name not in allowed_tools:
                return None
            violation = budget.check_before(tool_name)
            if violation:
                budget.violations.append(violation)
                return None
            result = execute_tool(db, tool_name, **tool_args)
            budget.record(result.duration_ms)
            tool_calls.append((tool_name, tool_args, result))
            return result

        for tool_name, tool_args in base_tools:
            _run(tool_name, tool_args)
        # 读工具调用（意图驱动）：进度快照 / 解析状态 / 实体与镜头清单 / 失败任务诊断
        # 统一走注册表里的只读工具，保证 Agent 侧与页面接口同口径，不再重复实现统计逻辑
        for tool_name, tool_args in build_read_tool_calls(payload.content, s.project_id):
            result = _run(tool_name, tool_args)
            if result is not None and result.ok and result.data:
                ctx_lines.append(format_tool_context(tool_name, result))
        overview = next((r for n, _, r in tool_calls if n == "project_overview"), None)
        stats = next((r for n, _, r in tool_calls if n == "asset_stats"), None)
        jobs = next((r for n, _, r in tool_calls if n == "list_jobs"), None)
        assets = next((r for n, _, r in tool_calls if n == "list_assets"), None)
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
        if assets is not None and assets.ok and assets.data and assets.data["count"] > 0:
            # 资产清单：注入文本上下文，并收集 asset_refs 供前端气泡渲染缩略图
            asset_refs = [
                {
                    "asset_id": it["asset_id"],
                    "asset_type": it["asset_type"],
                    "name": it["name"],
                    "has_thumb": it["has_thumb"],
                }
                for it in assets.data["items"]
            ]
            asset_lines = ", ".join(
                f"{it['asset_type']}({it['name']} v{it['version']})"
                for it in assets.data["items"][:6]
            )
            ctx_lines.append(
                f"最近生成的资产（新→旧，本次展示 {assets.data['count']} 项）：{asset_lines}"
            )
        if ctx_lines:
            # 上下文预算：工具上下文总字符数超限时从尾部丢弃（保留高优先级的概览/统计），
            # 违规记录进 budget.violations 供审计
            kept: list[str] = []
            for line in ctx_lines:
                if budget.context_allowed(len(line) + 1):
                    kept.append(line)
                    budget.record_context(len(line) + 1)
                else:
                    budget.violations.append(f"context_overflow:dropped_line_chars={len(line)}")
            if kept:
                system_prompt += "\n以下是当前关联项目的真实状态（由系统查询提供，回答项目相关问题以此为准）：\n" + "\n".join(
                    kept
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
    # 2.5 会话长期记忆：摘要注入系统提示词；窗口外未摘要消息达阈值时 LLM 压缩落库
    # （压缩失败不阻断对话：保留旧摘要，短期窗口照常工作；随主流程事务一起提交）
    from app.services.session_memory import build_summary_context, maybe_compress_session

    summary_ctx = build_summary_context(s)
    if summary_ctx:
        system_prompt += "\n\n" + summary_ctx
    # 2.6 长期记忆注入（记忆系统迭代一）：项目记忆/用户偏好/经验规则作为背景补充；
    # 记忆不得覆盖用户当前指令（注入文本中已显式声明，注入上限由确定性代码控制）
    from app.services.agent_memory import build_memory_context

    memory_ctx = build_memory_context(db, s.project_id)
    if memory_ctx:
        system_prompt += "\n\n" + memory_ctx
    if history:
        maybe_compress_session(db, s, history[0].seq)
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
        # Parent Agent：LLM 失败时任务落 failed 终态（不悬挂在 pending；
        # 用户重试会产生新消息与新任务，旧任务保留供审计）
        if task is not None:
            parent_agent.fail_task(db, task, f"llm_error: {last_err.code}")
            if unified_task is not None:
                task_orchestrator.fail_task(db, unified_task, error_code="llm_error", error_msg=last_err.code)
            db.commit()
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
            # 资产引用：前端据此渲染缩略图（assistant 气泡内）
            "asset_refs": asset_refs,
            # 预算审计（agent_governance）：本轮工具调用数/耗时/上下文字符数与违规记录
            "tool_budget": budget.audit_payload() if s.project_id else None,
            # Parent Agent 任务审计（Phase 3）：GUIDE 回落主对话/CHAT 时任务由主对话闭环
            "task_id": task.id if task is not None else None,
            "task_status": task.status if task is not None else None,
            "task_card": task_orchestrator.task_payload(db, unified_task) if unified_task is not None else None,
        },
        seq=tool_event_seq + 1,
        created_at=datetime.now(),
    )
    db.add(assistant_msg)
    # Parent Agent：主对话分支闭环任务（GUIDE 回落/CHAT 的执行类意图由主对话完成）
    if task is not None:
        parent_agent.complete_task(db, task, str(reply)[:2000])
        if unified_task is not None:
            task_orchestrator.complete_task(db, unified_task, summary=str(reply)[:2000])
    s.updated_at = datetime.now()
    db.commit()
    db.refresh(assistant_msg)

    return {
        "user_message": AgentMessageOut.model_validate(user_msg).model_dump(mode="json"),
        "assistant_message": AgentMessageOut.model_validate(assistant_msg).model_dump(mode="json"),
    }
