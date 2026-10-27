"""回填实体资产类型：定妆图/基准图/锚定图由 keyframe 纠正为 character/scene/prop

背景：worker 落库时曾统一把 cover 输出写成 asset_type=keyframe，导致资产库
「角色/场景/道具」分类按权威枚举（enums.ASSET_TYPE）查询永远无匹配。
本迁移按来源任务的 entity 标记回填存量数据，不修改表结构、不删除任何记录。

判定依据：job.params 中存在 __character_id__ / __scene_id__ / __prop_id__，
且该任务已成功、产物当前有效且 asset_type 仍为 keyframe。

Revision ID: e1b4c7d3f208
Revises: d8f3a2b6c9e1
Create Date: 2026-10-25
"""
from alembic import op
import sqlalchemy as sa

revision = "e1b4c7d3f208"
down_revision = "d8f3a2b6c9e1"
branch_labels = None
depends_on = None

# (job.params 标记, 目标 asset_type)
_ENTITY_MARKS = (
    ("__character_id__", "character"),
    ("__scene_id__", "scene"),
    ("__prop_id__", "prop"),
)


def upgrade() -> None:
    conn = op.get_bind()
    for mark, target_type in _ENTITY_MARKS:
        # 仅回填由带该实体标记的成功任务产出、且仍为 keyframe 的当前有效产物
        conn.execute(
            sa.text(
                "UPDATE asset SET asset_type = :t "
                "WHERE asset_type = 'keyframe' "
                "AND source_job_id IN ("
                "  SELECT id FROM job "
                "  WHERE status = 'succeeded' AND params LIKE :m"
                ")"
            ),
            {"t": target_type, "m": f"%{mark}%"},
        )


def downgrade() -> None:
    # 回滚：把实体类型还原为 keyframe（仅还原本迁移可识别的类型，不动 keyframe/clip/audio）
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE asset SET asset_type = 'keyframe' "
            "WHERE asset_type IN ('character', 'scene', 'prop')"
        )
    )
