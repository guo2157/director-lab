"""多图参考重新生成工作台 V1：任务输入参考图结构化表 + 提交幂等索引。

Revision ID: d1e2f3a4b5c6
Revises: c3d4e5f6a7b8
Create Date: 2026-10-27

变更内容：
1. 新增 job_input_ref 表：把每次任务实际使用的参考图结构化落库，
   记录槽位参数、工作流输入键、资产 ID 与版本、用途、必需性、语义与启用状态，
   使『某张参考图被哪些任务用过』和『任务用了哪一版参考图』可查询。
2. 为 job.request_key 建立条件唯一索引，实现提交级幂等：
   同一幂等键重复提交不会产生重复任务，历史 NULL 值不受影响。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_input_ref",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("job.id"), nullable=False),
        sa.Column("slot_param", sa.String(48), nullable=False),
        sa.Column("input_key", sa.String(120), nullable=True),
        sa.Column("asset_id", sa.String(32), nullable=True),
        sa.Column("asset_version", sa.Integer(), nullable=True),
        sa.Column("asset_type", sa.String(16), nullable=True),
        sa.Column("role", sa.String(24), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("file_path", sa.String(500), nullable=True),
        sa.Column("semantic", sa.String(24), nullable=False, server_default="reference"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_job_input_ref_job", "job_input_ref", ["job_id"])
    op.create_index("ix_job_input_ref_asset", "job_input_ref", ["asset_id"])
    # SQLite 条件唯一索引：仅对非空 request_key 生效，兼容历史 NULL 数据
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_job_request_key "
        "ON job (request_key) WHERE request_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_job_request_key")
    op.drop_index("ix_job_input_ref_asset", table_name="job_input_ref")
    op.drop_index("ix_job_input_ref_job", table_name="job_input_ref")
    op.drop_table("job_input_ref")
