"""job 表新增 priority 列与索引（优先级队列，D-22）

Revision ID: d8f3a2b6c9e1
Revises: c4d9e2f7a1b8
Create Date: 2026-10-25
"""
from alembic import op
import sqlalchemy as sa

revision = "d8f3a2b6c9e1"
down_revision = "c4d9e2f7a1b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_job_status_priority",
        "job",
        ["status", "priority"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_status_priority", table_name="job")
    op.drop_column("job", "priority")
