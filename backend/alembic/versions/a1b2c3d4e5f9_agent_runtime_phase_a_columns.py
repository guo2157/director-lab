"""Agent Runtime Phase A：补充步骤/目标/计划列

Revision ID: a1b2c3d4e5f9
Revises: g1a2b3c4d5e7
Create Date: 2026-11-26
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f9"
down_revision = "g1a2b3c4d5e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_plan_step") as batch:
        batch.add_column(sa.Column("confirmation_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("job_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("step_key", sa.String(64), nullable=True))
        batch.create_index("ix_agent_plan_step_confirmation_id", ["confirmation_id"])
        batch.create_index("ix_agent_plan_step_job_id", ["job_id"])

    with op.batch_alter_table("agent_goal") as batch:
        batch.add_column(sa.Column("fingerprint", sa.String(64), nullable=True))
        batch.create_index("ix_agent_goal_fingerprint", ["fingerprint"])

    with op.batch_alter_table("agent_plan") as batch:
        batch.add_column(sa.Column("parent_plan_id", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_plan") as batch:
        batch.drop_column("parent_plan_id")

    with op.batch_alter_table("agent_goal") as batch:
        batch.drop_index("ix_agent_goal_fingerprint")
        batch.drop_column("fingerprint")

    with op.batch_alter_table("agent_plan_step") as batch:
        batch.drop_index("ix_agent_plan_step_job_id")
        batch.drop_index("ix_agent_plan_step_confirmation_id")
        batch.drop_column("step_key")
        batch.drop_column("job_id")
        batch.drop_column("confirmation_id")
