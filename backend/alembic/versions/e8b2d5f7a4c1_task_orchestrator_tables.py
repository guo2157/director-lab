"""task / task_stage / task_event / task_result 统一任务编排层表（对话式任务执行改造 Phase 2）
Revision ID: e8b2d5f7a4c1
Revises: c7d9e1f5a3b8
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = "e8b2d5f7a4c1"
down_revision = "c7d9e1f5a3b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("session_id", sa.String(32), nullable=True),
        sa.Column("agent_task_id", sa.String(32), nullable=True),
        sa.Column("project_id", sa.String(32), nullable=True),
        sa.Column("task_type", sa.String(32), nullable=False, server_default="custom"),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("progress", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("error_msg", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_task_session_id", "task", ["session_id"])
    op.create_index("ix_task_agent_task_id", "task", ["agent_task_id"])
    op.create_index("ix_task_project_id", "task", ["project_id"])
    op.create_index("ix_task_status", "task", ["status"])
    op.create_index("ix_task_session_status", "task", ["session_id", "status"])
    op.create_index("ix_task_project_type", "task", ["project_id", "task_type"])

    op.create_table(
        "task_stage",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("task.id"), nullable=False),
        sa.Column("stage_index", sa.Integer, nullable=False, server_default="0"),
        sa.Column("stage_key", sa.String(64), nullable=False, server_default=""),
        sa.Column("stage_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("progress", sa.Integer, nullable=False, server_default="0"),
        sa.Column("job_id", sa.String(32), nullable=True),
        sa.Column("result", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_task_stage_task_id", "task_stage", ["task_id"])
    op.create_index("ix_task_stage_job_id", "task_stage", ["job_id"])
    op.create_index("ix_task_stage_task_idx", "task_stage", ["task_id", "stage_index"])

    op.create_table(
        "task_event",
        sa.Column("event_seq", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("task.id"), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_task_event_task_id", "task_event", ["task_id"])

    op.create_table(
        "task_result",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("task.id"), nullable=False),
        sa.Column("result_type", sa.String(32), nullable=False, server_default="summary"),
        sa.Column("data", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_task_result_task_id", "task_result", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_result_task_id", table_name="task_result")
    op.drop_table("task_result")
    op.drop_index("ix_task_event_task_id", table_name="task_event")
    op.drop_table("task_event")
    op.drop_index("ix_task_stage_task_idx", table_name="task_stage")
    op.drop_index("ix_task_stage_job_id", table_name="task_stage")
    op.drop_index("ix_task_stage_task_id", table_name="task_stage")
    op.drop_table("task_stage")
    op.drop_index("ix_task_project_type", table_name="task")
    op.drop_index("ix_task_session_status", table_name="task")
    op.drop_index("ix_task_status", table_name="task")
    op.drop_index("ix_task_project_id", table_name="task")
    op.drop_index("ix_task_agent_task_id", table_name="task")
    op.drop_index("ix_task_session_id", table_name="task")
    op.drop_table("task")
