"""音频模块表：voice_profile / audio_segment / audio_review
（音频模块与 H3 VA 工作流改造，2026-11-06）

- voice_profile：角色音色配置，同角色跨分镜跨集复用
- audio_segment：音频时间轴片段（对白/旁白/BGM/SFX/环境声/H3 原生音频）
- audio_review：音频版本审核记录，绑定 asset_id + version
Revision ID: a1b2c3d4e5f7
Revises: c9e2f5a8d1b4
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f7"
down_revision = "c9e2f5a8d1b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_profile",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("character_id", sa.String(32), sa.ForeignKey("character.id"), nullable=False),
        sa.Column("voice_provider", sa.String(64), nullable=False, server_default="comfyui_qwen3_tts"),
        sa.Column("voice_model", sa.String(128), nullable=False, server_default="wf_tts_qwen3_custom_voice"),
        sa.Column("voice_id", sa.String(64), nullable=False),
        sa.Column("voice_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("speed", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("pitch", sa.Float(), nullable=True),
        sa.Column("default_emotion", sa.String(16), nullable=False, server_default="neutral"),
        sa.Column("voice_reference_asset_id", sa.String(32), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_voice_profile_project", "voice_profile", ["project_id"])
    op.create_index("ix_voice_profile_character", "voice_profile", ["character_id", "is_default"])

    op.create_table(
        "audio_segment",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("shot_id", sa.String(32), sa.ForeignKey("shot.id"), nullable=True),
        sa.Column("track_type", sa.String(16), nullable=False),
        sa.Column("segment_type", sa.String(32), nullable=False, server_default=""),
        sa.Column("character_id", sa.String(32), sa.ForeignKey("character.id"), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("voice_profile_id", sa.String(32), sa.ForeignKey("voice_profile.id"), nullable=True),
        sa.Column("emotion", sa.String(16), nullable=True),
        sa.Column("speed", sa.Float(), nullable=True),
        sa.Column("start_time_ms", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("audio_asset_id", sa.String(32), nullable=True),
        sa.Column("video_asset_id", sa.String(32), nullable=True),
        sa.Column("generation_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("review_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("cross_shot", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audio_segment_project", "audio_segment", ["project_id"])
    op.create_index("ix_audio_segment_episode", "audio_segment", ["episode"])
    op.create_index("ix_audio_segment_shot", "audio_segment", ["shot_id"])
    op.create_index("ix_audio_segment_track", "audio_segment", ["track_type"])
    op.create_index("ix_audio_segment_generation", "audio_segment", ["generation_status"])
    op.create_index("ix_audio_segment_review", "audio_segment", ["review_status"])
    op.create_index("ix_audio_segment_asset", "audio_segment", ["audio_asset_id"])
    op.create_index(
        "ix_audio_segment_project_episode_track", "audio_segment",
        ["project_id", "episode", "track_type"],
    )

    op.create_table(
        "audio_review",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("audio_asset_id", sa.String(32), nullable=False),
        sa.Column("asset_version", sa.Integer(), nullable=False),
        sa.Column("segment_id", sa.String(32), sa.ForeignKey("audio_segment.id"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audio_review_project", "audio_review", ["project_id"])
    op.create_index("ix_audio_review_asset", "audio_review", ["audio_asset_id"])


def downgrade() -> None:
    op.drop_index("ix_audio_review_asset", table_name="audio_review")
    op.drop_index("ix_audio_review_project", table_name="audio_review")
    op.drop_table("audio_review")
    op.drop_index("ix_audio_segment_project_episode_track", table_name="audio_segment")
    op.drop_index("ix_audio_segment_asset", table_name="audio_segment")
    op.drop_index("ix_audio_segment_review", table_name="audio_segment")
    op.drop_index("ix_audio_segment_generation", table_name="audio_segment")
    op.drop_index("ix_audio_segment_track", table_name="audio_segment")
    op.drop_index("ix_audio_segment_shot", table_name="audio_segment")
    op.drop_index("ix_audio_segment_episode", table_name="audio_segment")
    op.drop_index("ix_audio_segment_project", table_name="audio_segment")
    op.drop_table("audio_segment")
    op.drop_index("ix_voice_profile_character", table_name="voice_profile")
    op.drop_index("ix_voice_profile_project", table_name="voice_profile")
    op.drop_table("voice_profile")
