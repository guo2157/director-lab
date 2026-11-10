# 03 接口设计（系列化创作与续集管理）

> 事实来源：`backend/app/api/v1/series.py`、`backend/app/schemas/series.py`，以及 `app.openapi()['paths']` 实测结果。
> 当前 OpenAPI 路径总数 183 条，其中系列相关 30 条（含既有的 `/api/audio/...`、`/api/scenes/...` 匹配项，系列新增实际为 27 条）。

## 1. 通用约定

| 项 | 约定 |
|----|------|
| 前缀 | `/api`（路由文件内不带前缀，由 `main.py` 的 include_router 统一挂载） |
| 错误格式 | 全局异常处理器输出 `{"detail": {"code": "...", "message": "..."}}` |
| 时间格式 | ISO 8601 字符串 |
| 分页 | `page` 从 1 开始，`page_size` 受 `le` 限制 |
| 鉴权 | 单用户本地工具，无多用户鉴权；系列接口只做「系列—剧集—项目」归属一致性校验 |
| 长耗时 | 续写/重新生成返回 **202 + task_id**，不同步等待剧本生成 |
| 写入正式剧情 | 只有 `confirm` 接口可写正式剧情状态，且必须经 StateCommitService |

## 2. 接口清单（27 条）

### 2.1 系列主档（6 条）

#### POST /api/series → 201

创建系列，同时建立 Series Bible V1。

请求体 `SeriesCreate`：

```json
{
  "name": "山海纪",
  "description": "架空东方奇幻",
  "genre": "奇幻",
  "style": "写实水墨",
  "target_audience": "青年",
  "language": "zh-CN"
}
```

响应 `SeriesOut`：`id`(srs_)、`name`、`description`、`cover_image`、`genre`、`style`、`target_audience`、`language`、`status`、`mainline_id`、`latest_confirmed_episode_id`、`latest_completed_episode_id`、`state_version`、`created_at`、`updated_at`。

#### GET /api/series

| 参数 | 类型 | 约束 | 默认 |
|------|------|------|------|
| keyword | str | max_length=100 | null |
| status | str | max_length=16 | null |
| page | int | ge=1 | 1 |
| page_size | int | ge=1, le=100 | 20 |

响应 `SeriesPageOut`：`{ items: SeriesListOut[], total, page, page_size }`。
`SeriesListOut` 在 `SeriesOut` 基础上追加派生统计：`episode_count`、`confirmed_script_count`、`completed_video_count`、`open_plot_thread_count`、`bible_version`。

#### GET /api/series/{series_id}

返回 `SeriesListOut`（含派生统计）。系列不存在时 `biz_error`。

#### PATCH /api/series/{series_id}

请求体 `SeriesUpdate`，只允许修改基础信息字段，不允许直接改 `state_version`。

#### POST /api/series/{series_id}/archive

归档系列。**不物理删除任何历史资产与生产记录**。归档后 `continue` 会返回 `invalid_state_transition`。

#### POST /api/series/{series_id}/unarchive

恢复归档系列。

### 2.2 Bible（3 条）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/series/{series_id}/bible` | 当前激活版本 `BibleOut`；无激活版本时 `series_bible_not_found` |
| PUT | `/api/series/{series_id}/bible` → **201** | 保存为**新版本**，请求体 `BibleSaveIn{content, change_summary}` |
| GET | `/api/series/{series_id}/bible/versions` | 全部版本列表（按版本） |

`BibleOut`：`id`(sbv_)、`series_id`、`version`、`content`(JSON)、`change_summary`、`created_by`、`created_at`、`is_active`。

### 2.3 剧集（5 条）

#### POST /api/series/{series_id}/episodes → 201

请求体 `EpisodeCreate`：`episode_number`(1–9999，可空则自动取下一号)、`title`(≤200)、`synopsis`(≤4000)、`project_id`(≤32)。
要求系列为 active（`get_active_series`）。

#### GET /api/series/{series_id}/episodes

| 参数 | 约束 | 默认 |
|------|------|------|
| page | ge=1 | 1 |
| page_size | ge=1, le=200 | 100 |

响应 `EpisodePageOut`：`{ items: EpisodeOut[], total, page, page_size }`。

`EpisodeOut` 字段：`id`(eps_)、`series_id`、`project_id`、`project_episode_no`、`episode_number`、`title`、`synopsis`、`script_status`、`production_status`、`script_current_version_id`、`confirmed_script_version_id`、`context_manifest_id`、`continuity_status`、`mainline_id`、`created_at`、`updated_at`。

