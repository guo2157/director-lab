"""修复 agent_execution_log 表缺失 updated_at 列（M1 迁移遗漏）。

Revision ID: g1a2b3c4d5e7
Revises: g1a2b3c4d5e6
Create Date: 2026-11-25
"""
from alembic import op
import sqlalchemy as sa

revision = "g1a2b3c4d5e7"
down_revision = "g1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_execution_log",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_execution_log", "updated_at")
