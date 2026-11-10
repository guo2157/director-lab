# Director-Lab 系列化改造 · 详细实施计划

> 前置文档：`00-current-architecture-audit.md`  
> 迁移基线（实测）：唯一 head = `62f96f83efa9`（`add_task_stage_agent_name`）  
> 原则：只增不改、复用优先、分阶段可回归、真实状态不造假

---

## 1. 总体策略

| 决策 | 内容 |
|---|---|
| 部署形态 | 保持单体 FastAPI + 独立 worker，不引入微服务、不引入消息中间件、不引入向量库 |
| 关联方式 | 新增 `series` 作为故事层；`series_episodes.project_id` 关联现有生产工作空间；`project` 表不加破坏性约束 |
| 复用边界 | 生产链路（解析/资产/分镜/视频/音频/合成/导出）完全复用；系列层只做「创作—确认—入账—上下文」 |
| 数据兼容 | 所有新表新列；历史 Project 不迁移、不拆分、不改 ID |
| 状态权威 | 正式剧情状态唯一写入方为 `StateCommitService`；Agent 只能产出提案 |
| 幂等 | 状态提交使用 `idempotency_key` 唯一约束 + 事务；事件使用 `event_key` 唯一约束 |

---

## 2. 数据库变更

### 2.1 新增表（10 张）

**series**
```text
id VARCHAR(32) PK            srs_ 前缀
name VARCHAR(200) NOT NULL
description TEXT
cover_image VARCHAR(500)
genre VARCHAR(64)
style VARCHAR(200)
target_audience VARCHAR(64)
language VARCHAR(16) DEFAULT 'zh-CN'
status VARCHAR(16) DEFAULT 'active'      active / archived
mainline_id VARCHAR(32) DEFAULT 'main'   V1 固定单主线
latest_confirmed_episode_id VARCHAR(32)  latest_completed_episode_id VARCHAR(32)
state_version INT NOT NULL DEFAULT 1
created_at / updated_at
INDEX ix_series_status(status)
```

**series_episodes**
```text
id VARCHAR(32) PK            sep_ 前缀
series_id VARCHAR(32) NOT NULL FK series.id
project_id VARCHAR(32) NULL FK project.id       建集后异步创建/绑定
project_episode_no INT NULL                     历史多集项目兼容用
episode_number INT NOT NULL
title VARCHAR(200)
synopsis TEXT
script_status VARCHAR(24) DEFAULT 'draft'
production_status VARCHAR(24) DEFAULT 'not_started'
script_current_version_id VARCHAR(32)
confirmed_script_version_id VARCHAR(32)
context_manifest_id VARCHAR(32)
continuity_status VARCHAR(24) DEFAULT 'not_checked'
mainline_id VARCHAR(32) DEFAULT 'main'
created_at / updated_at
UNIQUE(series_id, episode_number)
INDEX ix_series_episodes_series(series_id, episode_number)
INDEX ix_series_episodes_status(series_id, script_status, production_status)
```

**series_bible_versions**
```text
id / series_id / version INT / content JSON / change_summary TEXT
created_by VARCHAR(64) / created_at / is_active BOOL DEFAULT 0
UNIQUE(series_id, version)
```

**series_script_versions**（叙事层剧本版本）
```text
id / episode_id FK series_episodes.id / version INT / content TEXT / synopsis TEXT
source_context_manifest_id VARCHAR(32)
prompt_template_version VARCHAR(64) / model VARCHAR(64)
validation_result JSON / continuity_result JSON
status VARCHAR(24)  draft/analyzing/awaiting_review/confirmed/superseded/rejected
created_at / created_by
UNIQUE(episode_id, version)
```

**series_assets**（共享资产关联，不改 asset 表语义）
```text
id / series_id FK / asset_id FK asset.id / first_episode_id
created_at
UNIQUE(series_id, asset_id)
```

**episode_asset_refs**
```text
id / episode_id FK / asset_id FK / asset_version INT
usage_type VARCHAR(24)   character/scene/prop
is_required BOOL / source VARCHAR(24)
created_at
UNIQUE(episode_id, asset_id, asset_version, usage_type)
```

**story_events**
```text
id / series_id / episode_id / script_version_id
event_key VARCHAR(64)                       幂等键（同版本内稳定）
event_type VARCHAR(40) / title VARCHAR(200) / description TEXT
involved_asset_ids JSON / location_asset_id / source_scene_id / source_text TEXT
fact_status VARCHAR(16)  confirmed/derived/uncertain/proposed/contradicted
importance INT DEFAULT 3 / occurred_order INT
created_at / created_by
UNIQUE(episode_id, script_version_id, event_key)
INDEX (series_id, episode_id) / (series_id, fact_status)
```