#### GET /api/episodes/{episode_id}

单剧集详情。

#### GET /api/series/{series_id}/next-episode-number

响应 `{ "next_episode_number": 4 }`。

#### GET /api/episodes/{episode_id}/workspace

剧集工作区：返回绑定的 Project 与生产进度入口；生产流程本身复用既有单集接口，不新增生产链路。

### 2.4 续写与重新生成（2 条，均返回 202）

#### POST /api/series/{series_id}/episodes/continue

请求体 `ContinueIn`：

```json
{
  "user_instruction": "本集收束记忆线索，结尾留下新悬念",
  "direction": "悬疑推进",
  "constraints": { "no_new_main_character": true, "max_minutes": 3 },
  "target_episode_number": 4,
  "title": "记忆的裂口"
}
```

行为：
- 剧集不存在时按 `target_episode_number`（缺省用 `next_episode_number`）自动创建，`title` 截断至 200 字符。
- 系列已归档 → `invalid_state_transition`。
- 该集剧本已确认 → `series_script_status_conflict`。
- 创建 `Task`(`task_type=series_continue`) + 4 个阶段 + `Job`(`job_type=series_agent`)，立即返回。

响应 `ContinueOut`：`{ episode_id, episode_number, task_id, status: "context_building" }`。

#### POST /api/episodes/{episode_id}/scripts/{script_id}/regenerate → 202

请求体同为 `ContinueIn`。`script.status == "confirmed"` 时抛 `series_script_status_conflict`（已确认版本不可覆盖，请新建草稿版本后再重新生成）。

### 2.5 上下文（2 条）

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET | `/api/series/{series_id}/context-preview` | `target_episode_number`(ge=1)、`user_instruction`(≤4000) | 与正式生成**同一构建规则**，**不落库** |
| GET | `/api/episodes/{episode_id}/context-manifest` | - | 最近一次上下文清单；无记录时 `biz_error` |

`ContextPreview` 关键字段：`series_bible`、`continuity`、`previous_episode`、`plot_threads`、`history_events`、`assets`、`warnings`、`missing`、`context_hash`。

`ContextManifestOut`：`id`(ecm_)、`series_id`、`episode_id`、`context_version`、`manifest_json`、`context_hash`、`prompt_template_version`、`model`、`created_at`。

### 2.6 剧本（3 条）

| 方法 | 路径 | 状态码 | 说明 |
|------|------|--------|------|
| GET | `/api/episodes/{episode_id}/scripts` | 200 | 版本列表 `ScriptOut[]` |
| POST | `/api/episodes/{episode_id}/scripts` | 201 | 保存草稿（总是新建版本） |
| POST | `/api/episodes/{episode_id}/scripts/{script_id}/confirm` | 200 | 确认剧本并提交正式剧情状态 |

`ScriptSaveIn`：`content`(min_length=1)、`synopsis`(≤4000)、`source_context_manifest_id`(≤32)、`prompt_template_version`(≤64)、`model`(≤64)。

`ScriptOut`：`id`(ssv_)、`episode_id`、`version`、`content`、`synopsis`、`source_context_manifest_id`、`prompt_template_version`、`model`、`validation_result`、`continuity_result`、`status`、`created_at`、`created_by`。

**确认接口**关键约束：

`ScriptConfirmIn`：

```json
{
  "expected_series_state_version": 3,
  "review_note": "已人工核对道具持有人",
  "ignore_warnings": false
}
```

- `expected_series_state_version` 与 `series.state_version` 不一致 → `series_state_version_conflict`（乐观锁）。
- 存在 `severity=error` 的连续性阻断问题且未 `ignore_warnings` → `series_script_status_conflict`。
- 幂等键 `confirm:{episode_id}:{script_version_id}` 唯一；重复提交已确认版本 → 返回 `idempotent_replay: true`，不重复入账。
- events 取自 `script.validation_result["events"]`。

`ScriptConfirmOut`：`episode_id`、`script_version_id`、`script_status`、`series_state_version`、`snapshot_version`、`committed_event_count`、`issues`、`ignored_issue_ids`。

