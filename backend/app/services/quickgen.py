"""随手生成（A 方案）：未关联项目的明确出图请求落到全局草稿项目。

设计约束：
- 不建正式项目：全局唯一「随手生成」草稿项目（status=draft，settings.origin=quickgen），
  首次需要时创建、之后复用，避免项目列表被一次性项目撑爆；
- 只处理明确的出图请求：视频/音频仍需要正式项目（依赖剧本、镜头与阶段门禁）；
- 对话即授权：用户在对话里明确要求「生成一张图」，视为授权，直接提交生成任务；
  仍保留审批记录（approved）与审计消息，便于追溯，不绕过审批纪律的数据留痕。
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentSession, Project, ids

QUICKGEN_PROJECT_NAME = "随手生成"
QUICKGEN_ORIGIN = "quickgen"


def get_or_create_quickgen_project(db: Session) -> Project:
    """取全局「随手生成」草稿项目，不存在则创建。"""
    project = db.scalars(
        select(Project)
        .where(Project.name == QUICKGEN_PROJECT_NAME, Project.deleted_at.is_(None))
        .limit(1)
    ).first()
    if project is not None:
        return project
    project = Project(
        id=ids.project_id(),
        name=QUICKGEN_PROJECT_NAME,
        creation_type="novel",
        description="对话中随手生成的图片存放于此（草稿项目，可改名转为正式项目）",
        aspect_ratio="9:16",
        status="draft",
        settings={"origin": QUICKGEN_ORIGIN},
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def submit_quick_image(db: Session, *, session: AgentSession, prompt: str) -> dict:
    """绑定草稿项目 → 建审批记录并自动批准 → 直接提交生成任务。

    复用 request_generate / run_generate 同一条链路（审批记录、任务、审计消息齐全），
    只是把「人工点批准」换成「用户在对话中明确提出生成」这一授权事实。
    """
    from app.services import agent_tools, approvals

    project = get_or_create_quickgen_project(db)
    if session.project_id != project.id:
        session.project_id = project.id
        db.commit()

    created = agent_tools.execute_tool(
        db,
        "request_generate",
        session_id=session.id,
        project_id=project.id,
        target="keyframe",
        quick_prompt=prompt,
    )
    if not created.ok:
        raise RuntimeError(created.error_message or "提交生成申请失败")
    confirmation_id = (created.data or {}).get("confirmation_id")
    if not confirmation_id:
        raise RuntimeError("提交生成申请失败：缺少审批编号")

    approvals.resolve_confirmation(
        db,
        confirmation_id=confirmation_id,
        approve=True,
        comment="用户在对话中明确请求生成图片，视为授权（随手生成）",
    )
    executed = agent_tools.execute_tool(
        db, "run_generate", approved=True, confirmation_id=confirmation_id
    )
    if not executed.ok:
        raise RuntimeError(executed.error_message or "生成任务提交失败")
    return {"project": project, "confirmation_id": confirmation_id, **(executed.data or {})}


def try_quick_image(db: Session, session: AgentSession, text: str) -> tuple[str, dict] | None:
    """识别「随手出图」请求并直接执行；不是明确出图请求时返回 None。

    返回 (助手回复文案, 消息 payload)，供聊天接口直接落消息返回。
    """
    from app.services.generate_intent import extract_image_prompt, resolve_target

    if resolve_target(text) != "keyframe":
        return None
    prompt = extract_image_prompt(text)
    if not prompt:
        return None

    try:
        result = submit_quick_image(db, session=session, prompt=prompt)
    except Exception as exc:  # noqa: BLE001
        return f"随手生成提交失败：{exc}", {
            "ok": False,
            "error_code": "quick_image_failed",
            "quick": True,
        }

    project = result["project"]
    reply = (
        f"已提交随手生成任务（提示词：{prompt}），任务 {result.get('job_id')} 已入队。\n"
        f"图片会保存到草稿项目「{project.name}」的资产库，生成完成后可在该项目查看；"
        "如需把它做成正式作品，告诉我项目名称即可改名转正。"
    )
    return reply, {
        "ok": True,
        "quick": True,
        "job_id": result.get("job_id"),
        "project_id": project.id,
        "project_name": project.name,
        "prompt": prompt,
    }
