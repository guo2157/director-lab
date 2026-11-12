"""storyboard_regen_config 分镜视频重新生成工作台结构化配置表

Revision ID: b7d2e4a9c315
Revises: a1f4c8e2d907
Create Date: 2026-11-12 16:05:00
"""
from alembic import op
import sqlalchemy as sa

revision = "b7d2e4a9c315"
down_revision = "a1f4c8e2d907"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storyboard_regen_config",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("shot_id", sa.String(32), sa.ForeignKey("shot.id"), nullable=False),
        sa.Column("target", sa.String(16), nullable=False, server_default="clip"),
        sa.Column("workflow_id", sa.String(32), nullable=True),
        sa.Column("workflow_version", sa.Integer, nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("references", sa.JSON, nullable=True),
        sa.Column("character_bindings", sa.JSON, nullable=True),
        sa.Column("spatial_layout", sa.JSON, nullable=True),
        sa.Column("action_timeline", sa.JSON, nullable=True),
        sa.Column("prop_bindings", sa.JSON, nullable=True),
        sa.Column("prompt_sections", sa.JSON, nullable=True),
        sa.Column("final_prompt", sa.Text, nullable=True),
        sa.Column("parameters", sa.JSON, nullable=True),
        sa.Column("extra", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint("shot_id", "target", name="uq_regen_config_shot_target"),
    )
    op.create_index("ix_storyboard_regen_config_project_id", "storyboard_regen_config", ["project_id"])
    op.create_index("ix_storyboard_regen_config_shot_id", "storyboard_regen_config", ["shot_id"])
    op.create_index("ix_regen_config_project_target", "storyboard_regen_config", ["project_id", "target"])


def downgrade() -> None:
    op.drop_index("ix_regen_config_project_target", table_name="storyboard_regen_config")
    op.drop_index("ix_storyboard_regen_config_shot_id", table_name="storyboard_regen_config")
    op.drop_index("ix_storyboard_regen_config_project_id", table_name="storyboard_regen_config")
    op.drop_table("storyboard_regen_config")
