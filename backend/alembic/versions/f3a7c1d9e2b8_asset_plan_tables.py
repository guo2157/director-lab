"""asset_plan_tables

Revision ID: f3a7c1d9e2b8
Revises: b8d1c4e60a27
Create Date: 2026-10-28

资产定义链路（AssetPlan 第二阶段）：新增 asset_plan 与 asset_plan_dependency 两张表。
- asset_plan：解析阶段一次性定稿名称、占位路径、提示词与状态（pending/generated）。
- asset_plan_dependency：按槽位记录定义间的参考依赖（ref_identity/ref_scene 等）。
测试库由 Base.metadata.create_all 建表，生产库此前缺少迁移脚本，本脚本补齐。
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = 'f3a7c1d9e2b8'
down_revision: str | Sequence[str] | None = 'b8d1c4e60a27'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'asset_plan',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('project_id', sa.String(32), sa.ForeignKey('project.id'), nullable=False, index=True),
        sa.Column('plan_type', sa.String(16), nullable=False),
        sa.Column('asset_key', sa.String(160), nullable=False),
        sa.Column('media_path', sa.String(500), nullable=False),
        sa.Column('thumb_path', sa.String(500), nullable=True),
        sa.Column('prompt', sa.Text(), nullable=True),
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('owner_type', sa.String(16), nullable=True),
        sa.Column('owner_id', sa.String(32), nullable=True, index=True),
        sa.Column('episode', sa.Integer(), nullable=True),
        sa.Column('seq', sa.Integer(), nullable=True),
        sa.Column('asset_id', sa.String(32), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('confirmed', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.UniqueConstraint('project_id', 'asset_key', name='uq_asset_plan_project_key'),
    )
    op.create_index('ix_asset_plan_project_type_status', 'asset_plan',
                    ['project_id', 'plan_type', 'status'])
    op.create_table(
        'asset_plan_dependency',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('plan_id', sa.String(32), sa.ForeignKey('asset_plan.id'), nullable=False, index=True),
        sa.Column('slot', sa.String(32), nullable=False),
        sa.Column('ref_plan_id', sa.String(32), nullable=True),
        sa.Column('ref_key', sa.String(160), nullable=False),
        sa.Column('ref_path', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.UniqueConstraint('plan_id', 'slot', name='uq_asset_plan_dep_plan_slot'),
    )


def downgrade() -> None:
    op.drop_table('asset_plan_dependency')
    op.drop_index('ix_asset_plan_project_type_status', table_name='asset_plan')
    op.drop_table('asset_plan')
