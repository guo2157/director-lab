"""音色试听缓存字段（2026-11-07）
- voice_profile：新增 preview_asset_id / preview_text / preview_signature
- 目的：试听命中缓存时不再调用 ComfyUI；参数变化或强制重新生成时覆盖缓存

Revision ID: c3f8a1d5e9b4
Revises: d6f9a3b8e2c4
Create Date: 2026-11-07
"""
from alembic import op
import sqlalchemy as sa

revision = "c3f8a1d5e9b4"
down_revision = "d6f9a3b8e2c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("voice_profile", sa.Column("preview_asset_id", sa.String(32), nullable=True))
    op.add_column("voice_profile", sa.Column("preview_text", sa.String(500), nullable=True))
    op.add_column("voice_profile", sa.Column("preview_signature", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("voice_profile", "preview_signature")
    op.drop_column("voice_profile", "preview_text")
    op.drop_column("voice_profile", "preview_asset_id")
