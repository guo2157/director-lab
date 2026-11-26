"""Agent Runtime 基础模型：agent_goal / agent_plan / agent_execution_log 表 + agent_task 扩展列。

Revision ID: g1a2b3c4d5e6
Revises: a7c3e9f5b1d8
Create Date: 2026-11-25
"""
from alembic import op
import sqlalchemy as sa

revision = "g1a2b3c4d5e6"
down_revision = "a7c3e9f5b1d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── agent_goal ──
    op.create_table(
        "agent_goal",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("session_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("goal_type", sa.String(32), nullable=False, server_default="user_request"),
        sa.Column("constraints", sa.JSON(), nullable=True),
        sa.Column("success_criteria", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_goal_session", "agent_goal", ["session_id"])
    op.create_index("ix_agent_goal_project", "agent_goal", ["project_id"])

    # ── agent_plan ──
    op.create_table(
        "agent_plan",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("goal_id", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["goal_id"], ["agent_goal.id"]),
    )
    op.create_index("ix_agent_plan_goal", "agent_plan", ["goal_id"])

    # ── agent_execution_log ──
    op.create_table(
        "agent_execution_log",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("goal_id", sa.String(32), nullable=True),
        sa.Column("plan_id", sa.String(32), nullable=True),
        sa.Column("step_id", sa.String(32), nullable=True),
        sa.Column("tool_name", sa.String(64), nullable=True),
        sa.Column("input", sa.JSON(), nullable=True),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_execution_log_goal", "agent_execution_log", ["goal_id"])
    op.create_index("ix_agent_execution_log_plan", "agent_execution_log", ["plan_id"])
    op.create_index("ix_agent_execution_log_step", "agent_execution_log", ["step_id"])

    # ── agent_task 扩展 ──
    op.add_column("agent_task", sa.Column("goal_id", sa.String(32), nullable=True))
    op.add_column("agent_task", sa.Column("plan_id", sa.String(32), nullable=True))
    op.add_column("agent_task", sa.Column("current_step_id", sa.String(32), nullable=True))
    op.add_column("agent_task", sa.Column("lifecycle_state", sa.String(32), nullable=True, server_default="created"))
    op.add_column("agent_task", sa.Column("progress_snapshot", sa.JSON(), nullable=True))
    op.add_column("agent_task", sa.Column("resume_state", sa.JSON(), nullable=True))
    op.add_column("agent_task", sa.Column("failure_reason", sa.Text(), nullable=True))

    # ── agent_plan_step 扩展：增加 plan_id 与依赖/验证/重试字段 ──
    op.add_column("agent_plan_step", sa.Column("plan_id", sa.String(32), nullable=True))
    op.add_column("agent_plan_step", sa.Column("description", sa.String(500), nullable=True))
    op.add_column("agent_plan_step", sa.Column("depends_on", sa.JSON(), nullable=True))
    op.add_column("agent_plan_step", sa.Column("preconditions", sa.JSON(), nullable=True))
    op.add_column("agent_plan_step", sa.Column("verification", sa.JSON(), nullable=True))
    op.add_column("agent_plan_step", sa.Column("requires_approval", sa.Boolean(), nullable=True, server_default=sa.text("0")))
    op.add_column("agent_plan_step", sa.Column("retry_count", sa.Integer(), nullable=True, server_default="0"))
    op.add_column("agent_plan_step", sa.Column("max_retries", sa.Integer(), nullable=True, server_default="3"))
    op.add_column("agent_plan_step", sa.Column("timeout_seconds", sa.Integer(), nullable=True, server_default="600"))
    op.add_column("agent_plan_step", sa.Column("started_at", sa.DateTime(), nullable=True))
    op.add_column("agent_plan_step", sa.Column("completed_at", sa.DateTime(), nullable=True))
    op.add_column("agent_plan_step", sa.Column("error_code", sa.String(48), nullable=True))
    op.add_column("agent_plan_step", sa.Column("error_message", sa.Text(), nullable=True))


def downgrade() -> None:
    # agent_plan_step 还原
    op.drop_column("agent_plan_step", "error_message")
    op.drop_column("agent_plan_step", "error_code")
    op.drop_column("agent_plan_step", "completed_at")
    op.drop_column("agent_plan_step", "started_at")
    op.drop_column("agent_plan_step", "timeout_seconds")
    op.drop_column("agent_plan_step", "max_retries")
    op.drop_column("agent_plan_step", "retry_count")
    op.drop_column("agent_plan_step", "requires_approval")
    op.drop_column("agent_plan_step", "verification")
    op.drop_column("agent_plan_step", "preconditions")
    op.drop_column("agent_plan_step", "depends_on")
    op.drop_column("agent_plan_step", "description")
    op.drop_column("agent_plan_step", "plan_id")

    # agent_task 还原
    op.drop_column("agent_task", "failure_reason")
    op.drop_column("agent_task", "resume_state")
    op.drop_column("agent_task", "progress_snapshot")
    op.drop_column("agent_task", "lifecycle_state")
    op.drop_column("agent_task", "current_step_id")
    op.drop_column("agent_task", "plan_id")
    op.drop_column("agent_task", "goal_id")

    # agent_execution_log
    op.drop_table("agent_execution_log")

    # agent_plan
    op.drop_table("agent_plan")

    # agent_goal
    op.drop_table("agent_goal")
