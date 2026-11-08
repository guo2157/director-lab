"""补齐 asset.updated_at 列

Revision ID: f1a2b3c4d5e6
Revises: e5a7b9c1d3f2
Create Date: 2026-11-08

背景：
- ORM `Asset` 定义了 `updated_at`，执行器与资产接口在生成/审核后会写入该列；
- 初始建表与后续迁移均未创建该列，导致资产生成任务报
  `sqlite3.OperationalError: no such column: asset.updated_at`，任务重试耗尽后失败。
- 本迁移补齐该列，允许为空，历史行保持 NULL，后续写入即时更新。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "e5a7b9c1d3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(asset)"))}
    if "updated_at" not in cols:
        op.add_column("asset", sa.Column("updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(asset)"))}
    if "updated_at" in cols:
        op.drop_column("asset", "updated_at")
