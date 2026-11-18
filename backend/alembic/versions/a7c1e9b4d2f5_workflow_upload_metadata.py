"""workflow 增加上传溯源字段（P1）

Revision ID: a7c1e9b4d2f5
Revises: d2a8b5f14c37
Create Date: 2026-11-18

背景（docs/plan/workflow-dynamic-audit.md D1/D4）：
- 上传工作流时用户填写的别名、上传的原始文件名、定义指纹无处保存，
  导致「按别名搜索」「同名不同版本溯源」无法实现；
- 三列均可空且不带默认值，历史 10 条内置工作流保持 NULL，无需回填即可运行。

不含本迁移的内容：workflow_version 不可变版本表（P2），
任务快照表（P5），均按阶段单独迁移。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c1e9b4d2f5"
down_revision: str | Sequence[str] | None = "d2a8b5f14c37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    conn = op.get_bind()
    return {row[1] for row in conn.execute(sa.text("PRAGMA table_info(workflow)"))}


def upgrade() -> None:
    cols = _columns()
    if "alias" not in cols:
        op.add_column("workflow", sa.Column("alias", sa.String(length=200), nullable=True))
    if "original_filename" not in cols:
        op.add_column("workflow", sa.Column("original_filename", sa.String(length=300), nullable=True))
    if "file_hash" not in cols:
        op.add_column("workflow", sa.Column("file_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    cols = _columns()
    if "file_hash" in cols:
        op.drop_column("workflow", "file_hash")
    if "original_filename" in cols:
        op.drop_column("workflow", "original_filename")
    if "alias" in cols:
        op.drop_column("workflow", "alias")
