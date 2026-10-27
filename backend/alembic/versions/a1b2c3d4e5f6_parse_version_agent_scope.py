"""parse_version 增加 Agent 口径锁定字段

Revision ID: a1b2c3d4e5f6
Revises: f9a3b7c2d1e6
Create Date: 2026-10-26

script-agent 一体化分析流程：
- total_episodes：Agent 首批解析成功后锁定的总集数口径（后续增量解析不可超出）
- script_hash：口径对应的剧本文本 hash（剧本变化则口径失效）
"""

revision = "a1b2c3d4e5f6"
down_revision = "f9a3b7c2d1e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("parse_version") as batch:
        batch.add_column(sa.Column("total_episodes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("script_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("parse_version") as batch:
        batch.drop_column("script_hash")
        batch.drop_column("total_episodes")


from alembic import op
import sqlalchemy as sa
