"""补回 asset 版本列并归一化同名重复资产的有效性

Revision ID: b4e8c1a70d29
Revises: f3a7c1d9e2b8

背景：
- 库中 asset 表被按未完成的『无版本』模型重建，缺失 version/parent_id/validity，
  与 ORM 定义不一致，导致 /api/assets 直接 500，资产库只能回退展示历史行。
- 同名资产多版本并存使资产库重复展示（场景 2 个显示 8 张、角色 3 个显示 4 张，
  其中一张是 3.8KB 的早期失败产物）。

处理：
- 补回三列与 ix_asset_project_type_validity 索引；
- 每个 (project_id, asset_type, name) 分组只保留一条 validity=current，
  其余标记 stale（保留记录与文件，可追溯，不物理删除）；
- 保留优先级：实体主图指针所指资产 > 文件最大的真实产物。
"""
from alembic import op
import sqlalchemy as sa

revision = "b4e8c1a70d29"
down_revision = "f3a7c1d9e2b8"
branch_labels = None
depends_on = None

POINTER_COLUMNS = (
    ("character", "portrait_asset_id"),
    ("scene", "baseline_asset_id"),
    ("prop", "anchor_asset_id"),
)


def upgrade() -> None:
    conn = op.get_bind()
    cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(asset)"))}
    if "version" not in cols:
        op.add_column("asset", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    if "parent_id" not in cols:
        op.add_column("asset", sa.Column("parent_id", sa.String(length=32), nullable=True))
    if "validity" not in cols:
        op.add_column(
            "asset",
            sa.Column("validity", sa.String(length=16), nullable=False, server_default="current"),
        )
    indexes = {row[0] for row in conn.execute(sa.text("select name from sqlite_master where type='index'"))}
    if "ix_asset_project_type_validity" not in indexes:
        op.create_index("ix_asset_project_type_validity", "asset", ["project_id", "asset_type", "validity"])
    _normalize_duplicates(conn)


def downgrade() -> None:
    index_names = {row[0] for row in op.get_bind().execute(sa.text("select name from sqlite_master where type='index'"))}
    if "ix_asset_project_type_validity" in index_names:
        op.drop_index("ix_asset_project_type_validity", table_name="asset")
    with op.batch_alter_table("asset") as batch:
        batch.drop_column("validity")
        batch.drop_column("parent_id")
        batch.drop_column("version")


def _pointer_ids(conn) -> set[str]:
    ids: set[str] = set()
    for table, column in POINTER_COLUMNS:
        rows = conn.execute(sa.text("select %s from %s where %s is not null" % (column, table, column)))
        for row in rows:
            if row[0]:
                ids.add(row[0])
    return ids


def _normalize_duplicates(conn) -> None:
    pointers = _pointer_ids(conn)
    groups = conn.execute(
        sa.text(
            "select project_id, asset_type, name from asset "
            "group by project_id, asset_type, name having count(*) > 1"
        )
    ).fetchall()
    for project_id, asset_type, name in groups:
        items = conn.execute(
            sa.text(
                "select id, file_path from asset where project_id = :p "
                "and asset_type = :t and name = :n"
            ),
            {"p": project_id, "t": asset_type, "n": name},
        ).fetchall()
        keeper = None
        for item in items:
            if item[0] in pointers:
                keeper = item
                break
        if keeper is None:
            keeper = max(items, key=lambda r: _file_size(r[1]))
        for item in items:
            validity = "current" if item[0] == keeper[0] else "stale"
            conn.execute(
                sa.text("update asset set validity = :v, version = 1 where id = :i"),
                {"v": validity, "i": item[0]},
            )


def _file_size(path: str | None) -> int:
    import os

    if not path:
        return -1
    base = os.path.dirname(os.path.abspath(__file__))
    backend_dir = os.path.abspath(os.path.join(base, "..", ".."))
    candidates = [path, os.path.join(backend_dir, path), os.path.join(backend_dir, "data", "media", os.path.basename(path))]
    for candidate in candidates:
        try:
            if os.path.exists(candidate):
                return os.path.getsize(candidate)
        except OSError:
            continue
    return -1
