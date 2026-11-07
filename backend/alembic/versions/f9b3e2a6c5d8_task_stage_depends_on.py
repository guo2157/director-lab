"""task_stage 新增 depends_on 依赖门禁字段（对话式任务执行改造 Phase 5）
Revision ID: f9b3e2a6c5d8
Revises: e8b2d5f7a4c1
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = "f9b3e2a6c5d8"
down_revision = "e8b2d5f7a4c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_stage", sa.Column("depends_on", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_stage", "depends_on")
