# 02 数据库设计（Series / Episode / Bible / Asset Registry / Story Events / Continuity / Context）

> 事实来源：`backend/alembic/versions/a1f4c8e2d907_series_management_tables.py`（唯一 head，down_revision = `62f96f83efa9`）、`backend/app/models/series.py`、`series_asset.py`、`series_story.py`、`ids.py`。
> 本文件描述的是**已落地**的表结构，不是草案。任何后续改动必须以新迁移追加，禁止改动本迁移。

## 1. 设计原则

1. **不复制既有资产系统**：系列资产只做登记（`series_assets`），真实资产仍在 `asset` 表，版本仍由既有 Asset 版本机制管理。
2. **剧情状态与资产属性分离**：道具持有人等剧情状态写入 `continuity_snapshots.state_json`，不写入 Asset 字段。
3. **只有 confirmed 事件是正式剧情**：`story_events.fact_status` 默认 `proposed`，确认剧本时由 StateCommitService 落为 `confirmed`。
4. **版本不覆盖**：Bible、剧本、快照全部按 `version` 递增，唯一约束保证同一序列内版本不重复。
5. **乐观锁在系列维度**：`series.state_version` 是剧情状态版本；每次确认剧本 +1，用于并发校验。
6. **主键前缀语义化**：沿用 `ids.new_id(prefix)`，不使用数据库自增。

## 2. 表清单

| # | 表名 | 主键前缀 | 职责 |
|---|------|----------|------|
| 1 | `series` | `srs_` | 系列主档、状态版本、指针 |
| 2 | `series_episodes` | `eps_` | 剧集（叙事维度），可绑定既有 Project |
| 3 | `series_bible_versions` | `sbv_` | 系列设定版本 |
| 4 | `series_script_versions` | `ssv_` | 剧集剧本版本 |
| 5 | `series_assets` | `sas_` | 系列共享资产登记（引用 `asset.id`） |
| 6 | `episode_asset_refs` | `ear_` | 某集实际使用的资产与版本 |
| 7 | `story_events` | `sev_` | 剧情事件台账 |
| 8 | `continuity_snapshots` | `csn_` | 剧情状态快照（版本化） |
| 9 | `continuity_change_proposals` | `ccp_` | 状态变更提案 + 幂等键 |
| 10 | `episode_context_manifests` | `ecm_` | 上下文清单（实际引用版本的证据） |
| 11 | `plot_threads` | `plt_` | 剧情线（伏笔）台账 |

> 说明：迁移中除计划内的 10 张表外，`episode_asset_refs` 单独成表，共 11 张物理表；其中 `episode_asset_refs` 属于 Asset Registry 的一部分。

## 3. 表结构明细

### 3.1 series

| 字段 | 类型 | 空 | 默认 | 说明 |
|------|------|----|------|------|
| id | String(32) | PK | - | `srs_` |
| name | String(200) | NOT NULL | - | 系列名 |
| description | Text | NULL | - | 简介 |
| cover_image | String(500) | NULL | - | 封面地址 |
| genre | String(64) | NULL | - | 题材 |
| style | String(200) | NULL | - | 风格 |
| target_audience | String(64) | NULL | - | 目标受众 |
| language | String(16) | NOT NULL | `zh-CN` | 语言 |
| status | String(16) | NOT NULL | `active` | `active` / `archived` |
| mainline_id | String(32) | NOT NULL | `main` | V1 固定单主线 |
| latest_confirmed_episode_id | String(32) | NULL | - | 最近确认剧本的剧集 |
| latest_completed_episode_id | String(32) | NULL | - | 最近完成视频的剧集 |
| state_version | Integer | NOT NULL | `1` | 剧情状态版本（乐观锁） |
| created_at / updated_at | DateTime | NOT NULL | - | 时间戳 |

索引：`ix_series_status (status)`

### 3.2 series_episodes

