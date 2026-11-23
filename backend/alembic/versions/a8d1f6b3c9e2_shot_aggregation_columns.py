"""Shot 聚合改造：新增 timeline / cut_reason / aggregation_meta 三列

- timeline: JSON，镜内时间轴（替代 beats 成为唯一事实源）
- cut_reason: String(32)，本镜结束原因（枚举见方案 §3.2）
- aggregation_meta: JSON，预算占用与聚合决策记录

三列均可空，保证旧数据完全可读、零行为变化。

Revision ID: a8d1f6b3c9e2
Revises: a7c1e9f4b2d8
Create Date: 2026-11-23
"""
from alembic import op
import sqlalchemy as sa

revision = "a8d1f6b3c9e2"
down_revision = "a7c1e9f4b2d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shot", sa.Column("timeline", sa.JSON, nullable=True))
    op.add_column("shot", sa.Column("cut_reason", sa.String(32), nullable=True))
    op.add_column("shot", sa.Column("aggregation_meta", sa.JSON, nullable=True))
    # 为 cut_reason 建立索引（聚合报告与前端筛选需要）
    op.create_index(
        "ix_shot_cut_reason",
        "shot",
        ["cut_reason"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_shot_cut_reason", table_name="shot")
    op.drop_column("shot", "aggregation_meta")
    op.drop_column("shot", "cut_reason")
    op.drop_column("shot", "timeline")
