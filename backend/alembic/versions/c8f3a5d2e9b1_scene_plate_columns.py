"""scene_plate_columns
Revision ID: c8f3a5d2e9b1
Revises: a7d2e9f4c1b8
Create Date: 2026-10-31
场景板 LLM 编译器（P1）：scene 表新增 plate（JSON 全量）与 plate_text（四行文本）。
- plate：LLM 编译器产出的空镜环境规格（location_id/anchors/removed 审计账本等）
- plate_text：四行文本，供 asset_scene 模板 {identity} 使用
不覆盖 description：分镜层仍需要 beat（谁做了什么）。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c8f3a5d2e9b1'
down_revision: str | Sequence[str] | None = 'a7d2e9f4c1b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c['name'] for c in inspector.get_columns('scene')}
    if 'plate' not in cols:
        op.add_column('scene', sa.Column('plate', sa.Text(), nullable=True))
    if 'plate_text' not in cols:
        op.add_column('scene', sa.Column('plate_text', sa.Text(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c['name'] for c in inspector.get_columns('scene')}
    if 'plate_text' in cols:
        op.drop_column('scene', 'plate_text')
    if 'plate' in cols:
        op.drop_column('scene', 'plate')
