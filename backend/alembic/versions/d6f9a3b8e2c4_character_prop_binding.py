"""character prop binding table

Revision ID: d6f9a3b8e2c4
Revises: c5e8f2a9d4b7
Create Date: 2026-11-06

角色-道具绑定关系表（PROP_BINDING_PLAN.md P0）：
- 绑定是关系数据，组合参考图是关系的附属视觉参考，不新增基础资产一级分类
- 有效期以生命周期事件为边界（valid_from_event/valid_until_event）
"""
from alembic import op
import sqlalchemy as sa

revision = "d6f9a3b8e2c4"
down_revision = "c5e8f2a9d4b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "character_prop_binding",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("character_id", sa.String(32), sa.ForeignKey("character.id"), nullable=False),
        sa.Column("prop_id", sa.String(32), sa.ForeignKey("prop.id"), nullable=False),
        sa.Column("relation_type", sa.String(16), nullable=False, server_default="carried"),
        sa.Column("anchor_asset_id", sa.String(32), nullable=True),
        sa.Column("valid_from_event", sa.String(32), nullable=True),
        sa.Column("valid_until_event", sa.String(32), nullable=True),
        sa.Column("review_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("source", sa.String(16), nullable=False, server_default="llm"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_cpb_char_prop", "character_prop_binding", ["character_id", "prop_id"],
    )
    op.create_index(
        "ix_cpb_project_review", "character_prop_binding", ["project_id", "review_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_cpb_project_review", table_name="character_prop_binding")
    op.drop_index("ix_cpb_char_prop", table_name="character_prop_binding")
    op.drop_table("character_prop_binding")
