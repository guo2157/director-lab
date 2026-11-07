"""音频模块 CustomVoice 字段增量（V1 Qwen3-TTS CustomVoice 集成，2026-11-06）

- voice_profile：新增 mode / language / default_instruct / enabled / approved
- audio_segment：新增 instruct / sequence
Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f7
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a8"
down_revision = "a1b2c3d4e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("voice_profile", sa.Column("mode", sa.String(32), nullable=False, server_default="custom_voice"))
    op.add_column("voice_profile", sa.Column("language", sa.String(32), nullable=False, server_default="Chinese"))
    op.add_column("voice_profile", sa.Column("default_instruct", sa.String(500), nullable=False, server_default=""))
    op.add_column("voice_profile", sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")))
    op.add_column("voice_profile", sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.add_column("audio_segment", sa.Column("instruct", sa.String(500), nullable=True))
    op.add_column("audio_segment", sa.Column("sequence", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("audio_segment", "sequence")
    op.drop_column("audio_segment", "instruct")
    op.drop_column("voice_profile", "approved")
    op.drop_column("voice_profile", "enabled")
    op.drop_column("voice_profile", "default_instruct")
    op.drop_column("voice_profile", "language")
    op.drop_column("voice_profile", "mode")
