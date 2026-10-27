"""agent_session 与 agent_message 导演台会话消息表

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-27 11:00:00

- agent_session：导演台会话（关联项目可空，软删除 status=archived）
- agent_message：会话消息（user/assistant/system/tool_event/approval，payload JSON 负载）
"""
from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_session",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False, server_default="新会话"),
        sa.Column("project_id", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_session_project_id", "agent_session", ["project_id"])
    op.create_index("ix_agent_session_status", "agent_session", ["status"])

    op.create_table(
        "agent_message",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("session_id", sa.String(32), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("attachment_id", sa.String(32), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_message_session_id", "agent_message", ["session_id"])
    op.create_index(
        "ix_agent_message_session_seq", "agent_message", ["session_id", "seq"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_agent_message_session_seq", table_name="agent_message")
    op.drop_index("ix_agent_message_session_id", table_name="agent_message")
    op.drop_table("agent_message")
    op.drop_index("ix_agent_session_status", table_name="agent_session")
    op.drop_index("ix_agent_session_project_id", table_name="agent_session")
    op.drop_table("agent_session")
