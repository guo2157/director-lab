"""parse_version: 将 diff_summary 中的完整性字段提升为独立列

Revision ID: b3c7d1e5f9a2
Revises: a8d1f6b3c9e2
Create Date: 2026-11-23

原来 completeness_status / expected_episodes / parsed_episodes / 
successful_batches / failed_batches / coverage_status 全塞在 JSON 
diff_summary 里，导致查询、过滤与准入判断都要先解析 JSON。
现拆为独立列，读写在模型层透明保持 diff_summary 向后兼容。
"""
from alembic import op
import sqlalchemy as sa


revision = "b3c7d1e5f9a2"
down_revision = "a8d1f6b3c9e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "parse_version",
        sa.Column("completion_status", sa.String(16), nullable=True),
    )
    op.add_column(
        "parse_version",
        sa.Column("expected_episodes", sa.Integer, nullable=True),
    )
    op.add_column(
        "parse_version",
        sa.Column("parsed_episodes", sa.Integer, nullable=True),
    )
    op.add_column(
        "parse_version",
        sa.Column("successful_batches", sa.Integer, nullable=True),
    )
    op.add_column(
        "parse_version",
        sa.Column("failed_batches", sa.Integer, nullable=True),
    )
    op.add_column(
        "parse_version",
        sa.Column("coverage_status", sa.String(16), nullable=True),
    )

    # 从已有 diff_summary JSON 中回填独立列
    op.execute(
        """
        UPDATE parse_version
        SET completion_status = json_extract(diff_summary, '$.completion_status'),
            expected_episodes = json_extract(diff_summary, '$.expected_episodes'),
            parsed_episodes   = json_extract(diff_summary, '$.parsed_episodes'),
            successful_batches = json_extract(diff_summary, '$.successful_batches'),
            failed_batches    = json_extract(diff_summary, '$.failed_batches'),
            coverage_status   = json_extract(diff_summary, '$.coverage_status')
        WHERE diff_summary IS NOT NULL
          AND (
            json_extract(diff_summary, '$.completion_status') IS NOT NULL
            OR json_extract(diff_summary, '$.expected_episodes') IS NOT NULL
            OR json_extract(diff_summary, '$.parsed_episodes') IS NOT NULL
            OR json_extract(diff_summary, '$.successful_batches') IS NOT NULL
            OR json_extract(diff_summary, '$.failed_batches') IS NOT NULL
            OR json_extract(diff_summary, '$.coverage_status') IS NOT NULL
          )
        """
    )


def downgrade() -> None:
    op.drop_column("parse_version", "completion_status")
    op.drop_column("parse_version", "expected_episodes")
    op.drop_column("parse_version", "parsed_episodes")
    op.drop_column("parse_version", "successful_batches")
    op.drop_column("parse_version", "failed_batches")
    op.drop_column("parse_version", "coverage_status")
