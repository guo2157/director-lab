"""project_assets_review

Revision ID: b8d1c4e60a27
Revises: d1e2f3a4b5c6
Create Date: 2026-10-27

流程改造：资产库出图后必须人工审核通过，才允许进入分镜图生成。
project 表新增 assets_review_status / assets_reviewed_at 两个字段，
取值 pending / approved / rejected，历史项目回填 pending。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b8d1c4e60a27'
down_revision: str | Sequence[str] | None = 'd1e2f3a4b5c6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'project',
        sa.Column('assets_review_status', sa.String(length=16), nullable=False, server_default='pending'),
    )
    op.add_column(
        'project',
        sa.Column('assets_reviewed_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('project', 'assets_reviewed_at')
    op.drop_column('project', 'assets_review_status')
