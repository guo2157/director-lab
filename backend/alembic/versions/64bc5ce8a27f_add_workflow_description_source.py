"""add_workflow_description_source

Revision ID: 64bc5ce8a27f
Revises: a1d4c7e2f9b3
Create Date: 2026-11-19 11:30:26.846133
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '64bc5ce8a27f'
down_revision: str | Sequence[str] | None = 'a1d4c7e2f9b3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('workflow', sa.Column('description_source', sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workflow') as batch_op:
        batch_op.drop_column('description_source')
