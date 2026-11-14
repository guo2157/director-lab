"""workspace_event outbox 表（导演工作台 V2 Phase 3）
Revision ID: c1f7a4e93b26
Revises: b7d2e4a9c315
Create Date: 2026-11-13

仅新增 workspace_event 单表，不改动任何既有表结构；
回滚策略：downgrade 仅 drop workspace_event，业务数据表不受影响。
"""
from alembic import op
import sqlalchemy as sa

revision = "c1f7a4e93b26"
down_revision = "b7d2e4a9c315"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_event",
        sa.Column("event_seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=True),
        sa.Column("project_id", sa.String(length=32), nullable=True),
        sa.Column("route_name", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("event_seq"),
    )
    op.create_index("ix_workspace_event_event_type", "workspace_event", ["event_type"])
    op.create_index("ix_workspace_event_session_id", "workspace_event", ["session_id"])
    op.create_index("ix_workspace_event_project_id", "workspace_event", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_workspace_event_project_id", table_name="workspace_event")
    op.drop_index("ix_workspace_event_session_id", table_name="workspace_event")
    op.drop_index("ix_workspace_event_event_type", table_name="workspace_event")
    op.drop_table("workspace_event")
