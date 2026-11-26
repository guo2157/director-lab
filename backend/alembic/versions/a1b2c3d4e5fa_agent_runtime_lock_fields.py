"""Agent Runtime Phase A A11：原子执行锁字段

Revision ID: a1b2c3d4e5fa
Revises: a1b2c3d4e5f9
Create Date: 2026-11-26

只新增可空列，不改动现有列约束；downgrade 可完整回退。
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5fa"
down_revision = "a1b2c3d4e5f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_plan_step") as batch:
        batch.add_column(sa.Column("lock_owner", sa.String(36), nullable=True))

    with op.batch_alter_table("agent_goal") as batch:
        batch.add_column(sa.Column("locked_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_goal") as batch:
        batch.drop_column("locked_until")

    with op.batch_alter_table("agent_plan_step") as batch:
        batch.drop_column("lock_owner")