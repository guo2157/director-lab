"""workflow 新增 model_type 模型类型字段

模型类型用于前端按「文生图 / 图生图 / 参考图生视频 / 首尾帧生视频 / 文生视频 / TTS」
选择具体工作流。workflow_type 保留为粗粒度大类（image / video / tts / bgm / sfx）。

取值与 app/api/v1/workflows.py 的 _CLASSIFICATION_GROUPS 细分键一致，
由 capabilities.normalize() 的推导结果写入，迁移本身不猜能力。

Revision ID: a7c1e9f4b2d8
Revises: 64bc5ce8a27f
Create Date: 2026-11-20
"""
from alembic import op
import sqlalchemy as sa

revision = "a7c1e9f4b2d8"
down_revision = "64bc5ce8a27f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow", sa.Column("model_type", sa.String(32), nullable=True))
    op.create_index(
        "ix_workflow_type_model",
        "workflow",
        ["workflow_type", "model_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_type_model", table_name="workflow")
    op.drop_column("workflow", "model_type")
