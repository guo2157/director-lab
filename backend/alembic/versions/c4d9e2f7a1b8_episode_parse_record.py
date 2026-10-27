"""episode_parse_record
Revision ID: c4d9e2f7a1b8
Revises: b7e2f4a91c03
Create Date: 2026-10-24
迭代一（评估口径锁定 + 集级解析记录）：
- script_estimate 新增 total_episodes/locked_at（确认时锁定总集数快照）
- character/scene/prop 新增 normalized_name（归一化去重匹配）
- 新建 episode_parse_record 表（集 × 资产类型双粒度解析状态）
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c4d9e2f7a1b8'
down_revision: str | Sequence[str] | None = 'b7e2f4a91c03'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. script_estimate：确认时锁定的总集数快照
    op.add_column('script_estimate', sa.Column('total_episodes', sa.Integer(), nullable=True))
    op.add_column('script_estimate', sa.Column('locked_at', sa.DateTime(), nullable=True))

    # 2. 资产表归一化名（去重匹配用）
    op.add_column('character', sa.Column('normalized_name', sa.String(length=100), nullable=True))
    op.add_column('scene', sa.Column('normalized_name', sa.String(length=220), nullable=True))
    op.add_column('prop', sa.Column('normalized_name', sa.String(length=100), nullable=True))
    op.create_index('ix_character_normalized_name', 'character', ['normalized_name'])
    op.create_index('ix_scene_normalized_name', 'scene', ['normalized_name'])
    op.create_index('ix_prop_normalized_name', 'prop', ['normalized_name'])

    # 3. 集级解析记录表
    op.create_table(
        'episode_parse_record',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('project_id', sa.String(length=32), sa.ForeignKey('project.id'), nullable=False),
        sa.Column('episode_no', sa.Integer(), nullable=False),
        sa.Column('asset_type', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error_msg', sa.Text(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('asset_count', sa.Integer(), nullable=True),
        sa.Column('parse_version_id', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('project_id', 'episode_no', 'asset_type', name='uq_epr_project_episode_type'),
    )
    op.create_index('ix_episode_parse_record_project_id', 'episode_parse_record', ['project_id'])


def downgrade() -> None:
    op.drop_index('ix_episode_parse_record_project_id', table_name='episode_parse_record')
    op.drop_table('episode_parse_record')
    op.drop_index('ix_prop_normalized_name', table_name='prop')
    op.drop_index('ix_scene_normalized_name', table_name='scene')
    op.drop_index('ix_character_normalized_name', table_name='character')
    op.drop_column('prop', 'normalized_name')
    op.drop_column('scene', 'normalized_name')
    op.drop_column('character', 'normalized_name')
    op.drop_column('script_estimate', 'locked_at')
    op.drop_column('script_estimate', 'total_episodes')
