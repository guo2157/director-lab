"""工作流不可变版本表（P2）

Revision ID: b8d2f5c1e9a3
Revises: a7c1e9b4d2f5
Create Date: 2026-11-18

背景（docs/plan/workflow-dynamic-audit.md D3）：
- 现有 `workflow.version` 是可变整数，PATCH 时原地覆盖 `definition`，
  与 docs/API.md:431「生成新的不可变工作流版本」矛盾；
- 历史任务的 `job.workflow_version` 因此指向被改过的内容，无法复现。

本迁移：
1) 新建 `workflow_version`（unique(workflow_id, version)），定义一旦写入不再修改；
2) `workflow` 增加 current_version_id / status / validation_status / runtime_status；
3) 回填：为每条现有 workflow 生成一条与其当前 version 对应的版本记录，并置为 current。

注意：历史任务无法回溯到「已被覆盖的前一个版本」——那部分数据在旧模型下从未存在，
本迁移只能保证「从此以后」的修改是可复现的。
"""
import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "b8d2f5c1e9a3"
down_revision: str | Sequence[str] | None = "a7c1e9b4d2f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> set[str]:
    conn = op.get_bind()
    return {row[1] for row in conn.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    conn = op.get_bind()

    cols = _columns("workflow")
    if "current_version_id" not in cols:
        op.add_column("workflow", sa.Column("current_version_id", sa.String(length=32), nullable=True))
    if "status" not in cols:
        op.add_column("workflow", sa.Column("status", sa.String(length=24), nullable=False,
                                           server_default="published"))
    if "validation_status" not in cols:
        op.add_column("workflow", sa.Column("validation_status", sa.String(length=24), nullable=False,
                                           server_default="unchecked"))
    if "runtime_status" not in cols:
        op.add_column("workflow", sa.Column("runtime_status", sa.String(length=24), nullable=False,
                                           server_default="unchecked"))

    if "workflow_version" not in _columns("workflow_version") and not conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_version'")
    ).first():
        op.create_table(
            "workflow_version",
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("workflow_id", sa.String(length=32), sa.ForeignKey("workflow.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("definition", sa.JSON(), nullable=False),
            sa.Column("file_hash", sa.String(length=64), nullable=True),
            sa.Column("parsed_schema", sa.JSON(), nullable=True),
            sa.Column("schema_version", sa.String(length=16), nullable=True),
            sa.Column("parser_version", sa.String(length=16), nullable=True),
            sa.Column("validation_status", sa.String(length=24), nullable=False,
                      server_default="unchecked"),
            sa.Column("runtime_status", sa.String(length=24), nullable=False,
                      server_default="unchecked"),
            sa.Column("dependency_snapshot", sa.JSON(), nullable=True),
            sa.Column("changelog", sa.Text(), nullable=True),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workflow_id", "version", name="uq_workflow_version_wf_version"),
        )
        op.create_index("ix_workflow_version_workflow_id", "workflow_version", ["workflow_id"])

    # 回填：每条 workflow 生成一条对应其当前 version 的记录
    rows = conn.execute(sa.text(
        "SELECT id, version, definition, parsed_schema, file_hash, current_version_id "
        "FROM workflow"
    )).fetchall()
    for wf_id, version, definition, parsed_schema, file_hash, current_version_id in rows:
        if current_version_id:
            continue
        exists = conn.execute(
            sa.text("SELECT id FROM workflow_version WHERE workflow_id=:w AND version=:v"),
            {"w": wf_id, "v": version},
        ).first()
        if exists:
            v_id = exists[0]
        else:
            v_id = f"wfv_{uuid.uuid4().hex[:12]}"
            definition_json = definition if isinstance(definition, str) else json.dumps(
                definition or {}, ensure_ascii=False)
            schema_json = parsed_schema if isinstance(parsed_schema, str) else json.dumps(
                parsed_schema or {}, ensure_ascii=False)
            conn.execute(
                sa.text(
                    "INSERT INTO workflow_version "
                    "(id, workflow_id, version, definition, file_hash, parsed_schema, "
                    " schema_version, parser_version, validation_status, runtime_status, "
                    " dependency_snapshot, changelog, is_current, created_at) "
                    "VALUES (:id, :wf, :v, :def, :hash, :schema, '1.0', '1.0', "
                    " 'static_validated', 'unchecked', NULL, :changelog, 1, :now)"
                ),
                {
                    "id": v_id, "wf": wf_id, "v": version,
                    "def": definition_json, "hash": file_hash, "schema": schema_json,
                    "changelog": "backfill: 迁移前的当前定义快照",
                    "now": datetime.now(timezone.utc).replace(tzinfo=None),
                },
            )
        conn.execute(
            sa.text("UPDATE workflow SET current_version_id=:vid WHERE id=:wf AND current_version_id IS NULL"),
            {"vid": v_id, "wf": wf_id},
        )


def downgrade() -> None:
    cols = _columns("workflow")
    for col in ("runtime_status", "validation_status", "status", "current_version_id"):
        if col in cols:
            op.drop_column("workflow", col)
    conn = op.get_bind()
    if conn.execute(sa.text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_version'"
    )).first():
        op.drop_index("ix_workflow_version_workflow_id", table_name="workflow_version")
        op.drop_table("workflow_version")
