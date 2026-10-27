"""episode_continuity 跨集连续性记录表

Revision ID: f9a3b7c2d1e6
Revises: d8f3a2b6c9e1
Create Date: 2026-10-26 13:41:00
"""
from alembic import op
import sqlalchemy as sa

revision = "f9a3b7c2d1e6"
down_revision = "1283779e3172"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "episode_continuity",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("episode", sa.Integer, nullable=False),
        sa.Column("previous_episode", sa.Integer, nullable=True),
        sa.Column("next_episode", sa.Integer, nullable=True),
        sa.Column("source_composition_id", sa.String(32), nullable=True),
        sa.Column("source_video_path", sa.String(500), nullable=True),
        sa.Column("source_frame_path", sa.String(500), nullable=True),
        sa.Column("source_frame_hash", sa.String(64), nullable=True),
        sa.Column("source_frame_timestamp", sa.Float, nullable=True),
        sa.Column("source_video_duration", sa.Float, nullable=True),
        sa.Column("source_video_fps", sa.Float, nullable=True),
        sa.Column("opening_frame_path", sa.String(500), nullable=True),
        sa.Column("opening_frame_asset_id", sa.String(32), nullable=True),
        sa.Column("continuity_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("continuity_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("continuity_summary", sa.JSON, nullable=True),
        sa.Column("verification_result", sa.JSON, nullable=True),
        sa.Column("error_detail", sa.Text, nullable=True),
        sa.Column("fallback_used", sa.String(1), nullable=False, server_default="0"),
        sa.Column("fallback_reason", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_continuity_project_episode", "episode_continuity", ["project_id", "episode"])
    op.create_index("ix_continuity_project_status", "episode_continuity", ["project_id", "continuity_status"])


def downgrade() -> None:
    op.drop_index("ix_continuity_project_status", table_name="episode_continuity")
    op.drop_index("ix_continuity_project_episode", table_name="episode_continuity")
    op.drop_table("episode_continuity")