**continuity_snapshots**
```text
id / series_id / mainline_id / version INT
based_on_episode_id / based_on_script_version_id
state_json JSON / state_hash VARCHAR(64)
created_at / created_by
UNIQUE(series_id, version)
```

**continuity_change_proposals**
```text
id / series_id / episode_id / script_version_id
base_state_version INT / proposal_json JSON
status VARCHAR(16)  pending/committed/rejected
idempotency_key VARCHAR(80)
created_at / committed_at
UNIQUE(idempotency_key)
```

**episode_context_manifests**
```text
id / series_id / episode_id / context_version INT
manifest_json JSON      实际使用内容与引用（不只存指针）
context_hash VARCHAR(64)
prompt_template_version / model
created_at
UNIQUE(episode_id, context_version)
```

**plot_threads**
```text
id / series_id / title / description
introduced_episode_id / last_updated_episode_id
status VARCHAR(16)  open/in_progress/resolved/abandoned
related_asset_ids JSON / related_event_ids JSON
resolution_condition TEXT
created_at / updated_at
INDEX (series_id, status)
```

### 2.2 迁移文件

- 单文件 `alembic/versions/<rev>_series_management_tables.py`，`down_revision = '62f96f83efa9'`。
- `upgrade()`：10 张 `create_table` + 索引；全部可空或带 server_default。
- `downgrade()`：按依赖逆序 `drop_table`。
- 迁移前备份：`cp data/director_lab.db data/backups/director_lab.db.bak_series_$(date +%Y%m%d%H%M%S)`，并执行 `PRAGMA wal_checkpoint(TRUNCATE)`。

### 2.3 不做的事

- 不改 `asset.project_id` 的非空约束。
- 不给 `project` 加非空 `series_id`。
- 不重命名既有 `episode` 相关列。
- 不建外键级联删除（系列归档不删历史）。

---

## 3. 后端模块

```text
backend/app/
├── models/series.py                 series / series_episodes / bible / script_versions
├── models/series_story.py           story_events / snapshots / proposals / manifests / plot_threads
├── models/series_asset.py           series_assets / episode_asset_refs
├── schemas/series.py                Pydantic 输入输出（严格校验、长度限制）
├── services/series/
│   ├── series_service.py            系列 CRUD + 归档 + 统计
│   ├── episode_service.py           建集、下一集号、双状态机、Project 绑定
│   ├── bible_service.py             版本创建/激活/历史
│   ├── asset_registry_service.py    系列资产登记、按集引用、身份匹配建议
│   ├── story_event_service.py       事件查询/入账（仅经 StateCommit）
│   ├── narrative_continuity_service.py  快照读取、状态比对、剧情一致性检查
│   ├── plot_thread_service.py       剧情线 CRUD 与状态流转
│   ├── context_builder_service.py   确定性上下文组装 + Manifest 落库
│   ├── state_commit_service.py      事务提交：事件+状态+快照+剧集状态+系列指针
│   └── episode_writing_service.py   续写任务编排（调用 LLM、落草稿）
└── api/v1/series.py                 系列/剧集/上下文/连续性/剧本/剧情线 路由
```

命名避让：帧连续性仍为 `services/continuity.py`；叙事连续性使用 `narrative_continuity_service.py`。

---

## 4. API 设计（新增前缀，不改动旧接口）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/series` | 创建系列（同时创建 Bible V1） |
| GET | `/api/series` | 分页列表 + keyword |
| GET | `/api/series/{id}` | 详情（含统计） |
| PATCH | `/api/series/{id}` | 更新基础信息 |
| POST | `/api/series/{id}/archive` | 归档（不物理删除） |
| GET/PUT | `/api/series/{id}/bible` | 读取当前版本 / 创建新版本 |
| GET | `/api/series/{id}/bible/versions` | 版本历史 |
| POST | `/api/series/{id}/episodes` | 创建剧集（校验集号唯一） |
| GET | `/api/series/{id}/episodes` | 剧集列表 |
| GET | `/api/episodes/{id}` | 剧集详情 |
| POST | `/api/series/{id}/episodes/continue` | 创建续写任务（202 + task_id） |
| GET | `/api/series/{id}/context-preview` | 上下文预览（同一构建规则） |
| GET | `/api/episodes/{id}/context-manifest` | 最近一次 Manifest |
| GET | `/api/episodes/{id}/scripts` | 剧本版本列表 |
| POST | `/api/episodes/{id}/scripts` | 保存草稿（新版本） |
| POST | `/api/episodes/{id}/scripts/{vid}/confirm` | 确认剧本（带 expected_state_version） |
| POST | `/api/episodes/{id}/scripts/{vid}/regenerate` | 重新生成（新版本） |
| GET | `/api/series/{id}/story-events` | 事件时间线（可按集过滤） |
| GET | `/api/series/{id}/continuity` | 当前状态 + 最新快照版本 |
| GET | `/api/series/{id}/continuity/snapshots` | 快照历史 |
| GET | `/api/series/{id}/plot-threads` | 剧情线列表 |
| POST | `/api/series/{id}/plot-threads` | 新建剧情线 |
| POST | `/api/episodes/{id}/continuity/check` | 对指定剧本版本执行检查 |
| GET | `/api/episodes/{id}/continuity/issues` | 冲突列表 |
| GET | `/api/series/{id}/assets` | 系列共享资产 |
| POST | `/api/episodes/{id}/assets/resolve` | 资产匹配建议（不落正式状态） |

