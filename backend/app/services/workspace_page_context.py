"""工作台页面上下文服务（导演工作台 V2 Phase 4）。

职责边界：
- 只维护「用户此刻在哪个页面、选中了什么」这一份单行状态，不做业务决策
- 冲突检测用乐观并发：客户端带 expected_revision，服务端比对后决定写入或拒绝
- 拒绝时返回服务端当前快照，调用方可选择刷新或强制覆盖（强制需显式传 force）

禁止事项：
- 禁止静默覆盖：expected_revision 不一致时必须报错，不能悄悄改写
- 禁止伪造 nav_ok/nav_result：值只来自前端真实导航结果
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.services.errors import biz_error
from app.models import WORKSPACE_CONTEXT_ID, WorkspacePageContext

# 允许建模的实体类型（与前端 NavigateRequest.entityType 对齐）
_ENTITY_TYPES = frozenset(
    {"character", "scene", "prop", "shot", "asset", "composition", "export"}
)

# 已建模字段：其余键归入 extra，避免加字段就要改表
_MODELED_FIELDS = (
    "project_id",
    "page",
    "episode_no",
    "tab",
    "entity_type",
    "entity_id",
    "focus",
    "reason",
    "nav_result",
    "nav_ok",
    "route_name",
    "full_path",
)


def get_context(db: Session) -> WorkspacePageContext | None:
    """读取当前页面上下文（不存在返回 None）。"""
    return db.get(WorkspacePageContext, WORKSPACE_CONTEXT_ID)


def serialize_context(ctx: WorkspacePageContext | None) -> dict:
    """对外序列化：字段名与前端上报体保持一致（snake_case）。"""
    if ctx is None:
        return {"revision": 0, "context": None}
    data: dict = {k: getattr(ctx, k) for k in _MODELED_FIELDS}
    data["extra"] = ctx.extra or None
    data["created_at"] = ctx.created_at.isoformat() if ctx.created_at else None
    data["updated_at"] = ctx.updated_at.isoformat() if ctx.updated_at else None
    return {"revision": ctx.revision, "context": data}


def report_context(
    db: Session,
    payload: dict,
    *,
    expected_revision: int | None = None,
    force: bool = False,
) -> dict:
    """上报当前页面上下文（单行 upsert + revision 递增）。

    并发语义：
    - expected_revision 为 None：视为「首次上报或无需校验」，直接写入并递增
    - expected_revision 与当前 revision 不一致：抛 409，返回当前快照供前端刷新
    - force=True：显式强制覆盖（调用方明确要求时使用，不默认开启）

    返回 {"revision": 新版本号, "context": 快照}
    """
    entity_type = payload.get("entity_type")
    if entity_type is not None and entity_type not in _ENTITY_TYPES:
        raise biz_error(
            code="invalid_entity_type",
            http=422,
            message=f"不支持的实体类型：{entity_type}",
        )

    ctx = get_context(db)
    now = datetime.now()

    if ctx is None:
        ctx = WorkspacePageContext(
            id=WORKSPACE_CONTEXT_ID,
            revision=0,
            created_at=now,
            updated_at=now,
        )
        db.add(ctx)
        db.flush()

    # 冲突检测：只在客户端明确携带 expected_revision 且未要求强制覆盖时生效
    if (
        expected_revision is not None
        and not force
        and int(expected_revision) != int(ctx.revision)
    ):
        raise biz_error(
            code="context_revision_conflict",
            http=409,
            message=(
                f"页面上下文已被更新（服务端 revision={ctx.revision}，"
                f"客户端 expected_revision={expected_revision}），请刷新后重试"
            ),
            expected_revision=int(expected_revision),
            current_revision=int(ctx.revision),
            current=serialize_context(ctx)["context"],
        )

    # 已建模字段直接赋值；未建模键进 extra（保留原始，不丢信息）
    extra: dict = dict(ctx.extra or {})
    for key, value in (payload or {}).items():
        if key in _MODELED_FIELDS:
            setattr(ctx, key, value)
        elif key == "extra" and isinstance(value, dict):
            extra.update(value)
        else:
            extra[key] = value

    if extra:
        ctx.extra = extra
    ctx.revision = int(ctx.revision) + 1
    ctx.updated_at = now
    db.flush()
    return serialize_context(ctx)
