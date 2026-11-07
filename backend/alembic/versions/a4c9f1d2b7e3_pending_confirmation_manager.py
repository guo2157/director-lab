"""agent_confirmation 扩展为统一确认事项表（待确认事项管理整改 Phase 2）

Revision ID: a4c9f1d2b7e3
Revises: f9b3e2a6c5d8
Create Date: 2026-11-04

变更内容：
1. 新增 kind 区分 tool_approval（工具审批）/ artifact_review（业务对象审核）；
2. task_id 放宽为可空（业务对象审核不依赖 AgentTask）；
3. status 长度 16 -> 20，承载 revision_requested / deferred / expired / cancelled；
4. 新增业务对象审核字段：session_id / project_id / target_type / target_id /
   target_version / round / confirmation_type / question / allowed_responses /
   user_response / resolution / idempotency_key / resolved_at。

SQLite 不支持直接改列，统一使用 batch_alter_table。
"""
from alembic import op
import sqlalchemy as sa

revision = "a4c9f1d2b7e3"
down_revision = "f9b3e2a6c5d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_confirmation") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(16), nullable=False, server_default="tool_approval")
        )
        batch.alter_column("task_id", existing_type=sa.String(32), nullable=True)
        batch.alter_column(
            "tool_name",
            existing_type=sa.String(64),
            nullable=False,
            server_default="",
        )
        batch.alter_column("status", existing_type=sa.String(16), type_=sa.String(20))
        batch.add_column(sa.Column("session_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("project_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("target_type", sa.String(16), nullable=True))
        batch.add_column(sa.Column("target_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("target_version", sa.Integer, nullable=True))
        batch.add_column(sa.Column("round", sa.Integer, nullable=True, server_default="1"))
        batch.add_column(sa.Column("confirmation_type", sa.String(32), nullable=True))
        batch.add_column(sa.Column("question", sa.Text, nullable=True))
        batch.add_column(sa.Column("allowed_responses", sa.JSON, nullable=True))
        batch.add_column(sa.Column("user_response", sa.Text, nullable=True))
        batch.add_column(sa.Column("resolution", sa.JSON, nullable=True))
        batch.add_column(sa.Column("idempotency_key", sa.String(128), nullable=True))
        batch.add_column(sa.Column("resolved_at", sa.DateTime, nullable=True))

    op.create_index("ix_agent_confirmation_kind", "agent_confirmation", ["kind"])
    op.create_index(
        "ix_agent_confirmation_session_id", "agent_confirmation", ["session_id"]
    )
    op.create_index(
        "ix_agent_confirmation_project_id", "agent_confirmation", ["project_id"]
    )
    op.create_index(
        "ix_agent_confirmation_target_id", "agent_confirmation", ["target_id"]
    )
    op.create_index(
        "ix_agent_confirmation_idem_key", "agent_confirmation", ["idempotency_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_confirmation_idem_key", table_name="agent_confirmation")
    op.drop_index("ix_agent_confirmation_target_id", table_name="agent_confirmation")
    op.drop_index("ix_agent_confirmation_project_id", table_name="agent_confirmation")
    op.drop_index("ix_agent_confirmation_session_id", table_name="agent_confirmation")
    op.drop_index("ix_agent_confirmation_kind", table_name="agent_confirmation")

    with op.batch_alter_table("agent_confirmation") as batch:
        batch.drop_column("resolved_at")
        batch.drop_column("idempotency_key")
        batch.drop_column("resolution")
        batch.drop_column("user_response")
        batch.drop_column("allowed_responses")
        batch.drop_column("question")
        batch.drop_column("confirmation_type")
        batch.drop_column("round")
        batch.drop_column("target_version")
        batch.drop_column("target_id")
        batch.drop_column("target_type")
        batch.drop_column("project_id")
        batch.drop_column("session_id")
        batch.alter_column("status", existing_type=sa.String(20), type_=sa.String(16))
        batch.alter_column("task_id", existing_type=sa.String(32), nullable=False)
        batch.drop_column("kind")
