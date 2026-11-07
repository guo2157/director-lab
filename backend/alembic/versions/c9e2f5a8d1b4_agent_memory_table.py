"""agent_memory 记忆表（记忆系统迭代一：项目记忆/用户偏好/失败经验）
Revision ID: c9e2f5a8d1b4
Revises: b7d1e4c8a2f5
Create Date: 2026-11-05

设计约束（AGENTS.md P0）：
- 记忆只作上下文补充，不得覆盖用户当前明确指令
- 记忆必须可追溯：source 记录来源，source_ref 保留消息/任务引用
- 记忆有生命周期：superseded 表示被替代，保留记录不物理删除
"""
from alembic import op
import sqlalchemy as sa

revision = "c9e2f5a8d1b4"
down_revision = "b7d1e4c8a2f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_memory",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "memory_type",
            sa.String(16), nullable=False, server_default="project",
        ),
        sa.Column("project_id", sa.String(32), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="agent"),
        sa.Column("source_ref", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("superseded_by", sa.String(32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_agent_memory_memory_type", "agent_memory", ["memory_type"])
    op.create_index("ix_agent_memory_project_id", "agent_memory", ["project_id"])
    op.create_index("ix_agent_memory_status", "agent_memory", ["status"])


def downgrade() -> None:
    op.drop_index("ix_agent_memory_status", table_name="agent_memory")
    op.drop_index("ix_agent_memory_project_id", table_name="agent_memory")
    op.drop_index("ix_agent_memory_memory_type", table_name="agent_memory")
    op.drop_table("agent_memory")
