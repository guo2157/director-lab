"""Shot 表新增 shot_plan 镜头级契约列
Revision ID: a7c3e9f5b1d8
Revises: e5f8a2c1d7b9
Create Date: 2026-11-24
背景（分镜图生成改造 Phase 1）：
- 分镜规划的导演决策（画内角色/画外声音/可见道具/构图/首帧状态）此前只存在于
  plan_meta 预览，从未落库，下游 compiler/resolver/门禁/注入器各用一套启发式
  推断「谁在画面里」，口径互相矛盾；
- 本迁移为 Shot 新增可空 JSON 列 shot_plan（schema v1），作为镜头级契约落库；
- 仅加列，无数据迁移；历史行保持 NULL，走旧启发式兜底路径，行为不变。
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e9f5b1d8"
down_revision: str | Sequence[str] | None = "e5f8a2c1d7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(shot)"))}
    if "shot_plan" not in cols:
        op.add_column("shot", sa.Column("shot_plan", sa.JSON(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(shot)"))}
    if "shot_plan" in cols:
        op.drop_column("shot", "shot_plan")
