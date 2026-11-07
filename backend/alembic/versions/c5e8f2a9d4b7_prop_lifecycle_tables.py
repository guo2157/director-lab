"""prop lifecycle tables

Revision ID: c5e8f2a9d4b7
Revises: b2c3d4e5f6a8
Create Date: 2026-11-06

道具生命周期整改（PROP_LIFECYCLE_PLAN.md P0）：
- 新表 prop_lifecycle_event：道具生命周期事件（只追加不覆盖）
- 新表 prop_shot_state：分镜道具状态快照（事件推导，可重建）
- 新表 prop_episode_snapshot：集末道具状态快照（跨集连续性）
- shot 表加 props 列（JSON，剧本解析输出的本镜道具名数组）
- prop 表加 category / appearance_attrs 列
全部为加表/加列，不改现有列，存量数据零影响。
"""
from alembic import op
import sqlalchemy as sa

revision = "c5e8f2a9d4b7"
down_revision = "b2c3d4e5f6a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- shot.props ---
    op.add_column("shot", sa.Column("props", sa.JSON(), nullable=True))
    # --- prop.category / prop.appearance_attrs ---
    op.add_column("prop", sa.Column("category", sa.String(32), nullable=True))
    op.add_column("prop", sa.Column("appearance_attrs", sa.JSON(), nullable=True))

    # --- prop_lifecycle_event ---
    op.create_table(
        "prop_lifecycle_event",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("prop_id", sa.String(32), sa.ForeignKey("prop.id"), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("shot_id", sa.String(32), nullable=True),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_evidence", sa.Text(), nullable=True),
        sa.Column("holder_before", sa.String(32), nullable=True),
        sa.Column("holder_after", sa.String(32), nullable=True),
        sa.Column("location_before", sa.String(200), nullable=True),
        sa.Column("location_after", sa.String(200), nullable=True),
        sa.Column("physical_before", sa.JSON(), nullable=True),
        sa.Column("physical_after", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("review_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("supersedes_id", sa.String(32), nullable=True),
        sa.Column("analysis_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_ple_project_prop_ep_shot", "prop_lifecycle_event",
        ["project_id", "prop_id", "episode", "shot_id"],
    )
    op.create_index(
        "ix_ple_project_review", "prop_lifecycle_event", ["project_id", "review_status"],
    )

    # --- prop_shot_state ---
    op.create_table(
        "prop_shot_state",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("prop_id", sa.String(32), sa.ForeignKey("prop.id"), nullable=False),
        sa.Column("shot_id", sa.String(32), sa.ForeignKey("shot.id"), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("existence_state", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("interaction_state", sa.String(16), nullable=False, server_default="unassigned"),
        sa.Column("holder_id", sa.String(32), nullable=True),
        sa.Column("position", sa.String(200), nullable=True),
        sa.Column("physical_state", sa.JSON(), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("reference_required", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("appearance_constraint", sa.String(200), nullable=True),
        sa.Column("continuity_from_shot", sa.String(32), nullable=True),
        sa.Column("continuity_event_ids", sa.JSON(), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("project_id", "prop_id", "shot_id", name="uq_pss_project_prop_shot"),
    )
    op.create_index("ix_pss_project_shot", "prop_shot_state", ["project_id", "shot_id"])

    # --- prop_episode_snapshot ---
    op.create_table(
        "prop_episode_snapshot",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("prop_id", sa.String(32), sa.ForeignKey("prop.id"), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("final_state", sa.JSON(), nullable=True),
        sa.Column("analysis_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("project_id", "prop_id", "episode", name="uq_pes_project_prop_episode"),
    )


def downgrade() -> None:
    op.drop_table("prop_episode_snapshot")
    op.drop_index("ix_pss_project_shot", table_name="prop_shot_state")
    op.drop_table("prop_shot_state")
    op.drop_index("ix_ple_project_review", table_name="prop_lifecycle_event")
    op.drop_index("ix_ple_project_prop_ep_shot", table_name="prop_lifecycle_event")
    op.drop_table("prop_lifecycle_event")
    op.drop_column("prop", "appearance_attrs")
    op.drop_column("prop", "category")
    op.drop_column("shot", "props")