### 2.7 剧情事件与连续性（5 条）

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET | `/api/series/{series_id}/story-events` | `episode_id`、`fact_status`(≤16)、`limit`(1–500，默认 100) | 排序 `occurred_order ASC, created_at ASC` |
| GET | `/api/series/{series_id}/continuity` | - | 概览 |
| GET | `/api/series/{series_id}/continuity/snapshots` | `limit`(1–200，默认 50) | `version DESC` |
| POST | `/api/episodes/{episode_id}/continuity/check` | - | 执行检查并回写 `episode.continuity_status` |
| GET | `/api/episodes/{episode_id}/continuity/issues` | - | 无结果时返回 `{status:"not_checked", issues:[], script_version_id:null}` |

`ContinuityOverviewOut`：`state_version`、`latest_snapshot`、`open_plot_threads`（统计 `open`/`in_progress`）、`confirmed_event_count`、`episodes_with_conflict`（统计 `continuity_status=="conflict"`）。

`StoryEventOut`：`id`(sev_)、`series_id`、`episode_id`、`script_version_id`、`event_key`、`event_type`、`title`、`description`、`involved_asset_ids`、`location_asset_id`、`source_scene_id`、`source_text`、`fact_status`、`importance`、`occurred_order`、`created_at`、`created_by`。

`ContinuitySnapshotOut`：`id`(csn_)、`series_id`、`mainline_id`、`version`、`based_on_episode_id`、`based_on_script_version_id`、`state_json`、`state_hash`、`created_at`、`created_by`。

### 2.8 剧情线（3 条）

| 方法 | 路径 | 状态码 | 说明 |
|------|------|--------|------|
| GET | `/api/series/{series_id}/plot-threads` | 200 | query `status`（alias，≤16） |
| POST | `/api/series/{series_id}/plot-threads` | **201** | 请求体 `PlotThreadCreate` |
| PATCH | `/api/plot-threads/{thread_id}` | 200 | 请求体 `PlotThreadUpdate`，状态流转由用户显式操作 |

`PlotThreadOut`：`id`(plt_)、`series_id`、`title`、`description`、`introduced_episode_id`、`last_updated_episode_id`、`status`、`related_asset_ids`、`related_event_ids`、`resolution_condition`、`created_at`、`updated_at`。

### 2.9 资产登记（5 条）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/series/{series_id}/assets` | query `asset_type`；返回 `SeriesAssetOut[]` |
| POST | `/api/series/{series_id}/assets` | query `asset_id`(1–32)、`first_episode_id`(≤32)；**只建立系列关联，不复制记录、不改归属** |
| GET | `/api/series/{series_id}/assets/{asset_id}/usage` | 资产不存在 → `asset_not_found`(404) |
| GET | `/api/episodes/{episode_id}/assets` | 某集实际使用的资产版本 `EpisodeAssetRefOut[]`，含 `version_drift` |
| POST | `/api/episodes/{episode_id}/assets/resolve` | 名称匹配建议，返回 `candidates` + `note` |

`SeriesAssetOut` 字段：`id`、`series_id`、`asset_id`、`asset_name`、`asset_type`、`current_version`、`file_path`、`thumb_path`、`first_episode_id`、`created_at`。

`resolve` 说明：从 `validation_result` 的 `characters/scenes/props` 提取名称，最多 60 个；note 原文为「名称匹配仅为提示，同一角色必须由人工确认 Asset ID，禁止仅凭名称合并」。

## 3. 状态码汇总

| 状态码 | 场景 |
|--------|------|
| 200 | 查询、更新、确认、检查 |
| 201 | 创建系列、保存 Bible 新版本、创建剧集、保存剧本、创建剧情线 |
| 202 | 续写、重新生成 |
| 404 | `series_not_found`、`episode_not_found`、`asset_not_found`、`task_not_found` |
| 409 | `series_state_version_conflict`、`series_script_status_conflict`、`invalid_state_transition` |
| 422 | 请求体校验失败（FastAPI 默认结构） |

## 4. 与既有单集接口的边界

- 系列接口**不替换**任何既有 `/api/projects/{id}` 链路；剧集绑定 Project 后，生产仍在既有项目页完成。
- 系列接口**不写** `episode_continuity`（视频首尾帧连续性），该概念与剧情连续性完全独立。
- 前端调用顺序推荐：`context-preview` 确认上下文 → `episodes/continue` 拿 task_id → `GET /api/tasks/{task_id}` 轮询真实进度 → 进入审核页 → `confirm`。