| 字段 | 类型 | 空 | 默认 | 说明 |
|------|------|----|------|------|
| id | String(32) | PK | - | `eps_` |
| series_id | String(32) | NOT NULL | - | FK → `series.id` |
| project_id | String(32) | NULL | - | FK → `project.id`，单集生产项目 |
| project_episode_no | Integer | NULL | - | 历史多集项目内的集号，不迁移不拆分 |
| episode_number | Integer | NOT NULL | - | 系列内集号 |
| title | String(200) | NULL | - | 剧集标题 |
| synopsis | Text | NULL | - | 梗概 |
| script_status | String(24) | NOT NULL | `draft` | 剧本状态 |
| production_status | String(24) | NOT NULL | `not_started` | 生产状态（与剧本状态独立） |
| script_current_version_id | String(32) | NULL | - | 当前剧本版本 |
| confirmed_script_version_id | String(32) | NULL | - | 已确认剧本版本 |
| context_manifest_id | String(32) | NULL | - | 最近使用的上下文清单 |
| continuity_status | String(24) | NOT NULL | `not_checked` | 剧情连续性状态 |
| mainline_id | String(32) | NOT NULL | `main` | 主线 |
| created_at / updated_at | DateTime | NOT NULL | - | 时间戳 |

约束：`uq_series_episode_number (series_id, episode_number)`
索引：`ix_series_episodes_series (series_id, episode_number)`、`ix_series_episodes_status (series_id, script_status, production_status)`

> 表名用 `series_episodes` 是为了避开既有 `scene_episodes` 之外又与生产层 Episode 概念混淆。

### 3.3 series_bible_versions

| 字段 | 类型 | 空 | 默认 |
|------|------|----|------|
| id | String(32) PK | - | `sbv_` |
| series_id | String(32) FK | NOT NULL |
| version | Integer | NOT NULL |
| content | JSON | NULL |
| change_summary | Text | NULL |
| created_by | String(64) | NULL |
| created_at | DateTime | NOT NULL |
| is_active | Boolean | NOT NULL `0` |

约束：`uq_series_bible_series_version (series_id, version)`
索引：`ix_series_bible_series_active (series_id, is_active)`

> `is_active` 为真表示当前生效版本；保存新版本时旧版本置为 false，历史保留。

### 3.4 series_script_versions

| 字段 | 类型 | 空 | 默认 | 说明 |
|------|------|----|------|------|
| id | String(32) PK | - | - | `ssv_` |
| episode_id | String(32) FK | NOT NULL | - | → `series_episodes.id` |
| version | Integer | NOT NULL | - | 集内版本号 |
| content | Text | NULL | - | 剧本正文 |
| synopsis | Text | NULL | - | 梗概 |
| source_context_manifest_id | String(32) | NULL | - | 生成本版本所用上下文清单 |
| prompt_template_version | String(64) | NULL | - | 提示词模板版本 |
| model | String(64) | NULL | - | 模型标识 |
| validation_result | JSON | NULL | - | 校验结果 / review_note |
| continuity_result | JSON | NULL | - | 确认时的连续性检查结果 |
| status | String(24) | NOT NULL | `draft` | `draft`/`analyzing`/`awaiting_review`/`confirmed`/`superseded`/`rejected` |
| created_at | DateTime | NOT NULL | - |
| created_by | String(64) | NULL | - |

约束：`uq_series_script_episode_version (episode_id, version)`
索引：`ix_series_script_episode_status (episode_id, status)`

### 3.5 series_assets

| 字段 | 类型 | 空 | 说明 |
|------|------|----|------|
| id | String(32) PK | - | `sas_` |
| series_id | String(32) FK | NOT NULL | → `series.id` |
| asset_id | String(32) FK | NOT NULL | → `asset.id` |
| first_episode_id | String(32) | NULL | 首次出现剧集 |
| created_at | DateTime | NOT NULL | |

约束：`uq_series_asset_series_asset (series_id, asset_id)`
索引：`ix_series_assets_series`、`ix_series_assets_asset`

> 登记不等于复制：不新增 Asset 记录，不改 `asset.project_id`。

### 3.6 episode_asset_refs

