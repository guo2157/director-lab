"""agent_session 新增 summary / summary_upto_seq 列（会话长期记忆）

Revision ID: d4e6f8a1c3b9
Revises: a7d9e3f5c2b4
Create Date: 2026-11-03
"""
from alembic import op
import sqlalchemy as sa

revision = "d4e6f8a1c3b9"
down_revision = "a7d9e3f5c2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_session") as batch_op:
        batch_op.add_column(
            sa.Column("summary", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("summary_upto_seq", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_session") as batch_op:
        batch_op.drop_column("summary_upto_seq")
        batch_op.drop_column("summary")
