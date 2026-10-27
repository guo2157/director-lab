"""兜底回填实体资产类型：按实体表关联字段反查纠正 keyframe

背景：e1b4c7d3f208 依赖 job.params 中的实体标记回填，若来源任务已被清理
或早期版本未写入标记，则无法覆盖。本迁移改以实体表关联字段为准：
character.portrait_asset_id / scene.baseline_asset_id / prop.anchor_asset_id
指向的资产若仍为 keyframe，纠正为对应实体类型。

本迁移幂等，不改表结构、不删除记录。

Revision ID: e2c5d8a4f319
Revises: e1b4c7d3f208
Create Date: 2026-10-25
"""
from alembic import op
import sqlalchemy as sa

revision = "e2c5d8a4f319"
down_revision = "e1b4c7d3f208"
branch_labels = None
depends_on = None

# (实体表, 关联字段, 目标 asset_type)
_RELATIONS = (
    ("character", "portrait_asset_id", "character"),
    ("scene", "baseline_asset_id", "scene"),
    ("prop", "anchor_asset_id", "prop"),
)


def upgrade() -> None:
    conn = op.get_bind()
    for table, column, target_type in _RELATIONS:
        conn.execute(
            sa.text(
                f"UPDATE asset SET asset_type = :t "
                f"WHERE asset_type = 'keyframe' "
                f"AND id IN (SELECT {column} FROM {table} WHERE {column} IS NOT NULL)"
            ),
            {"t": target_type},
        )


def downgrade() -> None:
    # 仅还原本迁移可能产生的类型，不动 keyframe/clip/audio 原生产物
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE asset SET asset_type = 'keyframe' "
            "WHERE asset_type IN ('character', 'scene', 'prop')"
        )
    )
