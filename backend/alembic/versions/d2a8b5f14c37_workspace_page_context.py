"""workspace_page_context 当前页面上下文表（导演工作台 V2 Phase 4）
Revision ID: d2a8b5f14c37
Revises: c1f7a4e93b26
Create Date: 2026-11-13

仅新增 workspace_page_context 单表，不改动任何既有表结构。
回滚策略：downgrade 仅 drop 该表，业务数据与工作台事件流不受影响。
"""
from alembic import op
import sqlalchemy as sa

revision = "d2a8b5f14c37"
down_revision = "c1f7a4e93b26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_page_context",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=True),
        sa.Column("page", sa.String(length=64), nullable=True),
        sa.Column("episode_no", sa.Integer(), nullable=True),
        sa.Column("tab", sa.String(length=64), nullable=True),
        sa.Column("entity_type", sa.String(length=32), nullable=True),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("focus", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("nav_result", sa.String(length=32), nullable=True),
        sa.Column("nav_ok", sa.Boolean(), nullable=True),
        sa.Column("route_name", sa.String(length=64), nullable=True),
        sa.Column("full_path", sa.String(length=500), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workspace_page_context_project_id", "workspace_page_context", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_workspace_page_context_project_id", table_name="workspace_page_context")
    op.drop_table("workspace_page_context")
