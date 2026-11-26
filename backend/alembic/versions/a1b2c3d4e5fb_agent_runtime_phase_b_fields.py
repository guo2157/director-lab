"""Agent Runtime Phase B B0：恢复标记与 Goal 生命周期字段

Revision ID: a1b2c3d4e5fb
Revises: a1b2c3d4e5fa
Create Date: 2026-11-26

只新增可空列（旧代码在未升级的库上仍可运行；升级后旧代码也不会因新列报错），
全部带 downgrade。不修改任何现有列约束。
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5fb"
down_revision = "a1b2c3d4e5fa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_plan_step") as batch:
        batch.add_column(sa.Column("resume_requested_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("resume_reason", sa.String(32), nullable=True))

    with op.batch_alter_table("agent_goal") as batch:
        batch.add_column(sa.Column("agent_task_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("task_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("lifecycle_state", sa.String(32), nullable=True))
        batch.add_column(sa.Column("last_tick_at", sa.DateTime(), nullable=True))

    op.create_index(
        "ix_agent_plan_step_resume_requested_at",
        "agent_plan_step",
        ["resume_requested_at"],
    )
    op.create_index("ix_agent_goal_agent_task_id", "agent_goal", ["agent_task_id"])
    op.create_index("ix_agent_goal_task_id", "agent_goal", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_goal_task_id", table_name="agent_goal")
    op.drop_index("ix_agent_goal_agent_task_id", table_name="agent_goal")
    op.drop_index("ix_agent_plan_step_resume_requested_at", table_name="agent_plan_step")

    with op.batch_alter_table("agent_goal") as batch:
        batch.drop_column("last_tick_at")
        batch.drop_column("lifecycle_state")
        batch.drop_column("task_id")
        batch.drop_column("agent_task_id")

    with op.batch_alter_table("agent_plan_step") as batch:
        batch.drop_column("resume_reason")
        batch.drop_column("resume_requested_at")