响应统一沿用 `{"detail": {...}}` 错误格式；分页沿用现有 `Paginated` 结构。

---

## 5. Agent 工作流

```text
用户续写意图
  → intent_router 新增 series_continue 意图
  → 创建 Task（stage: context_building → writing → validating → awaiting_review）
  → ContextBuilderService 确定性构建并保存 Manifest（失败即 fail_task，不用空上下文）
  → LLM 生成大纲 → 完整剧本
  → 事件抽取（候选，fact_status=proposed）
  → NarrativeContinuityService 检查 → issues 落 continuity_result
  → 保存 series_script_versions（status=awaiting_review）
  → SSE 推送真实阶段进度
```

硬约束：写操作工具 `permission=write` 仍需 `AgentConfirmation`；Agent 不调用 `StateCommitService`。

---

## 6. 前端改造

| 文件 | 动作 |
|---|---|
| `src/types/series.ts` | 新增类型 |
| `src/api/series.ts` | 新增 API 封装（复用 `client.ts`） |
| `src/views/series/SeriesListView.vue` | 系列列表、搜索、空状态、续写入口 |
| `src/views/series/SeriesDetailView.vue` | 顶部信息 + 5 个 Tab |
| `src/views/series/SeriesCreateView.vue` | 创建表单 |
| `src/views/series/EpisodeContinueView.vue` | 续写表单 + 上下文预览 + 真实进度 |
| `src/views/series/EpisodeReviewView.vue` | 剧本审核、冲突来源、确认 |
| `src/components/series/*` | SeriesCard / EpisodeStatus / ContinuityIssueList / ContextPreviewDrawer / BibleEditor / PlotThreadPanel |
| `src/router/index.ts` | 追加 `/series`、`/series/new`、`/series/:id`、`/series/:id/continue`、`/episodes/:id/review` |
| `src/components/AppLayout.vue` | 导航追加「系列管理」，不改动既有项 |

状态覆盖：loading / empty / error / stale / submitting / conflict(409) / forbidden(403) / running。进度只来自 SSE 与任务查询。

---

## 7. 分阶段实施与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 审计 + 计划 | 本文档与 00 文档 |
| P1 | 数据表 + 迁移 + Series/Episode/Bible CRUD | 建系列、建多集、旧接口回归通过 |
| P2 | 前端系列管理 | 列表/详情/创建可用、移动端可用 |
| P3 | Context Builder + Manifest | 上下文可复现、缺失即失败 |
| P4 | 续写 Agent + 异步任务 | 202 建任务、真实进度、草稿落库 |
| P5 | 事件账本 + 快照 + State Commit | 幂等、版本冲突拒绝、Agent 无法直写 |
| P6 | 审核页 + 冲突展示 + 影响分析 | 冲突可定位到事件与剧本版本 |
| P7 | 优化项 | 留待后续 |

每阶段：列改动文件 → 实施 → 迁移 → `pytest` 全量 → 更新文档。

---

## 8. 测试方案

- 单元：集号计算、Bible 版本递增、状态机非法流转、Context Builder 缺项失败、幂等键重复提交、`expected_state_version` 不匹配拒绝。
- 集成：正常续写链路、未确认上一集拦截、道具冲突检出、历史修改影响标记、并发提交互不覆盖、视频未完成仍可续写。
- 回归：现有 `tests/` 全量必须通过。

---

## 9. 风险与回滚

- 迁移前备份 DB（含 WAL）；`downgrade()` 可完整删表。
- 新增列全部可空/带默认，旧代码可忽略新表运行。
- 若 P1 上线后发现问题，直接 `alembic downgrade -1` 并恢复备份。
