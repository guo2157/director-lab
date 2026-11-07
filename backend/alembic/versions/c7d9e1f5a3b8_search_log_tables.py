"""search_run / search_result 搜索日志表（联网搜索整改 Phase 2）

Revision ID: c7d9e1f5a3b8
Revises: b5f7a9c1d3e2
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = "c7d9e1f5a3b8"
down_revision = "b5f7a9c1d3e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "search_run",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("session_id", sa.String(32), nullable=True, index=True),
        sa.Column("project_id", sa.String(32), nullable=True, index=True),
        sa.Column("user_query", sa.Text(), nullable=False, server_default=""),
        sa.Column("normalized_query", sa.Text(), nullable=False, server_default=""),
        sa.Column("search_provider", sa.String(32), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending", index=True),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("valid_result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "search_result",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "search_run_id",
            sa.String(32),
            sa.ForeignKey("search_run.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("domain", sa.String(200), nullable=False, server_default=""),
        sa.Column("snippet", sa.Text(), nullable=False, server_default=""),
        sa.Column("published_at", sa.String(32), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(), nullable=True),
        sa.Column("fetch_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("content_hash", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("search_result")
    op.drop_table("search_run")
