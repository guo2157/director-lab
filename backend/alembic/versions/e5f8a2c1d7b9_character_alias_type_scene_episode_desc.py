"""character: aliases/character_type；scene_episode: 集级 description

Revision ID: e5f8a2c1d7b9
Revises: b3c7d1e5f9a2
Create Date: 2026-11-23

P1-八 步骤2：角色别名与群体分类落库。
  - character.aliases：名称括号别名数组（如『张三（张三丰）』中的『张三丰』），JSON 存储
  - character.character_type：individual（个体）/ crowd（群体，如『众人』『村民们』）

P1-八 步骤3：场景集级独立描述。
  - scene_episode.description：该集对该场景的具体描述，
    只写本列，不覆盖项目级 scene.description（该列仍为跨集公用的基准描述）。

新增列全部 nullable，不改写存量业务数据，历史行留 NULL 表示未采集。
"""
from alembic import op
import sqlalchemy as sa


revision = "e5f8a2c1d7b9"
down_revision = "b3c7d1e5f9a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "character",
        sa.Column("aliases", sa.JSON(), nullable=True),
    )
    op.add_column(
        "character",
        sa.Column("character_type", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_character_character_type",
        "character",
        ["character_type"],
        unique=False,
    )

    op.add_column(
        "scene_episode",
        sa.Column("description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scene_episode", "description")

    op.drop_index("ix_character_character_type", table_name="character")
    op.drop_column("character", "character_type")
    op.drop_column("character", "aliases")
