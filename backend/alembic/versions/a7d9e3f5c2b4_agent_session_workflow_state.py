"""agent_session 新增 workflow_state 列（视频生成工作流会话级状态）

Revision ID: a7d9e3f5c2b4
Revises: c8f3a5d2e9b1
Create Date: 2026-11-03
"""
from alembic import op
import sqlalchemy as sa

revision = "a7d9e3f5c2b4"
down_revision = "c8f3a5d2e9b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_session") as batch_op:
        batch_op.add_column(
            sa.Column("workflow_state", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_session") as batch_op:
        batch_op.drop_column("workflow_state")
