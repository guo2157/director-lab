"""asset_quality_fields

Revision ID: 1283779e3172
Revises: e2c5d8a4f319
Create Date: 2026-10-26 10:37:21.842896
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '1283779e3172'
down_revision: str | Sequence[str] | None = 'e2c5d8a4f319'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('asset', sa.Column('quality_status', sa.String(16), nullable=True, server_default='pending'))
    op.add_column('asset', sa.Column('quality_issues', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('asset', 'quality_issues')
    op.drop_column('asset', 'quality_status')
