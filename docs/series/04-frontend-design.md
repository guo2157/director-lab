# 04 前端设计（系列化创作与续集管理）

> 事实来源：`frontend/src/router/index.ts`（111 行）、`frontend/src/components/AppLayout.vue`（302 行）、`frontend/src/api/series.ts`（201 行）、`frontend/src/api/tasks.ts`、`frontend/src/types/series.ts`（314 行）、`frontend/src/views/series/*.vue`。
> 技术栈未改动：Vue 3.5 + TypeScript + Vite 6 + naive-ui + Pinia + vue-router 4 + @tanstack/vue-query + axios。

## 1. 设计原则

1. **不动旧路由**：既有 10 条以 project 为中心的路由路径与名称全部保留，系列只做**新增**。
2. **渐进式接入**：系列页面是「编排层」，单集生产（剧本/评估/解析/分镜/资产/合成）仍由既有项目页承担。
3. **状态不混用**：剧本状态（`script_status`）、生产状态（`production_status`）、剧情连续性状态（`continuity_status`）在前端分开展示，不合并为单一进度。
4. **进度以后端为准**：任务进度只读 `GET /api/tasks/{task_id}` 的 `progress` / `current_step` / `elapsed_seconds`，前端不自行累加。
5. **写操作显式**：确认剧本、归档系列、剧情线状态流转都必须用户主动点击，不做自动提交。

## 2. 路由设计

### 2.1 新增路由（5 条）

| 路径 | name | 组件 | meta.title |
|------|------|------|-----------|
| `/series` | `series-list` | `views/series/SeriesListView.vue` | 系列 |
| `/series/create` | `series-create` | `views/series/SeriesCreateView.vue` | 新建系列 |
| `/series/:id` | `series-detail` | `views/series/SeriesDetailView.vue` | 系列详情 |
| `/series/:id/continue` | `series-continue` | `views/series/SeriesContinueView.vue` | 续写剧集 |
| `/episodes/:id/review` | `episode-review` | `views/series/EpisodeReviewView.vue` | 剧集审核 |

注册位置在 `router/index.ts` 的 `/jobs` 之前，`afterEach` 统一改写 `document.title` 的逻辑未改动。

### 2.2 既有路由（10 条，未改动）

`director`、`projects`、`script`、`estimate`、`parse`、`storyboard-images`、`storyboard`、`assets`、`compose`、`jobs`、`settings`。

### 2.3 路由参数约定

- `series-continue`：`params.id` = seriesId，可选 `query.episodeId`（从详情页某集进入时预置）。
- `episode-review`：`params.id` = episodeId。
- 跳转均由 `SeriesDetailView` 的 `goContinue()` / `goReview()` 发起，名称与参数严格匹配，未语义复用项目级 `id`。

## 3. 布局与导航改动

### 3.1 顶部导航新增「系列」

`topMenuOptions` 由 4 项扩为 5 项：导演台 / 项目 / **系列** / 任务中心 / 设置。

### 3.2 修复 `isProjectPage` 误判（关键）

原实现为 `Boolean(route.params.id)`，而系列路由同样带 `:id`，会误触发项目面包屑与 `ProjectProgressRail`。改为白名单判定：

```ts
const PROJECT_ROUTE_NAMES = [
  'script', 'estimate', 'parse',
  'storyboard-images', 'storyboard', 'assets', 'compose',
]
const isProjectPage = computed(
  () => Boolean(projectId.value)
    && PROJECT_ROUTE_NAMES.includes(String(route.name ?? '')),
)
```

效果：系列页面不再显示项目流程轨，项目页面行为完全不变。

### 3.3 导航高亮

```ts
const SERIES_ROUTE_NAMES = [
  'series-list', 'series-create', 'series-detail',
  'series-continue', 'episode-review',
]
const activeKey = computed(() => {
  const name = String(route.name ?? '')
  if (isProjectPage.value) return 'projects'
  if (SERIES_ROUTE_NAMES.includes(name)) return 'series-list'
  return name
})
```

