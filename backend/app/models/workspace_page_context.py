"""工作台当前页面上下文（导演工作台 V2 Phase 4）。

用途：让后端与 Agent 知道「用户此刻在哪个页面、选中了什么对象」，
供右侧 Agent 面板做指代消解与上下文提示。

设计取舍：
- 单行状态表（固定主键），因为工作台是单用户单窗口场景，
  「当前所在页面」天然只有一份；不按 project/page 建多行，避免历史行堆积。
- 不复用 agent_session.context_state：那份 JSON 是「对话上下文」（focus/operation/
  execution），语义不同；且页面上下文上报不带 session_id，混在一起会造成污染。
- revision 整数单调递增，客户端可带 expected_revision 做乐观并发控制：
  不一致时服务端返回 409 + 当前快照，绝不静默覆盖。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base

# 单行状态固定主键（不参与业务 ID 前缀约定，因为是全局唯一状态而非业务实体）
WORKSPACE_CONTEXT_ID = "wspcx_current"


class WorkspacePageContext(Base):
    """当前页面上下文快照（单行）。"""

    __tablename__ = "workspace_page_context"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=WORKSPACE_CONTEXT_ID)
    # 关联项目（可空：项目列表/设置等页面无项目上下文）
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 页面标识（前端 PAGE_ROUTES 的 key，如 storyboard / assets）
    page: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 集数（可空）
    episode_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 页内 Tab（如 assets 的 character/scene/prop/voice）
    tab: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 选中实体类型与 ID（character/scene/prop/shot/asset/composition/export）
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 页内焦点（edit/review/result 等）
    focus: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 导航理由（Agent 下发的说明，便于事后审计导航是否合理）
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 导航结果码与实际落地路由（失败也要如实记录，不粉饰）
    nav_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    nav_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    route_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 冗余原始载荷：保留未建模字段，避免后续加字段就改表结构
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 乐观并发控制版本号：每次成功写入 +1
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