| 字段 | 类型 | 空 | 默认 | 说明 |
|------|------|----|------|------|
| id | String(32) PK | - | - | `ear_` |
| episode_id | String(32) FK | NOT NULL | - | → `series_episodes.id` |
| asset_id | String(32) FK | NOT NULL | - | → `asset.id` |
| asset_version | Integer | NOT NULL | - | 使用版本 |
| usage_type | String(24) | NOT NULL | - | character/prop/location 等 |
| is_required | Boolean | NOT NULL | `0` | 是否必需 |
| source | String(24) | NULL | - | manual/resolve 等来源 |
| created_at | DateTime | NOT NULL | - | |

约束：`uq_episode_asset_ref_unique (episode_id, asset_id, asset_version, usage_type)`
索引：`ix_episode_asset_refs_episode`、`ix_episode_asset_refs_asset`

> 记录的是**实际使用的版本**，因此可以检测 `version_drift`（当前版本 ≠ 使用版本）。

### 3.7 story_events

| 字段 | 类型 | 空 | 默认 | 说明 |
|------|------|----|------|------|
| id | String(32) PK | - | - | `sev_` |
| series_id | String(32) FK | NOT NULL | - | |
| episode_id | String(32) FK | NOT NULL | - | |
| script_version_id | String(32) | NOT NULL | - | 来源剧本版本 |
| event_key | String(64) | NOT NULL | - | 集内幂等键 |
| event_type | String(40) | NOT NULL | - | 事件类型（含 plot_resolution 等） |
| title | String(200) | NOT NULL | - | |
| description | Text | NULL | - | |
| involved_asset_ids | JSON | NULL | - | 涉及资产 |
| location_asset_id | String(32) | NULL | - | 地点 |
| source_scene_id | String(32) | NULL | - | 来源场景（若已解析） |
| source_text | Text | NULL | - | 原文摘录 |
| fact_status | String(16) | NOT NULL | `proposed` | `confirmed`/`derived`/`uncertain`/`proposed`/`contradicted` |
| importance | Integer | NOT NULL | `3` | 1–5 |
| occurred_order | Integer | NULL | - | 集内顺序 |
| created_at | DateTime | NOT NULL | - | |
| created_by | String(64) | NULL | - | |

约束：`uq_story_event_episode_version_key (episode_id, script_version_id, event_key)`
索引：`ix_story_events_series_episode (series_id, episode_id)`、`ix_story_events_series_fact (series_id, fact_status)`

### 3.8 continuity_snapshots

| 字段 | 类型 | 空 | 默认 |
|------|------|----|------|
| id | String(32) PK | - | - |
| series_id | String(32) FK | NOT NULL | - |
| mainline_id | String(32) | NOT NULL | `main` |
| version | Integer | NOT NULL | - |
| based_on_episode_id | String(32) | NULL | - |
| based_on_script_version_id | String(32) | NULL | - |
| state_json | JSON | NULL | - |
| state_hash | String(64) | NULL | - |
| created_at | DateTime | NOT NULL | - |
| created_by | String(64) | NULL | - |

约束：`uq_continuity_snapshot_series_version (series_id, version)`
索引：`ix_continuity_snapshots_series (series_id, version)`

`state_json` 结构（由 `_apply_state_patch` 生成）：

```json
{
  "props": { "ast_xxx": { "holder_asset_id": "chr_yyy", "status": "held" } },
  "characters": { "chr_yyy": { "alive": true, "location_asset_id": "ast_loc" } },
  "plot_threads": { "plt_zzz": "resolved" },
  "episode_number": 3
}
```

### 3.9 continuity_change_proposals

| 字段 | 类型 | 空 | 默认 |
|------|------|----|------|
| id | String(32) PK | - | - |
| series_id | String(32) FK | NOT NULL | - |
| episode_id | String(32) FK | NOT NULL | - |
| script_version_id | String(32) | NOT NULL | - |
| base_state_version | Integer | NOT NULL | - |
| proposal_json | JSON | NULL | - |
| status | String(16) | NOT NULL | `pending` |
| idempotency_key | String(80) | NOT NULL | - |
| created_at | DateTime | NOT NULL | - |
| committed_at | DateTime | NULL | - |

