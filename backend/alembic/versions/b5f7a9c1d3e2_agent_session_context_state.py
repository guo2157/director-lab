"""agent_session 新增 context_state 列（Context Manager 结构化上下文持久化）

Revision ID: b5f7a9c1d3e2
Revises: d4e6f8a1c3b9
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = "b5f7a9c1d3e2"
down_revision = "d4e6f8a1c3b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_session") as batch_op:
        batch_op.add_column(
            sa.Column("context_state", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_session") as batch_op:
        batch_op.drop_column("context_state")
