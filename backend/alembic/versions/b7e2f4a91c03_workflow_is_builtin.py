"""workflow_is_builtin
Revision ID: b7e2f4a91c03
Revises: a3f8c2d91e07
Create Date: 2026-10-24
工作流内置标识：workflow 表新增 is_builtin 字段，
系统预置工作流（data/workflows 目录种子导入）标记为内置，不可删除，匹配时优先执行。
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = 'b7e2f4a91c03'
down_revision: str | Sequence[str] | None = 'a3f8c2d91e07'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('workflow', sa.Column('is_builtin', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('workflow', 'is_builtin')
