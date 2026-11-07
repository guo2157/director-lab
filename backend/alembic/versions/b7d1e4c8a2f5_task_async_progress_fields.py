"""task 表新增异步进度字段（对话式任务执行整改：真实进度与耗时）

Revision ID: b7d1e4c8a2f5
Revises: a4c9f1d2b7e3
Create Date: 2026-11-05

新增字段用途：
- message_id：记录发起任务的用户消息，任务完成后据此回推到原对话位置，
  刷新页面/切换会话后仍能定位（不依赖内存上下文）
- current_step / current_message：当前执行步骤标识与展示文案，由确定性代码写入
- started_at：实际开始执行时间，耗时一律由它与当前时间计算

关于 status 长度：模型侧声明为 String(20) 以承载 queued 等取值。
SQLite 的 VARCHAR 长度参数不参与约束校验（不截断、不报错），故无需重建表；
此前用 batch_alter_table 重建表会因 task 被子表外键引用而失败，故移除该步骤。

本迁移按幂等编写：字段/索引已存在时跳过，可安全重复执行。
"""
from alembic import op
import sqlalchemy as sa

revision = "b7d1e4c8a2f5"
down_revision = "a4c9f1d2b7e3"
branch_labels = None
depends_on = None

_NEW_COLUMNS = [
    ("message_id", sa.String(32)),
    ("current_step", sa.String(64)),
    ("current_message", sa.String(500)),
    ("started_at", sa.DateTime()),
]


def _existing_columns() -> set[str]:
    bind = op.get_bind()
    rows = bind.execute(sa.text("PRAGMA table_info(task)")).fetchall()
    return {r[1] for r in rows}


def _existing_indexes() -> set[str]:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='task'")
    ).fetchall()
    return {r[0] for r in rows}


def upgrade() -> None:
    cols = _existing_columns()
    for name, type_ in _NEW_COLUMNS:
        if name not in cols:
            op.add_column("task", sa.Column(name, type_, nullable=True))
    if "ix_task_message_id" not in _existing_indexes():
        op.create_index("ix_task_message_id", "task", ["message_id"])


def downgrade() -> None:
    indexes = _existing_indexes()
    if "ix_task_message_id" in indexes:
        op.drop_index("ix_task_message_id", table_name="task")
    cols = _existing_columns()
    for name, _ in reversed(_NEW_COLUMNS):
        if name in cols:
            op.drop_column("task", name)