约束：`uq_continuity_proposal_idempotency (idempotency_key)`
索引：`ix_continuity_proposals_episode (episode_id, status)`

幂等键构造：`build_idempotency_key(episode_id, script_version_id, action="confirm")` → `confirm:{episode_id}:{script_version_id}`。

### 3.10 episode_context_manifests

| 字段 | 类型 | 空 | 默认 |
|------|------|----|------|
| id | String(32) PK | - | - |
| series_id | String(32) FK | NOT NULL | - |
| episode_id | String(32) FK | NOT NULL | - |
| context_version | Integer | NOT NULL | - |
| manifest_json | JSON | NULL | - |
| context_hash | String(64) | NULL | - |
| prompt_template_version | String(64) | NULL | - |
| model | String(64) | NULL | - |
| created_at | DateTime | NOT NULL | - |

约束：`uq_context_manifest_episode_version (episode_id, context_version)`
索引：`ix_context_manifests_series_episode (series_id, episode_id)`

> `context_hash` 由 `_hash(payload)` 生成，用于证明同一输入得到同一上下文。

### 3.11 plot_threads

| 字段 | 类型 | 空 | 默认 |
|------|------|----|------|
| id | String(32) PK | - | - |
| series_id | String(32) FK | NOT NULL | - |
| title | String(200) | NOT NULL | - |
| description | Text | NULL | - |
| introduced_episode_id | String(32) | NULL | - |
| last_updated_episode_id | String(32) | NULL | - |
| status | String(16) | NOT NULL | `open` |
| related_asset_ids | JSON | NULL | - |
| related_event_ids | JSON | NULL | - |
| resolution_condition | Text | NULL | - |
| created_at | DateTime | NOT NULL | - |
| updated_at | DateTime | NULL | - |

索引：`ix_plot_threads_series_status (series_id, status)`

## 4. 状态枚举（与代码一致）

| 枚举 | 取值 |
|------|------|
| `SERIES_STATUS` | active, archived |
| `SCRIPT_STATUS` | draft, analyzing, awaiting_review, confirmed, superseded, rejected |
| `PRODUCTION_STATUS` | not_started, queued, running, reviewing, completed, failed, cancelled |
| `EPISODE_CONTINUITY_STATUS` | not_checked, checking, consistent, warning, conflict, needs_review |
| `STORY_EVENT_FACT_STATUS` | confirmed, derived, uncertain, proposed, contradicted |
| `PLOT_THREAD_STATUS` | open, in_progress, resolved, abandoned |
| `PROPOSAL_STATUS` | pending, committed, rejected |

## 5. 与既有表的关系

| 新表 | 关联既有表 | 关系语义 |
|------|-----------|----------|
| series_episodes | `project` | 可空 FK，一集最多绑定一个生产项目 |
| series_assets | `asset` | 只登记引用，不复制、不改归属 |
| episode_asset_refs | `asset` | 记录某集使用的具体版本 |

> 不新建第二套资产系统：`asset` 仍是唯一资产实体，`asset_version` 仍由既有版本机制维护。

## 6. 迁移与回滚

- 升级：`alembic upgrade head`（从 `62f96f83efa9` → `a1f4c8e2d907`）
- 回滚：`downgrade()` 按逆序删除索引与表，不触碰既有表
- 迁移只新增表与索引，不改动任何既有表结构，因此对单集生产链路无破坏性影响

## 7. 一致性约束汇总（不可放宽）

1. 同一系列 `episode_number` 唯一。
2. 同一剧集剧本 `version` 唯一。
3. 同一系列 Bible `version` 唯一，且同时只有一条 `is_active`。
4. 同一系列快照 `version` 唯一。
5. 同一剧集 + 剧本版本 + `event_key` 唯一（事件幂等）。
6. `idempotency_key` 全局唯一（确认操作幂等）。
7. 同一剧集同一上下文 `context_version` 唯一。
