"""shot_keyframe_confirmed

Revision ID: a3f8c2d91e07
Revises: 167f0caf4cb4
Create Date: 2026-10-24

静图先行闸口（D-13）：shot 表新增 keyframe_confirmed 字段，
分镜图人工确认后置 True；重绘/上游失效时回退 False。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a3f8c2d91e07'
down_revision: str | Sequence[str] | None = '167f0caf4cb4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('shot', sa.Column('keyframe_confirmed', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('shot', 'keyframe_confirmed')
