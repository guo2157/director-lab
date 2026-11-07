"""待确认事项管理器（Pending Confirmation Manager）+ 确认答复解析器。

核心原则：
1. 确认事项必须持久化，页面刷新 / Agent 重启 / 会话恢复后可重新读取；
2. 用户简短答复优先按当前待确认事项解释；
3. 用户拒绝后不重复询问同一问题，也不继续执行依赖审核通过的任务；
4. 审核结论绑定明确对象与版本（通过第 3 版不等于通过第 4 版）；
5. 不确定时只澄清，绝不默认通过；
6. 幂等：重复提交同一答复不重复改业务状态、不重复建修改任务；
7. 业务状态由确定性代码裁决，LLM 只负责措辞。

与 approvals.py 分工：approvals 管 kind=tool_approval 的工具审批门禁，
本模块管 kind=artifact_review 的业务对象人工审核，共用 agent_confirmation 表。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AgentConfirmation, AgentMessage, Project, ScriptVersion
from app.models.ids import agent_confirmation_id, agent_message_id

KIND_TOOL_APPROVAL = "tool_approval"
KIND_ARTIFACT_REVIEW = "artifact_review"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_REVISION_REQUESTED = "revision_requested"
STATUS_DEFERRED = "deferred"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"

VALID_STATUSES = (
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_REVISION_REQUESTED,
    STATUS_DEFERRED,
    STATUS_EXPIRED,
    STATUS_CANCELLED,
)

# 阻断依赖审核通过的后续任务的状态
BLOCKING_STATUSES = (STATUS_REJECTED, STATUS_REVISION_REQUESTED)

# 答复类型 -> 业务状态（确定性映射，禁止 LLM 自行决定）
RESOLUTION_STATUS = {
    "approve": STATUS_APPROVED,
    "reject": STATUS_REJECTED,
    "revise": STATUS_REVISION_REQUESTED,
    "defer": STATUS_DEFERRED,
}
ALLOWED_RESPONSES = ["approve", "reject", "revise", "defer"]
# 待确认事项有效期：超过后惰性过期（读取时判定），避免遗忘的 pending 长期阻塞视频工作流
PENDING_TTL = timedelta(hours=24)

TARGET_SCRIPT = "script"
TYPE_SCRIPT_REVIEW = "script_review"
_SCRIPT_REVIEW_KEY = "script_review"

# 判定顺序：暂缓 -> 否定（含修改要求）-> 单独修改诉求 -> 肯定 -> 不确定
# 『不可以』内含『可以』，必须先判否定，否则会被误判为通过。
_DEFER_RE = re.compile(r"再看看|先等等|暂时不|等一下|稍后再|待定|再议|先放着|回头再|等等再说")
_NEG_RE = re.compile(
    r"不可以|不行|不合适|不通过|不同意|不满意|不好|不对|不准确|有误|错误|不符合|不够|不太行|不能通过"
)
_REVISE_RE = re.compile(
    r"修改|改一下|改改|调整|再改|重写|重新写|重来|重新生成|补充|增加|删掉|去掉|换个|换一|"
    r"太长|太短|加强|强化|弱化|突出|淡化|优化"
)
_APPROVE_RE = re.compile(
    r"^(可以|可以了|行|好的|好|没问题|确认|确认保存|同意|通过|保存|ok|okay|就这样|就这个|按这个来)",
    re.IGNORECASE,
)
_LEAD_NOISE_RE = re.compile(r"^[\s,，.。!！?？、:：;；~]+")
_SELF_NOISE_RE = re.compile(r"^(这个|这份|这版|剧本|但是|但|不过|就是|我觉得|感觉|有点|还是)+")


def _extract_revision_request(text: str) -> str | None:
    """从否定语句中提取具体修改要求（用户已给要求就不再追问改什么）。"""
    cleaned = _NEG_RE.sub("", text or "")
    cleaned = _LEAD_NOISE_RE.sub("", cleaned)
    cleaned = _SELF_NOISE_RE.sub("", cleaned)
    cleaned = cleaned.strip(" \t\r\n,，.。!！?？~")
    return cleaned if len(cleaned) >= 4 else None


def parse_confirmation_response(text: str) -> dict:
    """把用户消息解析为对当前待确认事项的答复。

    返回 resolution=approve/reject/revise/defer/unclear，只做语义归类，
    是否真正关联某条待确认事项由调用方决定。
    """
    raw = (text or "").strip()
    if not raw:
        return {"resolution": "unclear", "revision_request": None, "reason": "empty"}

    if _DEFER_RE.search(raw):
        return {"resolution": "defer", "revision_request": None, "reason": "defer_keyword"}

    neg = _NEG_RE.search(raw)
    revise_hit = _REVISE_RE.search(raw)
    if neg:
        return {
            "resolution": "reject",
            "revision_request": _extract_revision_request(raw),
            "reason": "negation_keyword",
        }

    approve_hit = _APPROVE_RE.search(raw)
    if approve_hit and revise_hit:
        return {
            "resolution": "unclear",
            "revision_request": None,
            "reason": "approve_and_revise_conflict",
        }

    if revise_hit:
        return {"resolution": "revise", "revision_request": raw, "reason": "revision_keyword"}

    if approve_hit:
        tail = raw[approve_hit.end():].strip(" \t\r\n,，.。!！?？~")
        if len(tail) > 8:
            return {
                "resolution": "unclear",
                "revision_request": None,
                "reason": "approve_with_long_tail",
            }
        return {"resolution": "approve", "revision_request": None, "reason": "approve_prefix"}

    return {"resolution": "unclear", "revision_request": None, "reason": "no_keyword"}


def _now() -> datetime:
    return datetime.now()


def _next_seq(db: Session, session_id: str) -> int:
    current = db.scalar(
        select(func.max(AgentMessage.seq)).where(AgentMessage.session_id == session_id)
    )
    return int(current or 0) + 1


def build_idempotency_key(
    *,
    session_id: str,
    project_id: str | None,
    target_type: str,
    target_version: int | None,
    round_no: int = 1,
    confirmation_type: str = TYPE_SCRIPT_REVIEW,
) -> str:
    """同一会话 + 项目 + 对象 + 版本 + 轮次 + 类型 = 同一确认事项。"""
    payload = json.dumps(
        {
            "session": session_id,
            "project": project_id or "",
            "target_type": target_type,
            "target_version": target_version,
            "round": round_no,
            "type": confirmation_type,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def latest_script_version(db: Session, project_id: str) -> ScriptVersion | None:
    return (
        db.query(ScriptVersion)
        .filter(ScriptVersion.project_id == project_id)
        .order_by(ScriptVersion.version.desc())
        .first()
    )


def script_review_state(project: Project | None) -> dict:
    """读取项目中持久化的剧本审核状态（无记录时返回空 dict）。"""
    if project is None:
        return {}
    settings = project.settings or {}
    state = settings.get(_SCRIPT_REVIEW_KEY) or {}
    return dict(state) if isinstance(state, dict) else {}


def is_downstream_blocked(state: dict) -> bool:
    """剧本被拒 / 待修改时阻断依赖审核通过的后续制作任务。"""
    return (state or {}).get("status") in BLOCKING_STATUSES


def _write_script_state(
    db: Session,
    project: Project,
    *,
    status: str,
    version: int | None,
) -> dict:
    """把审核结论写回业务数据（Project.settings.script_review），不只留在聊天里。"""
    current = script_review_state(project)
    approved_version = current.get("approved_version")
    if status == STATUS_APPROVED:
        approved_version = version
    new_state = dict(current)
    new_state.update(
        {
            "status": status,
            "review_status": status,
            "reviewed_version": version,
            "updated_at": _now().isoformat(timespec="seconds"),
        }
    )
    if approved_version is not None:
        new_state["approved_version"] = approved_version
    settings = dict(project.settings or {})
    settings[_SCRIPT_REVIEW_KEY] = new_state
    project.settings = settings  # 重新赋值，保证 JSON 字段变更被检测到
    db.add(project)
    return new_state


def expire_stale_pending(db: Session, session_id: str) -> int:
    """惰性过期：把超过有效期的 pending 确认置为 expired 并落审计消息。
    - 只在读取入口调用（get_pending / list_pending_all），不引入定时任务
    - 过期不改业务审核结论（未通过仍是未通过），只解除对会话的阻塞
    """
    deadline = _now() - PENDING_TTL
    stale = list(
        db.scalars(
            select(AgentConfirmation).where(
                AgentConfirmation.kind == KIND_ARTIFACT_REVIEW,
                AgentConfirmation.session_id == session_id,
                AgentConfirmation.status == STATUS_PENDING,
                AgentConfirmation.created_at < deadline,
            )
        ).all()
    )
    for confirmation in stale:
        confirmation.status = STATUS_EXPIRED
        confirmation.resolved_at = _now()
        confirmation.resolution = {
            "resolution": "expire",
            "matched_by": "ttl",
            "note": "pending confirmation expired after 24h",
        }
        db.add(
            AgentMessage(
                id=agent_message_id(),
                session_id=session_id,
                role="approval",
                content="",
                payload={
                    "confirmation_id": confirmation.id,
                    "kind": confirmation.kind,
                    "confirmation_type": confirmation.confirmation_type,
                    "target_type": confirmation.target_type,
                    "target_id": confirmation.target_id,
                    "target_version": confirmation.target_version,
                    "action": STATUS_EXPIRED,
                    "resolution": "expire",
                    "comment": None,
                    "user_response": None,
                },
                seq=_next_seq(db, session_id),
                created_at=_now(),
            )
        )
    if stale:
        db.commit()
    return len(stale)


def get_pending(db: Session, session_id: str) -> AgentConfirmation | None:
    """当前会话最新的有效待确认事项（优先于普通自然语言意图识别）。"""
    expire_stale_pending(db, session_id)
    return db.scalars(
        select(AgentConfirmation)
        .where(
            AgentConfirmation.kind == KIND_ARTIFACT_REVIEW,
            AgentConfirmation.session_id == session_id,
            AgentConfirmation.status == STATUS_PENDING,
        )
        .order_by(AgentConfirmation.created_at.desc())
    ).first()


def list_pending_all(db: Session, session_id: str) -> list[AgentConfirmation]:
    """会话内全部待确认事项（用于多对象歧义判定）。"""
    expire_stale_pending(db, session_id)
    return list(
        db.scalars(
            select(AgentConfirmation)
            .where(
                AgentConfirmation.kind == KIND_ARTIFACT_REVIEW,
                AgentConfirmation.session_id == session_id,
                AgentConfirmation.status == STATUS_PENDING,
            )
            .order_by(AgentConfirmation.created_at.asc())
        ).all()
    )


def latest_review(db: Session, session_id: str) -> AgentConfirmation | None:
    """会话内最近一条确认事项（含已裁决），用于修改后按版本递增重新提交。"""
    return db.scalars(
        select(AgentConfirmation)
        .where(
            AgentConfirmation.kind == KIND_ARTIFACT_REVIEW,
            AgentConfirmation.session_id == session_id,
        )
        .order_by(AgentConfirmation.created_at.desc())
    ).first()


def list_reviews(db: Session, session_id: str, limit: int = 20) -> list[AgentConfirmation]:
    """会话内确认事项历史（新的在前），供前端确认卡片渲染。"""
    return list(
        db.scalars(
            select(AgentConfirmation)
            .where(
                AgentConfirmation.kind == KIND_ARTIFACT_REVIEW,
                AgentConfirmation.session_id == session_id,
            )
            .order_by(AgentConfirmation.created_at.desc())
            .limit(limit)
        ).all()
    )


def create_script_review(
    db: Session,
    *,
    session_id: str,
    project_id: str,
    question: str,
    target_version: int | None = None,
    round_no: int = 1,
) -> AgentConfirmation:
    """创建剧本审核确认事项（幂等）：同版本同轮次已有 pending 时直接复用。"""
    version = target_version
    if version is None:
        sv = latest_script_version(db, project_id)
        version = sv.version if sv is not None else 0
    key = build_idempotency_key(
        session_id=session_id,
        project_id=project_id,
        target_type=TARGET_SCRIPT,
        target_version=version,
        round_no=round_no,
    )
    existing = db.scalars(
        select(AgentConfirmation).where(
            AgentConfirmation.idempotency_key == key,
            AgentConfirmation.status == STATUS_PENDING,
        )
    ).first()
    if existing is not None:
        return existing

    confirmation = AgentConfirmation(
        id=agent_confirmation_id(),
        kind=KIND_ARTIFACT_REVIEW,
        task_id=None,
        tool_name="",
        args=None,
        status=STATUS_PENDING,
        comment=None,
        created_at=_now(),
        session_id=session_id,
        project_id=project_id,
        target_type=TARGET_SCRIPT,
        target_id=project_id,
        target_version=version,
        round=round_no,
        confirmation_type=TYPE_SCRIPT_REVIEW,
        question=question,
        allowed_responses=ALLOWED_RESPONSES,
        idempotency_key=key,
    )
    db.add(confirmation)

    project = db.get(Project, project_id)
    if project is not None:
        _write_script_state(db, project, status=STATUS_PENDING, version=version)
    db.commit()
    db.refresh(confirmation)
    return confirmation


def resolve(
    db: Session,
    confirmation: AgentConfirmation,
    *,
    resolution: str,
    user_response: str | None = None,
    comment: str | None = None,
    matched_by: str = "rule",
) -> AgentConfirmation:
    """裁决确认事项：更新状态 + 写业务状态 + 落审计消息（幂等）。"""
    if confirmation.status != STATUS_PENDING:
        return confirmation

    new_status = RESOLUTION_STATUS.get(resolution)
    if new_status is None:
        # 不确定：保持 pending，只记录原始答复，交由调用方澄清
        confirmation.user_response = user_response
        db.commit()
        return confirmation

    confirmation.status = new_status
    confirmation.user_response = user_response
    if comment:
        confirmation.comment = comment
    confirmation.resolution = {
        "resolution": resolution,
        "matched_by": matched_by,
        "note": comment or "",
    }
    confirmation.resolved_at = _now()

    session_id = confirmation.session_id
    if confirmation.target_type == TARGET_SCRIPT and confirmation.project_id:
        project = db.get(Project, confirmation.project_id)
        if project is not None:
            _write_script_state(
                db,
                project,
                status=new_status,
                version=confirmation.target_version,
            )

    if session_id:
        db.add(
            AgentMessage(
                id=agent_message_id(),
                session_id=session_id,
                role="approval",
                content="",
                payload={
                    "confirmation_id": confirmation.id,
                    "kind": confirmation.kind,
                    "confirmation_type": confirmation.confirmation_type,
                    "target_type": confirmation.target_type,
                    "target_id": confirmation.target_id,
                    "target_version": confirmation.target_version,
                    "action": new_status,
                    "resolution": resolution,
                    "comment": comment,
                    "user_response": user_response,
                },
                seq=_next_seq(db, session_id),
                created_at=_now(),
            )
        )
    db.commit()
    db.refresh(confirmation)
    return confirmation


def record_script_approval(db: Session, *, project_id: str, version: int) -> None:
    """上游已保存剧本（如视频工作流）后登记审核通过，绑定明确版本。"""
    project = db.get(Project, project_id)
    if project is None:
        return None
    _write_script_state(db, project, status=STATUS_APPROVED, version=version)
    db.commit()
    return None


def build_pending_hint(db: Session, session_id: str) -> str | None:
    """生成注入 LLM 的待确认事项提示（优先于普通意图识别参与上下文解析）。"""
    pendings = list_pending_all(db, session_id)
    if not pendings:
        return None
    pending = pendings[-1]
    version_part = ""
    if pending.target_version is not None:
        version_part = "（第 " + str(pending.target_version) + " 版）"
    parts = [
        "【当前待确认事项】存在等待用户确认的审核事项，用户的简短回答"
        "（可以 / 不可以 / 通过 / 退回 / 再改改 / 再看看）应理解为对该事项的答复。",
        "- 审核对象：" + (pending.target_type or "对象") + version_part,
        "- 待确认问题：" + (pending.question or "（未记录）"),
    ]
    if len(pendings) > 1:
        parts.append(
            "- 注意：当前共有 " + str(len(pendings)) + " 个待确认事项，"
            "若用户没有明确指代，必须请用户澄清针对哪一个，不要擅自选择。"
        )
    return "\n".join(parts)


def to_card(confirmation: AgentConfirmation) -> dict:
    """前端确认卡片结构：按版本展示，不混用不同版本的审核状态。"""
    return {
        "confirmation_id": confirmation.id,
        "kind": confirmation.kind,
        "confirmation_type": confirmation.confirmation_type,
        "target_type": confirmation.target_type,
        "target_id": confirmation.target_id,
        "target_version": confirmation.target_version,
        "round": confirmation.round,
        "status": confirmation.status,
        "question": confirmation.question,
        "allowed_responses": confirmation.allowed_responses or ALLOWED_RESPONSES,
        "user_response": confirmation.user_response,
        "comment": confirmation.comment,
        "resolution": confirmation.resolution,
        "created_at": confirmation.created_at.isoformat() if confirmation.created_at else None,
        "resolved_at": confirmation.resolved_at.isoformat() if confirmation.resolved_at else None,
    }