即「项目页仍固定高亮项目」的规则保持不变，系列五个页面统一高亮「系列」。

## 4. 页面设计

### 4.1 SeriesListView（260 行）

- 能力：关键词搜索、状态过滤（active/archived）、分页、归档 / 恢复。
- 统计列全部取自后端派生字段（`episode_count`、`confirmed_script_count`、`completed_video_count`、`open_plot_thread_count`、`bible_version`），前端不自行计数。
- 覆盖空态、错误态、加载态，并提供重试。

### 4.2 SeriesCreateView（153 行）

- 表单：名称（必填、长度校验）、简介、题材、风格、目标受众、语言。
- 提交调用 `createSeries`，后端同时建立 Bible V1；成功后跳转详情页。

### 4.3 SeriesDetailView（496 行）

- 顶部概览：剧集数、已确认剧本数、已完成视频数、未回收剧情线、Bible 版本、剧情状态版本（`state_version`）。
- 5 个 Tab：剧集 / 设定（Bible）/ 连续性 / 剧情线 / 共享资产。
- 剧集 Tab：新建空剧集、进入续写、进入审核、进入单集生产（无 `project_id` 时提示未绑定）。
- 设定 Tab：JSON 文本域编辑 + 前端 `JSON.parse` 校验，非法内容提示「设定内容不是合法 JSON，请检查格式」，保存即生成新版本（旧版本保留）。
- 连续性 Tab：概览 + 快照历史 + 冲突剧集；剧情线 Tab：列表与状态流转；资产 Tab：共享资产列表与使用历史入口。

### 4.4 SeriesContinueView（388 行）

流程：填写续写要求 → **构建上下文预览**（`previewContext`，不落库）→ 确认后**开始续写**（`continueEpisode`，202）→ 轮询真实进度。

- 预览区展示 `series_bible`、`continuity`、`previous_episode`、`plot_threads`、`history_events`、`assets`、`warnings`、`missing`、`context_hash`。
- 有 `warnings` / `missing` 时高亮提示，不阻断提交。
- 任务区展示：`status`、`progress` 进度条、`current_step`、`current_message`、`error_msg`、`task_id`。
- 轮询实现：`setInterval` 2.5s 调 `getTask(taskId)`，进入终态（completed/failed/cancelled）自动停止；`onBeforeUnmount` 清理定时器，避免泄漏。
- 终态后提供「进入剧本审核」按钮，跳 `episode-review`。

### 4.5 EpisodeReviewView（482 行）

- 顶部：剧集号、标题、剧本状态、生产状态、连续性状态、剧情状态版本（乐观锁基准）。
- 剧本版本列表：点击切换查看，默认优先 `script_current_version_id`，其次 `awaiting_review`，再次最早版本。
- 连续性区：可手动触发 `checkContinuity`，展示冲突/警告计数与逐条问题（`severity`、`type`、`message`、`source_event_ids`、`suggestion`）。
- 确认区：审核备注、`ignoreWarnings` 勾选、确认按钮。
  - 提交携带 `expected_series_state_version`。
  - 409 / 版本冲突时提示「系列状态已变化，本次确认未生效」并自动刷新版本号，不覆盖他人结果。
  - 成功后展示 `script_status`、`series_state_version`、`snapshot_version`、`committed_event_count`、`ignored_issue_ids`。
  - 可确认状态限定为 `draft` / `analyzing` / `awaiting_review` / `rejected`，已确认版本不提供再次确认按钮。

## 5. 状态文案映射（三套独立枚举）

