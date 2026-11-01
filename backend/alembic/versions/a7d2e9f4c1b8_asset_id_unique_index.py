"""asset_id_unique_index
Revision ID: a7d2e9f4c1b8
Revises: b4e8c1a70d29
Create Date: 2026-10-31
修复 asset 表外键不匹配：历史建表语句中 asset.id 缺少主键约束，
导致 asset_ref.asset_id 的外键在 SQLite 下报 foreign key mismatch。
本迁移为 asset.id 创建唯一索引，使外键引用有效（幂等，可重复执行）。
注：生产库已于 2026-10-31 手工创建同名索引 ix_asset_id，本迁移做正式固化。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a7d2e9f4c1b8'
down_revision: str | Sequence[str] | None = 'b4e8c1a70d29'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = 'ix_asset_id'


def _index_exists(inspector, table: str, name: str) -> bool:
    return any(idx['name'] == name for idx in inspector.get_indexes(table))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _index_exists(inspector, 'asset', INDEX_NAME):
        op.create_index(INDEX_NAME, 'asset', ['id'], unique=True)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _index_exists(inspector, 'asset', INDEX_NAME):
        op.drop_index(INDEX_NAME, table_name='asset')
