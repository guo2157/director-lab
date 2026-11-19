"""查询性能索引：补齐 EXPLAIN QUERY PLAN 实测出的全表扫描与临时排序

Revision ID: a1d4c7e2f9b3
Revises: c4e8a1d7f2b6
Create Date: 2026-11-19

背景
----
任务中心/作品页在高频轮询下出现后端整体卡死（连 /api/health 都不响应）。
逐条跑 `EXPLAIN QUERY PLAN` 后发现 13 条全表扫描 + 5 条临时 B-Tree 排序，
其中最重的是：

- `job_event.event_seq` 曾疑似缺索引 —— **实测证伪**：该列是 INTEGER PRIMARY KEY
  （rowid 别名），`WHERE event_seq > ?` 已走 `SEARCH ... USING INTEGER PRIMARY KEY`，
  100 次查询仅 0.57ms。因此本迁移**不为 event_seq 建索引**，避免无收益的写放大。
- 真正缺的是「复合过滤 + 排序」组合：单列索引能消掉 WHERE，但 ORDER BY 仍走临时
  B-Tree；以及「只有复合索引、但查询只用尾部列」导致的全表扫描。

本迁移新增（每条都对应实测的 SCAN / TEMP B-TREE）：

job
  - ix_job_project_created   (project_id, created_at)   项目页任务列表倒序分页
  - ix_job_status_created    (status, created_at)       任务中心状态筛选 + 倒序
  - ix_job_shot_id           (shot_id)                  按镜头反查任务（原 SCAN）
  - ix_job_status_finished   (status, finished_at)      /jobs/stats 今日统计
  - ix_job_event_seq         (event_seq)                list_jobs 增量补齐（原 SCAN）

asset
  - ix_asset_project_name_version (project_id, name, version)  版本历史查询

shot
  - ix_shot_episode          (episode)                  按集取镜头（原 SCAN，高频）
  - ix_shot_status           (status)                   合成/审核状态筛选（原 SCAN）
  - ix_shot_scene_seq        (scene_id, seq)            场景内镜头排序（原 TEMP B-TREE）

scene_episode
  - ix_scene_episode_episode_seq (episode, seq_in_episode)  集内场景播放顺序（原 SCAN）

series
  - ix_series_updated_created (updated_at, created_at)  系列列表倒序（原 SCAN）

workflow
  - ix_workflow_type_default  (workflow_type, is_default)  默认工作流选择（原 SCAN）
  - ix_workflow_type_created  (workflow_type, created_at)  工作流匹配排序（原 SCAN）

audio_segment
  - ix_audio_segment_shot_created (shot_id, created_at) 按镜头取音频（原 TEMP B-TREE）

voice_library
  - ix_voice_library_sort_order (sort_order)            音色列表固定排序（原 SCAN）

prop_shot_state
  - ix_pss_shot_id           (shot_id)                  仅按 shot 反查（原 SCAN；
                                                        已有复合索引以 project_id 打头，
                                                        单 shot_id 查询无法使用）

export
  - ix_export_composition_status (composition_id, status)  进度查询（原 SCAN）

task
  - ix_task_agent_task_id_created (agent_task_id, created_at) 任务按意图倒序（原 TEMP B-TREE）

幂等：SQLite 非事务 DDL，先探测同名索引再创建；downgrade 同样先探测再删。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1d4c7e2f9b3"
down_revision: str | Sequence[str] | None = "c4e8a1d7f2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (表名, 索引名, 列清单)
INDEXES: list[tuple[str, str, list[str]]] = [
    ("job", "ix_job_project_created", ["project_id", "created_at"]),
    ("job", "ix_job_status_created", ["status", "created_at"]),
    ("job", "ix_job_shot_id", ["shot_id"]),
    ("job", "ix_job_status_finished", ["status", "finished_at"]),
    ("job", "ix_job_event_seq", ["event_seq"]),
    ("asset", "ix_asset_project_name_version", ["project_id", "name", "version"]),
    ("shot", "ix_shot_episode", ["episode"]),
    ("shot", "ix_shot_status", ["status"]),
    ("shot", "ix_shot_scene_seq", ["scene_id", "seq"]),
    ("scene_episode", "ix_scene_episode_episode_seq", ["episode", "seq_in_episode"]),
    ("series", "ix_series_updated_created", ["updated_at", "created_at"]),
    ("workflow", "ix_workflow_type_default", ["workflow_type", "is_default"]),
    ("workflow", "ix_workflow_type_created", ["workflow_type", "created_at"]),
    ("audio_segment", "ix_audio_segment_shot_created", ["shot_id", "created_at"]),
    ("voice_library", "ix_voice_library_sort_order", ["sort_order"]),
    ("prop_shot_state", "ix_pss_shot_id", ["shot_id"]),
    ("export", "ix_export_composition_status", ["composition_id", "status"]),
    ("task", "ix_task_agent_task_id_created", ["agent_task_id", "created_at"]),
    # 补充：所有 JobEvent 查询都同时约束 job_id + event_type，故建复合而非单列 event_type
    ("job_event", "ix_job_event_job_type_seq", ["job_id", "event_type", "event_seq"]),
    # 任务中心按类型筛选 + 倒序（前端 typeTabs 会传 job_type）
    ("job", "ix_job_type_created", ["job_type", "created_at"]),
]


def _existing_indexes(table: str) -> set[str]:
    conn = op.get_bind()
    return {row[1] for row in conn.execute(sa.text(f"PRAGMA index_list({table})"))}


def upgrade() -> None:
    for table, name, cols in INDEXES:
        if name in _existing_indexes(table):
            continue
        op.create_index(name, table, cols)


def downgrade() -> None:
    for table, name, _cols in reversed(INDEXES):
        if name not in _existing_indexes(table):
            continue
        op.drop_index(name, table_name=table)
