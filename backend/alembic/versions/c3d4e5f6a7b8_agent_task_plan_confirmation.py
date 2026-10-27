"""agent_task / agent_plan_step / agent_confirmation 三张表

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-27 14:46:00

B 档（意图识别结构化 + 审批门禁）：
- agent_task：Agent 任务（结构化意图识别结果落库：intent/goal/risk_level/confidence/success_criteria）
- agent_plan_step：任务执行计划步骤（tool_call/clarification/reply，模型提议、代码裁决）
- agent_confirmation：高风险工具审批门禁（pending/approved/rejected）
"""
from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_task",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("session_id", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(32), nullable=True),
        sa.Column("intent", sa.String(32), nullable=False, server_default="query"),
        sa.Column("intent_goal", sa.Text(), nullable=False, server_default=""),
        sa.Column("risk_level", sa.String(8), nullable=False, server_default="low"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("success_criteria", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_task_session_id", "agent_task", ["session_id"])
    op.create_index("ix_agent_task_message_id", "agent_task", ["message_id"])
    op.create_index("ix_agent_task_status", "agent_task", ["status"])

    op.create_table(
        "agent_plan_step",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("step_type", sa.String(16), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=True),
        sa.Column("args", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_plan_step_task_id", "agent_plan_step", ["task_id"])

    op.create_table(
        "agent_confirmation",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("args", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_confirmation_task_id", "agent_confirmation", ["task_id"])
    op.create_index("ix_agent_confirmation_status", "agent_confirmation", ["status"])


def downgrade() -> None:
    op.drop_index("ix_agent_confirmation_status", table_name="agent_confirmation")
    op.drop_index("ix_agent_confirmation_task_id", table_name="agent_confirmation")
    op.drop_table("agent_confirmation")
    op.drop_index("ix_agent_plan_step_task_id", table_name="agent_plan_step")
    op.drop_table("agent_plan_step")
    op.drop_index("ix_agent_task_status", table_name="agent_task")
    op.drop_index("ix_agent_task_message_id", table_name="agent_task")
    op.drop_index("ix_agent_task_session_id", table_name="agent_task")
    op.drop_table("agent_task")