| 枚举 | 映射 |
|------|------|
| `SCRIPT_STATUS` | draft=草稿 / analyzing=分析中 / awaiting_review=待审核 / confirmed=已确认 / superseded=已被替代 / rejected=已驳回 |
| `PRODUCTION_STATUS` | not_started=未开始 / queued=排队中 / running=生产中 / reviewing=待审核 / completed=已完成 / failed=失败 / cancelled=已取消 |
| `EPISODE_CONTINUITY_STATUS` | not_checked=未检查 / checking=检查中 / consistent=一致 / warning=有警告 / conflict=有冲突 / needs_review=待人工确认 |
| `SEVERITY` | conflict=冲突 / warning=警告 / info=提示 |

## 6. API 与类型封装

### 6.1 `api/series.ts`（201 行）

覆盖 27 条系列接口，函数名与后端语义一一对应，例如：

`listSeries` / `getSeries` / `createSeries` / `updateSeries` / `archiveSeries` / `unarchiveSeries` / `getSeriesBible` / `saveSeriesBible` / `listSeriesBibleVersions` / `listEpisodes` / `createEpisode` / `getEpisode` / `getNextEpisodeNumber` / `getEpisodeWorkspace` / `continueEpisode` / `previewContext` / `getContextManifest` / `listScripts` / `saveScript` / `confirmScript` / `regenerateScript` / `listStoryEvents` / `getContinuityOverview` / `listContinuitySnapshots` / `checkContinuity` / `listContinuityIssues` / `listPlotThreads` / `createPlotThread` / `updatePlotThread` / `listSeriesAssets` / `registerSeriesAsset` / `listEpisodeAssets` / `resolveEpisodeAssets` / `getAssetUsage`。

### 6.2 `types/series.ts`（314 行）

与本项目 `backend/app/schemas/series.py` 字段严格对齐，包含 `Series`、`SeriesListItem`、`SeriesEpisode`、`SeriesBible`、`SeriesScript`、`ContinuityIssue`、`ContinuityCheckResult`、`ContinuitySnapshot`、`ContinuityOverview`、`PlotThread`、`ContextManifest`、`ContextPreview`、`ContinuePayload`、`ContinueResult`、`ScriptConfirmPayload`、`ScriptConfirmResult`、`EpisodeAssetRefView`、`SeriesAssetView` 及 5 个状态类型别名。

> 注意：剧集类型在前端命名为 `SeriesEpisode`（后端 schema 名为 `EpisodeOut`），审核页已按前者引用。

### 6.3 `api/tasks.ts` 增加 `getTask`

新增 `TaskDetail` 接口（继承 `TaskCardData`，追加 `project_id`、`conversation_id`、`message_id`、`current_step`、`current_message`、`started_at`、`updated_at`、`elapsed_seconds`）与 `getTask(taskId)`，指向 `GET /api/tasks/{task_id}`。

续写页据此显示后端真实进度，不依赖 SSE，避免刷新页面后进度丢失。

## 7. 与单集生产链路的兼容性

| 关注点 | 处理 |
|--------|------|
| 旧路由 | 10 条原样保留，路径与 name 均未改动 |
| 旧导航 | 四入口语义不变，仅追加一项 |
| 项目页行为 | 依赖的 `isProjectPage` 改为白名单后，项目级页面仍命中，流程轨与面包屑不变 |
| 生产页跳转 | 详情页「进入单集生产」仍跳 `script` 路由（`params.id` = projectId） |
| 数据写入 | 前端不直接写正式剧情状态，一律走确认接口 |

## 8. 构建验证

执行 `npx vue-tsc --noEmit -p tsconfig.json`：

- 退出码 `EXIT=0`
- 输出日志 0 行（`0 /tmp/tsc_series.log`）

即新增的 5 个页面、路由改动、AppLayout 改动与 `api/tasks.ts` 扩展均通过 TypeScript 类型检查，未引入类型错误。

## 9. 后续可选项（未实现，不属于本次交付）

1. 系列列表接入 vue-query 缓存与预取，减少详情页首屏等待。
2. 续写任务复用 `useTaskEvents` 的 SSE 通道，与轮询互为兜底。
3. 审核页增加剧本 diff 视图（当前仅整段展示）。
4. 剧情线拖拽排序与关联事件可视化。
