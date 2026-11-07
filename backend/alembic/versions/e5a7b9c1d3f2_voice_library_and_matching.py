"""角色音色智能匹配：voice_library 表 + voice_profile 扩展字段（2026-11-07）

- 新增 voice_library：系统默认音色库（9 个音色，speaker_id 溯源 workflow meta voice_values）
- voice_profile 新增：voice_library_id / match_score / match_reason / alternatives /
  is_locked / source / voice_profile_json
- 目的：角色音色从固定 speaker 选择升级为系统音色库 + 程序评分 + 全局分配模式
Revision ID: e5a7b9c1d3f2
Revises: c3f8a1d5e9b4
Create Date: 2026-11-07
"""
from alembic import op
import sqlalchemy as sa

revision = "e5a7b9c1d3f2"
down_revision = "c3f8a1d5e9b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_library",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("voice_code", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("speaker_id", sa.String(64), nullable=False, index=True),
        sa.Column("display_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("gender_presentation", sa.String(16), nullable=False),
        sa.Column("perceived_age", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("pitch", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("timbre", sa.JSON(), nullable=True),
        sa.Column("personality_tags", sa.JSON(), nullable=True),
        sa.Column("speaking_styles", sa.JSON(), nullable=True),
        sa.Column("language", sa.String(64), nullable=False, server_default="中文"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_workflow", sa.String(128), nullable=True),
        sa.Column("preview_asset_id", sa.String(32), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_voice_library_gender_enabled",
        "voice_library",
        ["gender_presentation", "enabled"],
    )
    op.add_column("voice_profile", sa.Column("voice_library_id", sa.String(32), nullable=True))
    op.create_index("ix_voice_profile_voice_library_id", "voice_profile", ["voice_library_id"])
    op.add_column("voice_profile", sa.Column("match_score", sa.Float(), nullable=True))
    op.add_column("voice_profile", sa.Column("match_reason", sa.Text(), nullable=True))
    op.add_column("voice_profile", sa.Column("alternatives", sa.JSON(), nullable=True))
    op.add_column("voice_profile", sa.Column("is_locked", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("voice_profile", sa.Column("source", sa.String(16), nullable=False, server_default="manual"))
    op.add_column("voice_profile", sa.Column("voice_profile_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("voice_profile", "voice_profile_json")
    op.drop_column("voice_profile", "source")
    op.drop_column("voice_profile", "is_locked")
    op.drop_column("voice_profile", "alternatives")
    op.drop_column("voice_profile", "match_reason")
    op.drop_column("voice_profile", "match_score")
    op.drop_index("ix_voice_profile_voice_library_id", table_name="voice_profile")
    op.drop_column("voice_profile", "voice_library_id")
    op.drop_index("ix_voice_library_gender_enabled", table_name="voice_library")
    op.drop_table("voice_library")
