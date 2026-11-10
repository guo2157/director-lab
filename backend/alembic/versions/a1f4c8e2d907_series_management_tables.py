"""series management tables

Revision ID: a1f4c8e2d907
Revises: 62f96f83efa9
Create Date: 2026-11-10

系列化创作与续集管理 V1（Phase 1）：仅新增表，不改动任何既有表结构与数据。
- series / series_episodes / series_bible_versions / series_script_versions
- series_assets / episode_asset_refs
- story_events / continuity_snapshots / continuity_change_proposals
- episode_context_manifests / plot_threads

兼容性：全部新表，旧代码与旧接口不受影响；downgrade 完整删除。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f4c8e2d907'
down_revision: str | Sequence[str] | None = '62f96f83efa9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. series
    op.create_table(
        'series',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('cover_image', sa.String(length=500), nullable=True),
        sa.Column('genre', sa.String(length=64), nullable=True),
        sa.Column('style', sa.String(length=200), nullable=True),
        sa.Column('target_audience', sa.String(length=64), nullable=True),
        sa.Column('language', sa.String(length=16), nullable=False, server_default='zh-CN'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='active'),
        sa.Column('mainline_id', sa.String(length=32), nullable=False, server_default='main'),
        sa.Column('latest_confirmed_episode_id', sa.String(length=32), nullable=True),
        sa.Column('latest_completed_episode_id', sa.String(length=32), nullable=True),
        sa.Column('state_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_series_status', 'series', ['status'])

    # 2. series_episodes
    op.create_table(
        'series_episodes',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('series_id', sa.String(length=32), sa.ForeignKey('series.id'), nullable=False),
        sa.Column('project_id', sa.String(length=32), sa.ForeignKey('project.id'), nullable=True),
        sa.Column('project_episode_no', sa.Integer(), nullable=True),
        sa.Column('episode_number', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=True),
        sa.Column('synopsis', sa.Text(), nullable=True),
        sa.Column('script_status', sa.String(length=24), nullable=False, server_default='draft'),
        sa.Column(
            'production_status', sa.String(length=24), nullable=False, server_default='not_started'
        ),
        sa.Column('script_current_version_id', sa.String(length=32), nullable=True),
        sa.Column('confirmed_script_version_id', sa.String(length=32), nullable=True),
        sa.Column('context_manifest_id', sa.String(length=32), nullable=True),
        sa.Column(
            'continuity_status', sa.String(length=24), nullable=False, server_default='not_checked'
        ),
        sa.Column('mainline_id', sa.String(length=32), nullable=False, server_default='main'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            'series_id', 'episode_number', name='uq_series_episode_number'
        ),
    )
    op.create_index(
        'ix_series_episodes_series', 'series_episodes', ['series_id', 'episode_number']
    )
    op.create_index(
        'ix_series_episodes_status',
        'series_episodes',
        ['series_id', 'script_status', 'production_status'],
    )

    # 3. series_bible_versions
    op.create_table(
        'series_bible_versions',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('series_id', sa.String(length=32), sa.ForeignKey('series.id'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('content', sa.JSON(), nullable=True),
        sa.Column('change_summary', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='0'),
        sa.UniqueConstraint(
            'series_id', 'version', name='uq_series_bible_series_version'
        ),
    )
    op.create_index(
        'ix_series_bible_series_active', 'series_bible_versions', ['series_id', 'is_active']
    )

    # 4. series_script_versions
    op.create_table(
        'series_script_versions',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column(
            'episode_id',
            sa.String(length=32),
            sa.ForeignKey('series_episodes.id'),
            nullable=False,
        ),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('synopsis', sa.Text(), nullable=True),
        sa.Column('source_context_manifest_id', sa.String(length=32), nullable=True),
        sa.Column('prompt_template_version', sa.String(length=64), nullable=True),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('validation_result', sa.JSON(), nullable=True),
        sa.Column('continuity_result', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(length=24), nullable=False, server_default='draft'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.UniqueConstraint(
            'episode_id', 'version', name='uq_series_script_episode_version'
        ),
    )
    op.create_index(
        'ix_series_script_episode_status',
        'series_script_versions',
        ['episode_id', 'status'],
    )

    # 5. series_assets
    op.create_table(
        'series_assets',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('series_id', sa.String(length=32), sa.ForeignKey('series.id'), nullable=False),
        sa.Column('asset_id', sa.String(length=32), sa.ForeignKey('asset.id'), nullable=False),
        sa.Column('first_episode_id', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('series_id', 'asset_id', name='uq_series_asset_series_asset'),
    )
    op.create_index('ix_series_assets_series', 'series_assets', ['series_id'])
    op.create_index('ix_series_assets_asset', 'series_assets', ['asset_id'])

    # 6. episode_asset_refs
    op.create_table(
        'episode_asset_refs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column(
            'episode_id',
            sa.String(length=32),
            sa.ForeignKey('series_episodes.id'),
            nullable=False,
        ),
        sa.Column('asset_id', sa.String(length=32), sa.ForeignKey('asset.id'), nullable=False),
        sa.Column('asset_version', sa.Integer(), nullable=False),
        sa.Column('usage_type', sa.String(length=24), nullable=False),
        sa.Column('is_required', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('source', sa.String(length=24), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            'episode_id',
            'asset_id',
            'asset_version',
            'usage_type',
            name='uq_episode_asset_ref_unique',
        ),
    )
    op.create_index('ix_episode_asset_refs_episode', 'episode_asset_refs', ['episode_id'])
    op.create_index('ix_episode_asset_refs_asset', 'episode_asset_refs', ['asset_id'])

    # 7. story_events
    op.create_table(
        'story_events',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('series_id', sa.String(length=32), sa.ForeignKey('series.id'), nullable=False),
        sa.Column(
            'episode_id',
            sa.String(length=32),
            sa.ForeignKey('series_episodes.id'),
            nullable=False,
        ),
        sa.Column('script_version_id', sa.String(length=32), nullable=False),
        sa.Column('event_key', sa.String(length=64), nullable=False),
        sa.Column('event_type', sa.String(length=40), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('involved_asset_ids', sa.JSON(), nullable=True),
        sa.Column('location_asset_id', sa.String(length=32), nullable=True),
        sa.Column('source_scene_id', sa.String(length=32), nullable=True),
        sa.Column('source_text', sa.Text(), nullable=True),
        sa.Column('fact_status', sa.String(length=16), nullable=False, server_default='proposed'),
        sa.Column('importance', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('occurred_order', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.UniqueConstraint(
            'episode_id',
            'script_version_id',
            'event_key',
            name='uq_story_event_episode_version_key',
        ),
    )
    op.create_index(
        'ix_story_events_series_episode', 'story_events', ['series_id', 'episode_id']
    )
    op.create_index(
        'ix_story_events_series_fact', 'story_events', ['series_id', 'fact_status']
    )

    # 8. continuity_snapshots
    op.create_table(
        'continuity_snapshots',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('series_id', sa.String(length=32), sa.ForeignKey('series.id'), nullable=False),
        sa.Column('mainline_id', sa.String(length=32), nullable=False, server_default='main'),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('based_on_episode_id', sa.String(length=32), nullable=True),
        sa.Column('based_on_script_version_id', sa.String(length=32), nullable=True),
        sa.Column('state_json', sa.JSON(), nullable=True),
        sa.Column('state_hash', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.UniqueConstraint(
            'series_id', 'version', name='uq_continuity_snapshot_series_version'
        ),
    )
    op.create_index(
        'ix_continuity_snapshots_series', 'continuity_snapshots', ['series_id', 'version']
    )

    # 9. continuity_change_proposals
    op.create_table(
        'continuity_change_proposals',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('series_id', sa.String(length=32), sa.ForeignKey('series.id'), nullable=False),
        sa.Column(
            'episode_id',
            sa.String(length=32),
            sa.ForeignKey('series_episodes.id'),
            nullable=False,
        ),
        sa.Column('script_version_id', sa.String(length=32), nullable=False),
        sa.Column('base_state_version', sa.Integer(), nullable=False),
        sa.Column('proposal_json', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('idempotency_key', sa.String(length=80), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('committed_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('idempotency_key', name='uq_continuity_proposal_idempotency'),
    )
    op.create_index(
        'ix_continuity_proposals_episode',
        'continuity_change_proposals',
        ['episode_id', 'status'],
    )

    # 10. episode_context_manifests
    op.create_table(
        'episode_context_manifests',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('series_id', sa.String(length=32), sa.ForeignKey('series.id'), nullable=False),
        sa.Column(
            'episode_id',
            sa.String(length=32),
            sa.ForeignKey('series_episodes.id'),
            nullable=False,
        ),
        sa.Column('context_version', sa.Integer(), nullable=False),
        sa.Column('manifest_json', sa.JSON(), nullable=True),
        sa.Column('context_hash', sa.String(length=64), nullable=True),
        sa.Column('prompt_template_version', sa.String(length=64), nullable=True),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            'episode_id', 'context_version', name='uq_context_manifest_episode_version'
        ),
    )
    op.create_index(
        'ix_context_manifests_series_episode',
        'episode_context_manifests',
        ['series_id', 'episode_id'],
    )

    # 11. plot_threads
    op.create_table(
        'plot_threads',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('series_id', sa.String(length=32), sa.ForeignKey('series.id'), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('introduced_episode_id', sa.String(length=32), nullable=True),
        sa.Column('last_updated_episode_id', sa.String(length=32), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='open'),
        sa.Column('related_asset_ids', sa.JSON(), nullable=True),
        sa.Column('related_event_ids', sa.JSON(), nullable=True),
        sa.Column('resolution_condition', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_plot_threads_series_status', 'plot_threads', ['series_id', 'status'])


def downgrade() -> None:
    op.drop_index('ix_plot_threads_series_status', table_name='plot_threads')
    op.drop_table('plot_threads')

    op.drop_index('ix_context_manifests_series_episode', table_name='episode_context_manifests')
    op.drop_table('episode_context_manifests')

    op.drop_index('ix_continuity_proposals_episode', table_name='continuity_change_proposals')
    op.drop_table('continuity_change_proposals')

    op.drop_index('ix_continuity_snapshots_series', table_name='continuity_snapshots')
    op.drop_table('continuity_snapshots')

    op.drop_index('ix_story_events_series_fact', table_name='story_events')
    op.drop_index('ix_story_events_series_episode', table_name='story_events')
    op.drop_table('story_events')

    op.drop_index('ix_episode_asset_refs_asset', table_name='episode_asset_refs')
    op.drop_index('ix_episode_asset_refs_episode', table_name='episode_asset_refs')
    op.drop_table('episode_asset_refs')

    op.drop_index('ix_series_assets_asset', table_name='series_assets')
    op.drop_index('ix_series_assets_series', table_name='series_assets')
    op.drop_table('series_assets')

    op.drop_index(
        'ix_series_script_episode_status', table_name='series_script_versions'
    )
    op.drop_table('series_script_versions')

    op.drop_index('ix_series_bible_series_active', table_name='series_bible_versions')
    op.drop_table('series_bible_versions')

    op.drop_index('ix_series_episodes_status', table_name='series_episodes')
    op.drop_index('ix_series_episodes_series', table_name='series_episodes')
    op.drop_table('series_episodes')

    op.drop_index('ix_series_status', table_name='series')
    op.drop_table('series')
