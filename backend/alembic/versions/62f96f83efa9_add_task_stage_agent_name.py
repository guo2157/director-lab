"""add_task_stage_agent_name

Revision ID: 62f96f83efa9
Revises: f1a2b3c4d5e6
Create Date: 2026-11-09 17:40:03.027785
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '62f96f83efa9'
down_revision: str | Sequence[str] | None = 'f1a2b3c4d5e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('task_stage', schema=None) as batch_op:
        batch_op.add_column(sa.Column('agent_name', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('task_stage', schema=None) as batch_op:
        batch_op.drop_column('agent_name')
