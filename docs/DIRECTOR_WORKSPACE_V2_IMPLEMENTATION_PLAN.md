# DIRECTOR_WORKSPACE_V2_IMPLEMENTATION_PLAN

> 文档性质：现状审计 + 架构设计 + 文件级实施计划
> 生成日期：2026-11-13
> 审计基线：`/home/ubuntu/director-lab`（git 工作区干净，无未提交改动）
> 本阶段约束：只做审计与方案设计，不改动业务代码、不执行数据库迁移、不覆盖配置

---

## 0. 结论摘要

从『带聊天功能的视频生成系统』升级为『Agent 驱动的可视化导演工作台』，缺口不在于 Agent 能力不足，而在于**缺少一层把 Agent 意图与真实业务页面连接起来的工作台基础设施**。

已确认可复用的基础设施：

- Agent 侧：28 个注册工具 + 10 个流程工具 + 5 个返工工具 + 完整审批门禁（PolicyEngine）+ 意图路由 + 结构化的会话上下文。
- 任务侧：统一的 Task / TaskStage / TaskEvent / TaskResult 编排层，事件为 outbox 语义、全局递增 seq、支持断线补齐。
- 前端侧：AppLayout 顶部导航与内容插槽、导演台三栏页面、SSE 任务事件消费 composable、Naive UI 设计令牌。

必须新增的能力（现有代码无法满足）：

1. 全局 Workspace 容器与 Agent 常驻面板（当前导演台与业务页面是两条互不相干的展示链路）。
2. 统一的导航服务与导航白名单（当前 Agent 没有任何导航工具，前端只有一处粗糙的 `goProject(page)` 跳转）。
3. 页面 → Agent 的动作回传通道（当前完全没有）。
4. Task 与集数、父任务的关联，以及完成时间、重试次数、工具名等字段（当前 Task 表缺）。
5. 执行计划持久化（`agent_plan_step` 表已存在但从未写入，属于空转表）。
6. 对象级并发控制（Asset 有 version 但更新路径未见校验，Shot 无 version 字段）。

数据库变更规模控制在最小：**新增 1 张表（workspace_event）、3 张表加列（task、shot、agent_plan_step 不需要新表）**。

实施分 6 个阶段，文件级改动约 40 项（新增约 18 项、修改约 22 项）。

---

## 1. 项目现状审计

### 1.1 技术栈与仓库结构

仓库根目录只有三块：`backend/`、`frontend/`、`docs/`，外加 `README.md` 与 `.gitignore`。

| 层 | 技术选型 | 版本 / 证据 |
|---|---|---|
| 前端框架 | Vue 3 + TypeScript | vue ^3.5.13，typescript ~5.6.2 |
| 构建 | Vite | ^6.0.5，`frontend/vite.config.ts` |
| UI | Naive UI（暗色主题） | ^2.40.1，`App.vue` 使用 darkTheme + themeOverrides |
| 状态 | Pinia + TanStack Vue Query | pinia ^2.3.0，@tanstack/vue-query ^5.62.0 |
| 路由 | vue-router | ^4.5.0，createWebHistory |
| HTTP | axios | ^1.7.9，统一封装 `src/api/client.ts` |
| 后端框架 | FastAPI + SQLAlchemy 2.0 | `backend/app/main.py`，`models/base.py` |
| 数据库 | SQLite + WAL | `backend/data/director_lab.db`（含 -wal / -shm） |
| 迁移 | Alembic | `backend/alembic/versions` 共 38 个版本文件 |
| 渲染底座 | ComfyUI 实例池 + ComfyKit 工作流 | `app/integrations/comfyui.py`、`comfyui_pool.py` |
| 后端代码量 | 约 43999 行 Python | `app/` 目录统计 |

`backend/app/workspace/` 目录存在但为空，说明此前有过工作台构想但未落地任何代码。

### 1.2 前端审计

#### 1.2.1 路由体系

`frontend/src/router/index.ts` 共 16 条路由，**全部扁平定义，没有任何嵌套结构**：

| 路径 | name | 组件 | 说明 |
|---|---|---|---|
| `/director` | director | DirectorConsoleView | 独立导演台，固定视口 |
| `/` | projects | ProjectListView | 作品列表 + 新建作品弹窗 |
| `/project/:id/script` | script | ScriptView | 剧本 |
| `/project/:id/estimate` | estimate | EstimateView | 剧本评估 |
| `/project/:id/parse` | parse | ParseView | 剧本解析 |
| `/project/:id/storyboard-images` | storyboard-images | StoryboardImagesView | 分镜图 |
| `/project/:id/storyboard` | storyboard | StoryboardView | 分镜看板 |
| `/project/:id/assets` | assets | AssetsView | 资产库 |
| `/project/:id/compose` | compose | ComposeView | 合成与导出 |
| `/series` | series-list | SeriesListView | 系列列表 |
| `/series/create` | series-create | SeriesCreateView | 新建系列 |
| `/series/:id` | series-detail | SeriesDetailView | 系列详情 |
| `/series/:id/continue` | series-continue | SeriesContinueView | 续写剧集 |
| `/episodes/:id/review` | episode-review | EpisodeReviewView | 剧集审核 |
| `/jobs` | jobs | JobsView | 任务中心 |
| `/settings` | settings | SettingsView | 设置 |

关键结论：

- 所有页面都是**顶层路由**，切换时 `router-view` 直接销毁上一个页面组件。
- 没有 `/workspace` 之类的容器路由，也没有父子路由关系。
- 除 `series-continue` 使用 `route.query.episodeId` 外，业务页面**只读 `route.params.id`**，不解析 tab、assetId、shotId、episode 等定位参数。

#### 1.2.2 全局 Layout

`frontend/src/components/AppLayout.vue`（300 行）是唯一全局布局，职责很薄：

- 顶部 header：品牌 + 四个一级导航（导演台 / 作品 / 任务中心 / 设置）。
- `PROJECT_ROUTE_NAMES` 白名单判断是否为项目内页面，命中时渲染面包屑与 `ProjectProgressRail` 侧栏。
- `SERIES_ROUTE_NAMES` 让系列页面统一高亮『作品』。
- `isFixedViewport` 只对 `director` 生效，此时内容区 `overflow: hidden`，由页面内部面板各自滚动。
- 内容区 `<slot />` 直接渲染 `router-view`，最大宽度 1680px，长度 `calc(100dvh - var(--app-header-height, 52px))`。

结论：`AppLayout` 具备改成『双栏工作台外壳』的最小基础，但当前它只是导航条 + 单栏插槽，**没有左右分栏能力、没有栏宽状态、没有 Agent 常驻位置**。

#### 1.2.3 业务页面清单与体量

| 页面文件 | 行数 | 现有职责 |
|---|---|---|
| `views/ProjectListView.vue` | 1326 | 作品/系列列表、新建作品弹窗（NModal，非独立页面）、归档、删除、清理 |
| `views/project/ScriptView.vue` | 530 | 剧本编辑、保存版本、触发解析 |
| `views/project/EstimateView.vue` | 228 | 剧本评估结果 |
| `views/project/ParseView.vue` | 828 | 解析进度、解析版本确认、跳转资产库 |
| `views/project/AssetsView.vue` | 1734 | 资产库：角色/场景/道具/音色四类、制作资产、上传、重生成、审核 |
| `views/project/StoryboardImagesView.vue` | 1749 | 分镜图看板、关键帧确认、重新生成 |
| `views/project/StoryboardView.vue` | 1957 | 分镜看板、按集加载镜头、视频生成、参考图槽位 |
| `views/project/ComposeView.vue` | 809 | 合成与导出两个阶段 |
| `views/JobsView.vue` | 466 | 任务中心 |
| `views/SettingsView.vue` | 997 | 系统设置 |
| `views/series/*.vue` | 153–496 | 系列创建/详情/续写/剧集审核 |
| `views/DirectorConsoleView.vue` | 1717 | 导演台三栏页面 |

可复用性判断：这些页面**业务逻辑完整、不需要重写**。改造点集中在『能否接受外部定位参数』与『能否上报用户动作』，属于增量改动，不触碰业务主流程。

#### 1.2.4 导演台页面现状

`views/DirectorConsoleView.vue`（1717 行）是三栏结构：

| 区域 | 内容 |
|---|---|
| 左栏 sessions-panel | 会话列表、搜索、新建、重命名、归档、移动端自动收起 |
| 中栏 chat-panel | 消息流、乐观发送、思考态提示、模型选择、任务卡片、资产缩略图引用、搜索结果记录 |
| 右栏 context-panel | 项目上下文、待审批申请（批准与执行分离）、审核卡片 |

关键实现细节：

- 任务卡片消费 `assistant.payload.task_card`，并通过 `useTaskEvents` 订阅 `/api/tasks/events` 做单卡片实时更新。
- 审批分两步：`resolveApproval` 只放行，`executeApproval` 才真正创建任务（后端 execute 幂等，复用已存在任务）。
- 页面级跳转只有一个函数：`goProject(page)`，拼接 `/project/${pid}/${page}`，**无法定位到具体集数、资产、镜头**。
- 该页面自带 `overflow` 布局，属于固定视口页面，与业务页面的滚动模型不同。

#### 1.2.5 状态管理现状

| 存储 | 当前内容 | 缺口 |
|---|---|---|
| Pinia `project` store | `currentProjectId`、`currentProject`、`hasProject` | 无当前集数、无当前页面/标签/焦点实体 |
| Pinia `preferences` store | 偏好设置 | 与工作台无关 |
| TanStack Query | 所有服务端数据（项目、资产、镜头、任务、进度） | 按 queryKey 缓存，页面组件卸载后缓存仍在，可作为页面状态恢复的部分基础 |
| 组件内 ref | 集数选择、当前资产分类、当前镜头等 | 页面卸载即丢失，无法跨路由恢复 |

结论：现有 Pinia 严格遵循『只存客户端状态』的设计原则（`stores/project.ts` 注释明确写出服务端数据一律走 TanStack Query）。这一原则应保留，新的 Workspace / Agent / Task 三态也必须遵守：**任务状态从后端读，不在前端自造**。

#### 1.2.6 实时机制现状

两套并存：

| 机制 | 位置 | 用途 | 特征 |
|---|---|---|---|
| SSE 任务事件 | `composables/useTaskEvents.ts` → `/api/tasks/events?last_seq=` | 任务卡片状态与进度 | `applyEvent` 仅接受 `seq > lastSeq`；`onopen` 触发 backfill；`onUnmounted` 自动断开 |
| 轮询业务进度 | `composables/useGenerationProgress.ts`，间隔 2500ms | 资产库、分镜图生成进度（`has_inflight` / `all_done`） | 有在途任务才轮询，全部结束自动停表，连续失败 3 次才提示 |

结论：任务粒度已经有可靠的事件机制，**业务页面粒度没有**。这正是『Agent 说生成中、左侧页面看不到变化』的根因之一：资产库/分镜图靠轮询，间隔最长 2.5 秒，且只在页面挂载时才启动。

#### 1.2.7 审计问题回答（对应需求 3.1 的十个问题）

1. 当前导演台在哪里？——`frontend/src/views/DirectorConsoleView.vue`，路由 `/director`。
2. 业务页面如何组织？——16 条扁平路由，`AppLayout` 单栏插槽渲染，无嵌套。
3. 是否已有全局 Layout？——有，`AppLayout.vue`，但只承担导航与单栏内容。
4. 是否可以直接增加双栏容器？——可以。`AppLayout` 的 `content-main` 是纯插槽，外层再加一层 Workspace 容器即可，不需要重写布局体系。
5. 哪些页面可以直接复用？——全部业务页面可复用，无需重写；只需补接受定位参数与上报动作的能力。
6. 哪些页面存在重复实现？——未发现整页级重复；存在局部重复（资产/分镜图各自实现生成进度轮询、多个页面各自拼装加载状态），可抽公共 composable，但不是本次改造的阻塞项。
7. 路由切换是否会卸载 Agent？——会。当前 Agent 在 `/director` 页面内，离开该路由即整体卸载，会话与任务卡片状态全部丢失（会话本身在数据库可恢复，但页面内状态不可）。
8. 页面状态目前保存在哪里？——组件内 `ref` + TanStack Query 缓存。切换路由后组件级状态（集数、分类、选中镜头）丢失。
9. 当前是否已经有实时事件机制？——有任务级 SSE，见 1.2.6；无工作区级事件。
10. 改造时哪些现有页面需要调整？——`AssetsView`、`StoryboardView`、`StoryboardImagesView`、`ScriptView`、`ParseView`、`ComposeView`、`ProjectListView`（弹窗唤起）需要增加定位参数支持；`DirectorConsoleView` 需要拆分为可复用 Agent 面板。

### 1.3 后端 Agent 审计

#### 1.3.1 后端模块分层

| 目录 | 内容 | 规模 |
|---|---|---|
| `app/api/v1/` | 23 个路由模块，全部在 `main.py` 注册 | agent.py 1905 行、generate.py 1573 行、regeneration.py 1025 行 |
| `app/services/` | 约 70 个服务模块 | agent_tool_impls.py 1749 行、prompt_compiler.py 1657 行、regen_workspace.py 1122 行 |
| `app/worker/` | executor / compose_executor / export_executor / parse_executor / series_executor / scheduler | executor.py 1687 行 |
| `app/models/` | 业务模型、task.py、agent.py | — |
| `app/integrations/` | comfyui / comfyui_pool / llm | — |
| `app/workspace/` | **空目录** | 0 文件 |

`app/workspace/` 为空这一事实很重要：说明工作台方向此前只停留在设想，**没有任何既有实现需要兼容**，本次可以从零设计而不担心冲突。

#### 1.3.2 Agent 执行链路（已核实的部分）

`POST /api/agent/sessions/{session_id}/chat` 是本轮对话的唯一入口，实际分支顺序如下：

1. 会话存在性与 `archived` 校验。
2. 持久化用户消息，`(session_id, seq)` 唯一索引冲突时回滚重取号，最多 3 次，超限返回 `seq_conflict`（503）。
3. **待确认事项优先解析** `_handle_pending_confirmation`：若存在有效 pending 确认事项，用户的『可以 / 不可以 / 退回 / 再改改 / 再看看』必须先按该事项解释，直接改业务状态并落一条 `role=assistant` 消息返回，不再进入后续分支。
4. **视频生成工作流** `_handle_video_workflow`：命中时直接返回，不进入意图路由（存在待确认事项时跳过此分支，避免重复追问）。
5. **联网搜索分支**：正则识别『搜索 / 搜一下 / 查一下』等前缀，走 `search_service.run_web_search`，失败或空结果如实反馈，不编造。
6. 意图路由 `intent_router.classify_intent` → `route_action`，分派到 pipeline_action / regen_action / generate_request / query / analysis / unsafe。
7. 流程动作 `_handle_pipeline_action`：状态机门禁 → 高风险工具只建审批申请 → 普通流程工具直接执行。
8. 主对话：LLM 多轮，历史窗口最近若干条 user/assistant，瞬态错误指数退避最多 3 次。

> 说明：`agent.py` 共 1905 行，本次精读了 chat 入口前段、`_handle_pipeline_action` 全段、审批 execute 分派、任务卡片下发点。其余上下文已核实：不存在独立的 `_handle_generate_request` 函数；`generate_request` 意图经 `route_action()` 直接返回 `NextAction.GUIDE` + 固定 `GENERATE_GUIDE_MESSAGE`，不进入主对话流程，是确定性规则分派而非 LLM 调用。

#### 1.3.3 三套工具执行器

| 执行器 | 文件 | 工具数 | 用途 |
|---|---|---|---|
| `execute_tool` | `services/agent_tools.py` | 28 | 查询类 + 生成申请类，统一策略门禁 |
| `execute_pipeline_tool` | `services/pipeline_tools.py` | 10 | 流程节点：create_project / save_script / analyze_script / review_assets / confirm_keyframes / compose_video / export_video / confirm_parse / query_parse_status / get_pipeline_status |
| `execute_regen_tool` | `services/regen_tools.py` | 5 | 返工：regenerate_asset / replace_asset / regenerate_shot / adopt_candidate / list_candidates |

`execute_tool` 的确定性门禁顺序（`agent_tools.py:678`）：

```
policy gate (evaluate_tool_policy) → preconditions → input_model 校验 → 执行
```

`requires_approval=True` 且 `approved=False` 时直接返回 `approval_required`，**不执行、无副作用**。这保证审批是后端强制，不依赖 Prompt 约束。

#### 1.3.4 意图到工具的映射范围（关键限制）

```python
INTENT_TOOL_MAPPING = {
    "query": list(_READ_ONLY_TOOLS),
    "analysis": list(_READ_ONLY_TOOLS),
    "generate_request": list(_READ_ONLY_TOOLS),  # 定位失败时回落主对话兜底
}
```

即：**通用工具循环只开放只读工具**。所有写操作必须经过 pipeline_action 或 regen_action 分支，且生成类工具强制走审批。这是一个很好的安全边界，本次改造必须保持，不能为了方便导航而把写工具塞进通用循环。

#### 1.3.5 审批与确认机制

| 概念 | 表 | 用途 |
|---|---|---|
| 工具审批 | `agent_confirmation.kind=tool_approval` | 高风险工具执行前置门禁，`task_id` 指向承载意图的 AgentTask |
| 业务审核 | `agent_confirmation.kind=artifact_review` | 剧本/资产/关键帧/视频人工审核，`target_type` + `target_id` + `target_version` + `round` |

状态集合：`pending / approved / rejected / revision_requested / deferred / expired / cancelled`。
幂等键：`idempotency_key`（会话 + 项目 + 对象 + 版本 + 轮次 + 类型）。

前端两阶段 API：`resolve` 只做裁决，`execute` 才创建任务；execute 内部幂等复用已存在任务。

#### 1.3.6 任务编排层

`services/task_orchestrator.py`（663 行）提供：

| 函数 | 职责 |
|---|---|
| `create_task` | 建 Task + 写 `task.created` 事件；按 `agent_task_id` 幂等 |
| `start_task` | queued/pending → running，记录 `started_at` |
| `add_stage` | 建 TaskStage，支持 `depends_on` 与 `agent_name` |
| `schedule_ready_stages` | 按依赖门禁调度可启动阶段 |
| `start_stage` / `update_stage_progress` / `finish_stage` | 阶段生命周期 |
| `set_task_step` | 写 `current_step` / `current_message` / `progress` 并落 `task.step` 事件 |
| `complete_task` / `fail_task` / `cancel_task` | 终态流转 |
| `bridge_job_event` | 把 Job 事件桥接到关联 Task 并刷新阶段 |
| `bridge_approval_resolved` | 审批裁决事件桥接 |
| `task_payload` | 任务卡片数据，`elapsed_seconds` 由 `started_at` 推导 |
| `reconcile_orphan_tasks` | 启动时对账孤儿任务 |
| `_aggregate_progress` | 任务进度 = 各阶段进度均值 |

状态迁移表在 `_TASK_TRANSITIONS` 中定义，`start_task` 会显式校验目标状态可达性。

#### 1.3.7 事件系统

| 端点 | 数据源 | 语义 |
|---|---|---|
| `GET /api/events`、`/api/events/backfill` | `job_event` | Job 级事件 |
| `GET /api/tasks/events`、`/api/tasks/events/backfill` | `task_event` | Task 级事件 |

共同实现特征（`api/v1/events.py`）：

- `event_seq` 自增主键，全局递增、跨项目连续。
- 重连按 `seq` **严格大于** 判定，天然幂等。
- 轮询间隔 1.0 秒，单连接最长 300 秒，期间发 `: ping` 心跳。
- 响应头 `Cache-Control: no-cache`、`X-Accel-Buffering: no`。
- 使用独立 `SessionLocal()`，不占用请求级会话。
- **不发送 `event:` 名称行**，事件类型放在 `data.event_type`，前端只用 `onmessage`。
- backfill 单次 `limit` 1000，SSE 单批 `limit` 200。

结论：任务级事件可靠性已达到生产水准，本次不需要重建，只需**在同一机制上扩展工作区事件类型**。

#### 1.3.8 会话上下文与记忆

`services/context_manager.py` 已经把上下文结构化了：

| 结构 | 字段 |
|---|---|
| `FocusContext` | `entity_type`（character / scene / prop / shot）、`entity_id`、`entity_name`、`updated_seq` |
| `OperationContext` | `operation`、`entity_type`、`entity_id`、`last_result`、`last_error`、`updated_seq` |
| `ExecutionContext` | `intent`、`subcategory`、`confidence`、`confidence_tier`、`target`、`stage`、`pending_confirmation_id` |

持久化在 `agent_session.context_state` JSON，切换项目时 `clear_on_project_switch` 清空 focus/operation 防污染。

**重要发现**：`FocusContext` 只支持 character / scene / prop / shot 四类实体，**没有 page / tab / episode 概念**。Agent 当前『关注什么实体』是知道的，『用户在看哪个页面』是不知道的。这正是双向联动缺失的直接证据。

#### 1.3.9 审计问题回答（对应需求 3.2 的十个问题）

1. **Agent 如何识别意图？** `intent_router.classify_intent`：规则快路径 `apply_fast_path_rules` 先行，未命中走 LLM，再做置信度分档（execute / recheck / clarify），最终 `route_action` 得出 NextAction。
2. **Agent 如何选择 Tool？** 按意图查 `INTENT_TOOL_MAPPING` 白名单；写操作不由 LLM 自由选择，而由 `pipeline_action` / `regen_action` 结构化字段驱动，在 `_handle_pipeline_action` 中查注册表。
3. **Tool 如何调用业务 API？** 直接调用 service 层与 ORM，不走 HTTP 自调用。三个执行器统一做策略门禁与参数校验。
4. **子 Agent 如何汇报结果？** 没有独立汇报协议。子 Agent 通过 `TaskStage.agent_name` 标识，结果写入 `TaskStage.result` 与 `TaskResult`，进度由 `bridge_job_event` 从 Job 事件桥接。
5. **父 Agent 如何汇总？** `_aggregate_progress` 取阶段进度均值；`complete_task` / `fail_task` 写终态；`parent_agent.aggregate_task_results` 已核实（`app/services/parent_agent.py:106`）：`incomplete = unfinished > 0`，其中 `unfinished` 统计 `stage.status IN ("pending", "running")` 的阶段数；任何未达终态的阶段都会使 incomplete=True。聚合结果以 `result_type="aggregate"` 的 `TaskResult` 幂等写入（存在则更新 data）。
6. **任务进度如何持久化？** `Task.progress` + `TaskStage.progress` + `task_event`。进度由确定性代码写入，LLM 不得生成。
7. **是否支持任务恢复？** 读侧完整支持：`GET /api/tasks`、`GET /api/tasks/{id}`、SSE backfill。写侧靠 `reconcile_orphan_tasks` 对账。**不支持断点续跑外部生成任务**，符合需求第七章第 12 条约束。
8. **是否支持取消任务？** 支持。`POST /api/tasks/{task_id}/cancel`，终态任务重复取消返回当前状态。
9. **是否支持多任务并行？** 支持。任务各有独立 `task_id`，底层 Job 由 ComfyUI 实例池控制在『单实例串行』前提下并行。
10. **如何映射到左侧业务页面？** **完全没有映射**。无导航工具、无页面事件、无 `current_page` 上报，是整个需求最大的能力缺口。

### 1.4 数据库审计

#### 1.4.1 现有表结构概览

Alembic 版本文件 38 个，业务表按域划分：

| 域 | 表 |
|---|---|
| 内容层 | `project`、`script_version`、`script_estimate`、`parse_version`、`episode_parse_record`、`episode_continuity` |
| 实体层 | `character`、`scene`、`scene_episode`、`prop` |
| 分镜层 | `shot`、`asset_ref`、`dependency_ref` |
| 产物层 | `asset`、`asset_plan`、`asset_plan_dependency`、`composition`、`export`、`upload_record` |
| 执行层 | `job`、`job_event`、`job_input_ref`、`workflow` |
| 编排层 | `task`、`task_stage`、`task_event`、`task_result` |
| Agent 层 | `agent_session`、`agent_message`、`agent_task`、`agent_plan_step`、`agent_confirmation`、`agent_memory` |

#### 1.4.2 编排层缺口（对应需求第四章 TaskState 与第七章）

| 需求字段 | `task` 表现状 | 结论 |
|---|---|---|
| `task_id` | 有 | 满足 |
| `parent_task_id` | **无** | 需新增列 |
| `project_id` | 有 | 满足 |
| `episode_id` | **无** | 需新增列 |
| `task_type` | 有 | 满足 |
| `task_status` | 有，取值 queued / pending / running / completed / failed / cancelled | 缺 `waiting_approval` / `paused` / `retrying` |
| `progress` | 有 | 满足 |
| `current_stage` | 有 `current_step` + `current_message` | 满足 |
| `result` | 独立 `task_result` 表 | 满足 |
| `error` | 有 `error_code` + `error_msg` | 满足 |
| `started_at` | 有 | 满足 |
| `completed_at` | **无**（靠 `updated_at` 近似） | 建议新增，语义更准 |
| `tool_name` | **无** | 建议新增，便于按工具聚合与回放 |
| `input_snapshot` | **无** | 建议新增，重试与审计需要 |
| `retry_count` | **无** | 需新增 |
| `conversation_id` | 有 `session_id` | 满足（命名差异，不强制改名） |

#### 1.4.3 并发控制缺口（对应需求第八章）

| 对象 | 现状 | 结论 |
|---|---|---|
| `asset` | 有 `version`（**产物版本号**，每条记录一个版本，靠 `parent_id` 串链）、`updated_at`、`locked`、`validity` | `version` 不是乐观锁版本；需用 `updated_at` 比较或新增 `revision` |
| `shot` | 无业务版本列，仅 `TimestampMixin` 提供 `updated_at` | 需新增 `revision` |
| `character` / `scene` / `prop` | 已核实：三表均在 `app/models/narrative.py`，均由 `TimestampMixin` 提供 `updated_at`；可编辑提示词字段为 `character.description/traits/appearance`、`scene.description/time_note/plate_text`、`prop.description/appearance`。更新接口（`PATCH /characters/{id}` 等）无版本校验，直接 `setattr` + `commit` | Phase 0 已核实 |
| 写作接口 | `assets.py` 未见 `PATCH`，写入走 upload / replace / regenerate / rollback / lock | 需逐接口补 `If-Match` 语义 |

结论：**不做全库加版本列**，只在真正会被 Agent 与用户同时编辑的对象上加 `revision`（`shot`、`asset` 的提示词类字段），其余对象用 `updated_at` 比较即可。

#### 1.4.4 空转表与重复数据

- `agent_plan_step` **表已建但从未写入**。全仓引用只有 `project_purge.py` 的级联删除与 `models/__init__.py` 的导出。这意味着『执行计划』目前只存在于 LLM 的回复文本里，**没有持久化的计划结构**，页面刷新即丢。
- `Task` / `AgentTask` / `Job` 三层并存：`AgentTask` 是意图快照，`Task` 是编排单元，`Job` 是执行单元。三者是刻意分层，**不算重复数据**，但必须保持 `agent_task_id` 与 `TaskStage.job_id` 两条关联不断裂。`bridge_job_event` 与 `reconcile_orphan_tasks` 已在做这件事。
- 未发现同一业务事实存在两份互相竞争的表。

#### 1.4.5 事件续传能力评估

| 能力 | 现状 |
|---|---|
| 事件唯一标识 | `task_event.event_seq` 自增主键、`job_event.event_seq` 同构 |
| 全局顺序 | 有，跨项目连续 |
| 重复事件幂等 | 前端 `seq > lastSeq` 判定，天然去重 |
| 断线补齐 | backfill 接口，`limit` 1000，按 seq 升序 |
| 事件与业务状态同事务 | 是，`write_event` 只 `flush` 不 `commit`，由调用方事务提交 |
| 事件恢复 | 支持 |
| 多标签页 | 每个标签独立 EventSource，各自维护 lastSeq，互不干扰 |
| 工作区级事件 | **无** |

结论：任务事件的可靠性已经达标。缺口是**没有工作区维度的事件**（导航、焦点、用户动作），这部分需要新增，但应复用同一套 outbox 语义，而不是另造一套。

---

## 2. 改造目标

### 2.1 目标形态

把『导演台聊天页 + 若干独立业务页』改造成『双栏可视化工作台』：左侧是**真实业务页面**（原封不动的现有组件），右侧是**常驻 Agent 面板**。

```
┌──────────────────────────────────────────────────────────────────┐
│ AppLayout Header：Director Lab | 导演台 作品 任务中心 设置        │
├──────────────────────────────────┬───────────────────────────────┤
│ WorkspaceContainer (68%~75%)     │ DirectorAgentPanel (25%~32%)  │
│                                  │                               │
│  ┌─ ProjectProgressRail ─────┐   │  ┌─ AgentHeader ───────────┐  │
│  │ 制作进度                  │   │  │ 会话切换 / 模型 / 收起   │  │
│  │                           │   │  ├─ AgentChat ─────────────┤  │
│  └───────────────────────────┘   │  │ 对话 + 计划 + 进度        │  │
│  ┌─ WorkspaceRouter ─────────┐   │  │ （统一时间线）            │  │
│  │ <router-view />           │   │  ├─ AgentApproval ─────────┤  │
│  │  真实业务页面             │   │  │ 待审批 / 待审核           │  │
│  │  ProjectPages             │   │  ├─ AgentInput ────────────┤  │
│  │  ScriptPages              │   │  │ 输入框 + 发送             │  │
│  │  AssetPages               │   │  └───────────────────────────┘  │
│  │  StoryboardPages          │   │                               │
│  │  VideoPages / AudioPages  │   │                               │
│  │  ExportPages              │   │                               │
│  └───────────────────────────┘   │                               │
└──────────────────────────────────┴───────────────────────────────┘
         ↑ 复用现有业务组件，不改业务逻辑          ↑ 从 DirectorConsoleView 拆出
```

### 2.2 设计原则（与现有架构约束对齐）

1. **不重写业务页面**。所有页面组件保持现状，只做『接受定位参数』与『上报动作』两件增量改动。
2. **不新建状态管理库**。继续用 Pinia + TanStack Query，新 Store 遵守既有原则：客户端状态进 Pinia，服务端数据走 Query。
3. **不重建事件系统**。工作区事件复用 `task_event` 的 outbox 语义与 SSE 通道形态，不引入 WebSocket（现有项目没有 WebSocket，引入等于重造）。
4. **导航与业务执行严格分离**。导航成功 ≠ 业务成功，二者用不同事件类型、不同状态位。
5. **Agent 不能构造 URL**。Agent 只提交结构化导航意图，由前端 Navigation Service 做白名单校验后执行。
6. **不改变审批门槛**。资产审核、关键帧确认、合成导出这些既有闸口一个都不能松。
7. **进度只能来自真实数据**。禁止任何前端模拟百分比或预设阶段。

### 2.3 三种状态彻底分离

| 状态 | 归属 | 存储位置 | 生命周期 |
|---|---|---|---|
| **WorkspaceState** | 用户当前在看什么 | Pinia `workspace` store + localStorage 兜底 | 跨路由常驻，刷新可恢复 |
| **AgentState** | Agent 这轮在做什么 | Pinia `agent` store（客户端视图） + `agent_session.context_state`（服务端权威） | Agent 面板生命期，跨路由常驻 |
| **TaskState** | 后台任务真实进展 | 后端 `task` / `task_stage` / `task_event` | 与页面无关，后端权威 |

关键约束：

- 切换页面 → 只改 WorkspaceState，不触碰 TaskState。
- 切换作品 → 只改 WorkspaceState 的 `current_project_id`，**不改变任何已运行 Task 的 `project_id`**。
- 切换集数 → 同理，不改变 Task 的 `episode_no`。
- Agent 可同时关注多个任务，但 `agent_session.context_state.execution` 只记录**当前对话正在聚焦的那一个**。

---

## 3. 新架构图

### 3.1 前端分层

```
main.ts
  └─ App.vue（NConfigProvider / 暗色主题 / NMessage / NDialog）
      └─ AppLayout.vue（保留：Header + 一级导航 + 面包屑）
          └─ DirectorWorkspaceLayout.vue          【新增】双栏外壳，常驻
              ├─ WorkspaceSplitPanel.vue          【新增】可拖拽分栏 + 折叠 + 全屏 + 移动端切换
              │   ├─ WorkspaceContainer.vue       【新增】左侧视口容器
              │   │   └─ <router-view />          （现有业务页面原样渲染）
              │   └─ DirectorAgentPanel.vue       【新增】右侧 Agent 面板
              │       ├─ AgentHeader.vue           【新增】会话 / 模型 / 折叠
              │       ├─ AgentTimeline.vue          【新增】对话 + 计划 + 进度统一时间线
              │       │   ├─ AgentMessageItem.vue    【新增】消息气泡（复用现有渲染逻辑）
              │       │   ├─ AgentPlanCard.vue       【新增】执行计划（消费 agent_plan_step）
              │       │   └─ TaskCard.vue             【已有，直接复用】
              │       ├─ AgentApproval.vue          【新增】待审批 / 待审核（复用现有审批逻辑）
              │       └─ AgentInput.vue             【新增】输入框
              └─（移动端：抽屉形态，同一面板组件）
```

### 3.2 后端分层

```
app/api/v1/
  ├─ agent.py                【修改】chat 增加 workspace_nav 输出；新增 plan 查询
  ├─ workspace.py            【新增】导航执行、页面上下文上报、工作区事件流
  ├─ tasks.py                【修改】列表增加 episode / parent 过滤
  └─ events.py               【修改】新增 /api/workspace/events（复用现有 outbox 实现）

app/services/
  ├─ workspace_nav.py        【新增】导航白名单 + 目标存在性校验 + 导航意图构建
  ├─ workspace_event.py      【新增】工作区事件写入与查询（复用 outbox 语义）
  ├─ workspace_context.py    【新增】页面上下文 → 会话上下文同步（含防环）
  ├─ agent_tools.py          【修改】注册 navigate_workspace 工具（低风险、无副作用）
  ├─ context_manager.py      【修改】FocusContext 扩展 page / tab / episode_no
  └─ task_orchestrator.py    【修改】create_task 支持 episode_no / parent_task_id / tool_name

app/models/
  ├─ workspace.py            【新增】WorkspaceEvent
  ├─ task.py                 【修改】Task 增列
  ├─ narrative.py            【修改】Shot 增 revision
  └─ production.py           【修改】Asset 增 revision
```

### 3.3 数据流

```
用户输入 ──► /api/agent/sessions/{id}/chat
                 │
                 ├─► 意图路由 ─► pipeline_action ─► 审批 / 执行
                 │                                    │
                 │                                    └─► create_task ─► task_event
                 │
                 ├─► 导航意图（新增）──► workspace_event(nav_requested)
                 │                          │
                 │                          └─► SSE /api/workspace/events
                 │                                    │
                 │                          Navigation Service ─► router.push
                 │                                    │
                 │                          workspace_event(nav_completed)
                 │
                 └─► Agent 回复（含 task_card / plan_steps）

用户在左侧页面操作 ──► 业务 API 成功 ──► POST /api/workspace/page-context
                                                  │
                                                  └─► agent_session.context_state 更新
                                                      + workspace_event(user_action)
```

---

## 4. 前端改造方案

### 4.1 路由改造：保持 URL 兼容

**不改变任何现有 URL**。新增一条工作台容器路由，把现有页面作为子路由挂进去：

```ts
// router/index.ts 新增
export const WORKSPACE_ROUTE_NAMES = [
  'director', 'projects', 'script', 'estimate', 'parse',
  'storyboard-images', 'storyboard', 'assets', 'compose',
  'series-list', 'series-create', 'series-detail', 'series-continue',
  'episode-review', 'jobs',
]
```

实现方式二选一，推荐 **方案 A**：

- **方案 A（推荐，改动最小）**：保留现有扁平路由，在 `App.vue` 中用 `DirectorWorkspaceLayout` 包住 `<router-view />`，由布局自己判断当前路由是否属于工作区白名单。属于则双栏，不属于（如 `/settings`）则单栏直通。
  - 优点：`router/index.ts` 一行不改，URL 100% 兼容，`<router-view />` 位置不变，业务页面零感知。
  - 风险：布局需要正确处理『Agent 面板在路由切换时不卸载』——这一点通过把 Agent 面板放在 `<router-view />` **同级**（而非其内部）天然满足。

- **方案 B**：加嵌套路由 `/workspace/project/:id/...`，与旧路由并存并做重定向。
  - 缺点：URL 变长、需要维护两套路由、`route.name` 冲突，收益不明显。

**结论：采用方案 A。**

### 4.2 DirectorWorkspaceLayout

职责（**只做布局，不含业务**）：

1. 判断当前路由是否在工作区白名单内。
2. 工作区内：渲染 `WorkspaceSplitPanel`（左：`<router-view />`；右：`DirectorAgentPanel`）。
3. 工作区外：渲染原样 `<router-view />`，不留白。
4. 保持 `<router-view />` 的组件实例在切换 `projectId` 时正常重建（Vue 默认行为），但 **Agent 面板实例不重建**。

关键实现点：

```vue
<template>
  <div v-if="inWorkspace" class="director-workspace">
    <WorkspaceSplitPanel v-model:left-ratio="leftRatio" v-model:collapsed="agentCollapsed">
      <template #left>
        <div class="workspace-left">
          <ProjectProgressRail v-if="projectId" :project-id="projectId" />
          <div class="workspace-page">
            <router-view />          <!-- 业务页面，切换时正常卸载重建 -->
          </div>
        </div>
      </template>
      <template #right>
        <DirectorAgentPanel />     <!-- 常驻，不受 router-view 影响 -->
      </template>
    </WorkspaceSplitPanel>
  </div>
  <router-view v-else />
</template>
```

注意：`ProjectProgressRail` 目前在 `AppLayout` 里渲染。迁移到 Workspace 左栏后，需从 `AppLayout` 移除，避免重复渲染（这是唯一需要动 `AppLayout` 的地方）。

### 4.3 WorkspaceSplitPanel

功能清单与实现约束：

| 功能 | 实现 |
|---|---|
| 左右栏宽度 | `leftRatio`（默认 0.71），拖拽分隔条调整，范围 0.5–0.85 |
| 折叠 / 展开 Agent | `agentCollapsed`，折叠时左栏 100%，右侧留 36px 竖向标签条 |
| 独立滚动 | 左右各自 `overflow-y: auto`，外层 `height: 100%; overflow: hidden` |
| 全屏左侧 | `leftFullscreen` 时右栏 `display: none`，提供悬浮退出按钮 |
| 移动端 | `≤1024px` 改为上下堆叠或抽屉；用 `NDrawer` 承载 Agent，底部 Tab 切换『业务/Agent』 |
| 宽度持久化 | `localStorage['workspace:layout']`，store 初始化时读取 |

拖拽实现不要引第三方库，直接用 `pointerdown/pointermove/pointerup` + `setPointerCapture`，20 行左右。

### 4.4 导航服务（前端）

**唯一允许改路由的地方**。所有 Agent 导航、页面间跳转都走它。

```ts
// src/services/workspaceNavigation.ts  【新增】
export interface NavigateRequest {
  projectId?: string
  episodeNo?: number
  page: WorkspacePage           // 白名单枚举
  tab?: string
  entityType?: 'character' | 'scene' | 'prop' | 'shot' | 'asset' | 'composition' | 'export'
  entityId?: string
  focus?: 'edit' | 'review' | 'result'
  reason?: string               // 展示给用户的导航理由
}

export async function navigateWorkspace(req: NavigateRequest): Promise<NavResult>
```

内部流程（**必须按顺序**）：

1. **白名单校验**：`page` 必须命中 `PAGE_ROUTES` 表，否则返回 `nav_invalid_page`。
2. **参数校验**：需要 `projectId` 的页面必须有值；`episodeNo` 为正整数。
3. **未保存保护**：读 `workspace.pendingGuard`（由业务页面注册），若有未保存内容弹 `NDialog` 让用户选择『保存并跳转 / 放弃并跳转 / 取消』。
4. **执行跳转**：`router.push({ name, params, query })`，定位参数统一走 `query`（不污染现有 `params`）。
5. **上报结果**：`POST /api/workspace/page-context`，带 `nav_result`（success / failed / blocked），让后端知道导航是否真的落地。
6. **失败反馈**：捕获 `router` 错误，回传 `nav_failed`，在 Agent 面板显示一条系统提示，**不谎报成功**。

页面映射表（**按实际路由名**）：

| Agent 动作 | `page` | 路由 name | 定位参数 |
|---|---|---|---|
| 查看作品 / 新增作品 | `project-list` | `projects` | `?create=1` 打开弹窗 |
| 编辑作品 / 查看作品详情 | `project-script` | `script` | `projectId` |
| 校验剧本 / 解析剧本 | `project-parse` | `parse` | `projectId` |
| 剧本评估 | `project-estimate` | `estimate` | `projectId` |
| 生成角色 | `project-assets` | `assets` | `projectId`, `tab=character` |
| 生成场景 | `project-assets` | `assets` | `projectId`, `tab=scene` |
| 生成道具 | `project-assets` | `assets` | `projectId`, `tab=prop` |
| 审核资产 | `project-assets` | `assets` | `projectId`, `tab=<类别>`, `entityId` |
| 生成分镜 | `project-storyboard` | `storyboard` | `projectId`, `episode` |
| 生成分镜图 | `project-storyboard-images` | `storyboard-images` | `projectId`, `episode`, `shotId` |
| 修改分镜提示词 | `project-storyboard` | `storyboard` | `projectId`, `episode`, `shotId`, `focus=edit` |
| 生成分镜视频 | `project-storyboard` | `storyboard` | `projectId`, `episode`, `shotId`, `focus=video` |
| 生成 TTS / 音频 | `project-assets` | `assets` | `projectId`, `tab=voice`, `entityId` |
| 视频合成 | `project-compose` | `compose` | `projectId`, `stage=compose` |
| 视频导出 | `project-compose` | `compose` | `projectId`, `stage=export` |
| 查询任务 | `jobs` | `jobs` | `?projectId=&taskId=` |

注意：现有 `ComposeView` 是『合成与导出』一个页面两个阶段（业务上不可合并，但 UI 上本来就同页），因此用 `stage` query 定位到对应区块，**不拆页面**。

### 4.5 业务页面改造清单（增量，不重写）

每个页面只做两件事：

**A. 接受定位参数**——在 `onMounted` 与相关 `watch` 中读取 `route.query`：

| 页面 | 需支持的 query | 具体改动 |
|---|---|---|
| `ProjectListView.vue` | `create=1` | 挂载时若 `create=1`，自动 `openCreateModal()`（已有函数） |
| `AssetsView.vue` | `tab`、`entityId` | `activeCategory.value = tab`；`entityId` 用于滚动定位并高亮对应资产卡片 |
| `StoryboardView.vue` | `episode`、`shotId`、`focus` | 已有 `allEpisodes` 与集数 chip，直接 `activeEpisode` 赋值；`shotId` 定位到 `activeShot`；`focus=edit` 打开提示词编辑区 |
| `StoryboardImagesView.vue` | `episode`、`shotId` | 同上，定位到分镜图卡片 |
| `ScriptView.vue` | `focus=edit` | 聚焦编辑器（可选，优先级低） |
| `ComposeView.vue` | `stage` | 滚动到合成区或导出区（可选） |
| `ParseView.vue` | 无 | 不需要改动 |
| `EstimateView.vue` | 无 | 不需要改动 |

**B. 上报用户动作**——只在业务 API **成功返回后**调用：

```ts
// src/services/workspaceContextReporter.ts  【新增】
reportUserAction({
  action: 'asset_prompt_updated' | 'asset_uploaded' | 'asset_deleted'
        | 'shot_prompt_updated' | 'shot_regenerated' | 'asset_approved'
        | 'asset_rejected' | 'keyframe_confirmed',
  projectId, episodeNo?, entityType, entityId, revision,
})
```

实现约束（对应需求 5.2 的七条）：

1. 只在 `mutateAsync` 的 `onSuccess` 或 `try` 成功分支调用，失败不上报。
2. 上报内容以**后端返回的业务数据**为准（如 `asset.revision`、`shot.revision`），不自己编。
3. Agent 后续执行任务时必须重新读库（现有工具天然如此，`execute_tool` 每次都是真实查询）。
4. 上报携带 `context_version`（后端返回的单调递增整数），Agent 侧过期则丢弃。
5. 上报**只更新上下文，不触发任何生成**，从根上避免事件环。
6. 同一 `(action, entity_type, entity_id, revision)` 在前端做 1 秒去重。
7. 页面不直接调 Agent 内部函数，只走 HTTP。

**上报接口**：`POST /api/workspace/page-context`（幂等，见第 9 节）。

### 4.6 前端 Store 设计

```ts
// src/stores/workspace.ts  【新增】
interface WorkspaceState {
  currentProjectId: string | null
  currentProjectName: string | null
  currentEpisodeNo: number | null
  currentPage: WorkspacePage | null      // 与 route.name 同步
  currentTab: string | null
  currentEntityId: string | null
  currentFocus: string | null
  leftRatio: number                      // 0.5–0.85
  agentCollapsed: boolean
  pendingGuard: { dirty: boolean; message: string } | null
  contextVersion: number                 // 后端返回的上下文版本
}

// src/stores/agent.ts  【新增】
interface AgentState {
  activeSessionId: string | null
  sessions: AgentSession[]
  messages: AgentMessage[]
  planSteps: AgentPlanStep[]              // 消费 agent_plan_step
  approvals: AgentApproval[]
  reviewCards: ReviewCard[]
  pendingNav: NavigateRequest | null      // 待执行的导航意图
  connectionStatus: 'connected' | 'reconnecting' | 'offline'
  lastTaskSeq: number
}
```

**不新增 task store**：任务快照继续由 `useTaskEvents` 的 `taskSnapshots` 管理，需要全局共享时把它提升为 `useTaskEvents` 单例（`src/composables/useTaskEvents.ts` 内部改为模块级单例，一处在 `DirectorWorkspaceLayout` 建立连接，各页面订阅）。

注意：现有 `stores/project.ts` 与新的 `workspace` store 职责重叠。方案是**保留 `project` store 不动**（业务页面在用），`workspace` store 通过 `watch` 同步 `currentProjectId`，避免改动业务页面。

---

## 5. 后端改造方案

### 5.1 总体思路

不新增执行层能力，只在既有三套工具执行器、任务编排层与事件系统之上补三件事：

1. **导航意图通道**：Agent 可以『请求导航』，但不能自己改路由。
2. **页面上下文通道**：前端可以上报用户动作，后端把它落到会话上下文。
3. **工作区事件流**：把导航事件与用户动作事件纳入可回溯的事件序列。

### 5.2 为什么不能只复用现有能力

| 需求 | 现有能力 | 缺口原因 |
|---|---|---|
| Agent 自动切到资产库角色 Tab | 无 | `TOOL_REGISTRY` 无导航工具；`INTENT_TOOL_MAPPING` 只读工具不带 UI 语义 |
| 页面定位到具体镜头 | 无 | 业务页面只读 `route.params.id`，无 query 解析 |
| Agent 感知用户改了提示词 | 部分 | `context_manager.OperationContext` 只能由 Agent 自己的工具调用写入，页面操作不经过它 |
| 刷新后恢复执行计划 | 无 | `agent_plan_step` 表已建但从未写入 |
| 多任务跨作品并行时正确归属 | 部分 | `Task` 有 `project_id`，**无 `episode_id`**，集级归属靠 `TaskStage.result` 兜底，不可靠 |

### 5.3 导航服务（后端）

`app/services/workspace_nav.py` 【新增】

```python
NAV_PAGE_WHITELIST = {
    "project-list", "project-script", "project-estimate", "project-parse",
    "project-assets", "project-storyboard", "project-storyboard-images",
    "project-compose", "jobs", "series-detail", "episode-review",
}

NAV_PAGE_ROUTES = {
    "project-list":           {"route_name": "projects",   "needs_project": False},
    "project-script":         {"route_name": "script",             "needs_project": True},
    "project-estimate":       {"route_name": "estimate",           "needs_project": True},
    "project-parse":          {"route_name": "parse",              "needs_project": True},
    "project-assets":         {"route_name": "assets",             "needs_project": True},
    "project-storyboard":     {"route_name": "storyboard",         "needs_project": True},
    "project-storyboard-images": {"route_name": "storyboard-images", "needs_project": True},
    "project-compose":        {"route_name": "compose",            "needs_project": True},
    "jobs":                   {"route_name": "jobs",               "needs_project": False},
}

def build_nav_request(db, *, project_id, page, tab=None, entity_type=None,
                      entity_id=None, episode_no=None, focus=None, reason=None) -> dict:
    """校验并构建导航请求。失败抛 NavError(code, message)。

    校验顺序：
    1. page 在白名单内
    2. needs_project 时 project_id 必填且项目存在
    3. episode_no 若给出必须为正整数
    4. entity_id 若给出必须存在且属于该项目
       - character/scene/prop -> 对应实体表
       - shot -> Shot + 校验所属项目（经 Scene.project_id）
       - asset -> Asset + project_id 匹配
    5. tab 在该 page 的合法取值内
    """
```

设计要点：

- **白名单是硬编码常量**，不从请求值拼接，杜绝任意 URL 构造。
- **实体存在性校验查真实数据库**，不信任 Agent 给的 ID。
- 校验失败返回结构化错误码（`nav_invalid_page` / `nav_project_not_found` / `nav_entity_not_found` / `nav_entity_project_mismatch`），Agent 据此如实告知用户，不得假装跳转成功。
- 导航请求**不落业务表**，只作为 `workspace_event` 的 payload 与 chat 响应的一部分返回。

### 5.4 页面上下文同步（后端）

`app/services/workspace_context.py` 【新增】

```python
def apply_page_context(db, session: AgentSession, payload: PageContextIn) -> dict:
    """把页面上报的上下文落到 agent_session.context_state。

    规则：
    1. 只接受白名单 action（asset_prompt_updated / asset_uploaded /
       asset_deleted / shot_prompt_updated / shot_regenerated /
       asset_approved / asset_rejected / keyframe_confirmed / page_viewed）
    2. 实体必须存在且属于 project_id；否则拒绝，不写上下文
    3. 更新 focus（entity_type / entity_id / entity_name）
    4. 更新 operation（operation / last_result=success / updated_seq）
    5. context_version += 1（单调递增，存 context_state["version"]）
    6. 写 workspace_event(type=workspace.user_action)
    7. 不触发任何生成，不修改业务表
    """
```

防环设计（对应需求 5.2 第 6 条）：

- 页面动作 → 只更新上下文 + 写事件，**绝不**回写触发 Agent 动作。
- Agent 导航 → 前端执行 → 上报 `nav_result`，这条上报**不更新 operation**（因为不是业务操作），只更新 `focus` 与 `current_page`。
- 前端对 `(action, entity_type, entity_id, revision)` 做 1 秒去重。
- 后端对同一 `idempotency_key`（session + action + entity + revision）做 60 秒幂等窗口。

### 5.5 工作区事件存储

不新建独立事件总线服务，直接新增一张表 `workspace_event`，与 `task_event` 同构：

```python
class WorkspaceEvent(Base):
    """工作区事件 outbox：导航意图、导航结果、用户动作、页面焦点。

    - event_seq 全局自增，与 task_event 相同的断线重连语义
    - 只记录『工作台交互事实』，不记录业务状态（业务状态仍在业务表）
    - session_id 必填：工作区事件永远归属于一个 Agent 会话
    """
    __tablename__ = "workspace_event"
    event_seq: int         # 自增主键
    session_id: str        # 必填
    project_id: str | None
    conversation_seq: int  # 会话内递增，便于按会话回放
    event_type: str        # workspace.nav_requested / nav_completed / nav_failed /
                           # workspace.focus_changed / workspace.user_action
    payload: dict | None
    created_at: datetime
```

索引：`ix_workspace_event_session_seq (session_id, conversation_seq)`、`ix_workspace_event_project_type (project_id, event_type)`。

为什么复用 outbox 结构而不是另建消息队列：

- 现有 `task_event` 的 SSE 消费方式已经很成熟，前端 `useTaskEvents` 的 seq 比较逻辑可以原样复用到 `useWorkspaceEvents`。
- SQLite 单写、业务规模是单机自托管，不需要引入 Redis/Kafka。
- 事件量级小（每次导航/动作 1 条），单表足够。

### 5.6 任务归属增强

`Task` 需要补 `episode_no`，否则多集并行时无法判定结果归属哪一集。

`create_task` 签名扩展（保持向后兼容，全部新参数有默认值）：

```python
def create_task(
    db, *, session_id, task_type, title,
    agent_task_id=None, project_id=None, message_id=None,
    episode_no: int | None = None,        # 新增
    parent_task_id: str | None = None,    # 新增
    tool_name: str | None = None,         # 新增
    input_snapshot: dict | None = None,   # 新增
) -> Task:
```

调用方（`pipeline_tools.tool_analyze_script`、`search_service`、`series/continue_service`）需补传 `episode_no`；不传则视为整项目级任务。

---

## 6. Agent Orchestrator 改造方案

### 6.1 现状定位

严格说，本项目没有独立的『Orchestrator 类』，编排逻辑分散在：

| 文件 | 承担的编排职责 |
|---|---|
| `api/v1/agent.py` `chat()` | 对话主流程：待确认解析 → 视频工作流 → 搜索 → 意图路由 → 流程动作 → 主对话 |
| `services/intent_router.py` | 意图分类与动作路由 |
| `services/agent_governance.py` | PolicyEngine + ToolBudget + NextAction / TerminalStatus |
| `services/task_orchestrator.py` | 任务与阶段生命周期 |
| `services/parent_agent.py` | AgentTask 创建、状态流转、结果聚合 |

本次改造**不重构这个结构**，只在 `chat()` 增加一条导航分支，并在 `parent_agent` 补齐计划持久化。

### 6.2 chat 流程新增分支

在『9 主对话』之前插入导航意图处理：

```
chat()
 ├─ ... 既有 1~7 步不变 ...
 ├─ 8【新增】导航意图处理
 │    intent.nav_request = {page, tab, entity_type, entity_id, episode_no, focus}
 │    ↓
 │    若存在 nav_request：
 │      nav_req = workspace_nav.build_nav_request(...)
 │      ├─ 校验失败 → assistant 消息 content 说明无法跳转原因，payload.nav_error
 │      └─ 校验通过 →
 │           workspace_event.write(workspace.nav_requested, nav_req)
 │           assistant 消息 payload.nav_request = nav_req
 │           （导航执行由前端完成，后端不阻塞对话）
 └─ 9 主对话（原逻辑）
```

关键约束：

- 导航请求**可以是纯导航**（用户说『带我看看角色库』），也可以是**业务动作的附带副作用**（Agent 决定生成角色资产时顺便把页面切到角色 Tab）。
- 导航与业务执行**分别记录**：`nav_requested` 事件与 `task.created` 事件是两条独立事实，前端 UI 必须分别显示，不能合并成一条『已完成』。
- 若业务工具执行失败但导航成功，Agent 回复必须说明『页面已切换，但生成任务提交失败』，不得混淆。

### 6.3 执行计划持久化

`agent_plan_step` 表已存在，本次真正启用：

| 时机 | 写入内容 |
|---|---|
| Agent 生成多步计划（如『分析剧本 → 生成角色 → 生成场景 → 生成道具』） | 每个步骤写一条 `AgentPlanStep`，`step_type=tool_call`，`tool_name` 填对应工具，`status=pending` |
| 步骤开始执行 | `status=running` |
| 步骤成功 / 失败 | `status=succeeded` / `failed`，`result` 写错误码或产物摘要 |
| 步骤被跳过（前置失败） | `status=skipped` |

查询接口：`GET /api/agent/sessions/{session_id}/plans?task_id=`，返回按 `step_index` 排序的步骤列表。

前端 `AgentPlanCard.vue` 消费该接口，刷新后仍然能看到计划。这解决了需求 2.3 的『执行计划』与场景 6 的『刷新后 Agent 会话恢复』。

最小实现边界（避免过度设计）：

- **只为 pipeline_action 类多步请求建计划**（例如『生成第一集的角色、场景和道具』）。
- 单步请求（『生成第一集分镜图』）不建计划，直接用 TaskStage 展示，避免重复展示。
- 计划步骤与 TaskStage 的关系：`AgentPlanStep` 是**用户视角的计划**，`TaskStage` 是**执行视角的阶段**；两者可能存在一对多（一个计划步骤派生多个阶段）。不在表上加外键关联，靠 `tool_name` + `created_at` 邻近度展示，保持解耦。

### 6.4 子 Agent 汇总增强

现状：`TaskStage.agent_name` 标识子 Agent，`_aggregate_progress` 取均值。

补充（按需求 7.9 / 7.10）：

1. **失败不吞**：任一阶段 `failed` 时，父任务若继续执行剩余阶段，`complete_task` 的 `summary` 必须显式列出失败阶段，`TaskResult.result_type` 用 `partial`。
2. **部分失败可重试**：`POST /api/tasks/{task_id}/stages/{stage_id}/retry` 【新增】，只重置该阶段与其下游依赖阶段，已成功阶段不动。
3. **不伪装续跑**：外部生成任务（ComfyUI）不支持断点续跑，重试即重建 Job，需在 `TaskStage.result` 中明确 `resumed=false`。

---

## 7. Tool Registry 改造方案

### 7.1 新增工具：navigate_workspace

| 项 | 值 |
|---|---|
| 工具名 | `navigate_workspace` |
| 描述 | 把左侧业务工作区导航到指定页面与实体位置（只切页面，不执行任何业务操作） |
| permission | `read` |
| risk_level | `low` |
| side_effect | `False`（不改业务数据） |
| idempotent | `True` |
| requires_approval | `False` |
| timeout_seconds | 10 |
| max_retries | 0 |
| 入参 | `project_id`（可选）、`page`（必填，白名单）、`tab`（可选）、`entity_type`（可选）、`entity_id`（可选）、`episode_no`（可选，正整数）、`focus`（可选）、`reason`（可选，≤200 字） |
| 返回 | `{ok, nav_request}` 或 `{ok: False, error_code, error_message}` |

实现要点：

- 工具本身**不执行导航**，只返回结构化的 `nav_request`；实际跳转由前端完成。这样工具是纯函数，天然幂等、天然无副作用，也不会因为前端不可用而失败。
- 工具内部复用 `workspace_nav.build_nav_request` 做校验。
- **不加入** `_READ_ONLY_TOOLS`。复核后修正：本项目不存在 LLM 工具调用循环（详见 24.1 坑 1），只加白名单没有任何代码会主动调用它。实际接入方式改为在 `pipeline_action` 下新增 `navigate_workspace` 动作键，由 `_handle_pipeline_action` 分派，从而绕过 `ToolBudget` 预算上限与只读工具预取机制。

### 7.2 风险等级复核

按需求第九章的划分，对照现有注册表：

| 风险 | 现有工具 | 是否已符合 |
|---|---|---|
| 低：查询 / 导航 | 所有 `permission=read` 工具 + 新增 `navigate_workspace` | 符合 |
| 中：创建 / 修改提示词 / 生成资产 | `request_generate`、`batch_request_generate`、`request_style_update`、`create_project`、`save_script`、`review_assets`、`confirm_keyframes` | 符合 |
| 高：删除 / 覆盖 / 批量重置 | `run_generate`、`run_style_update`、`compose_video`、`export_video`、`regenerate_asset`、`replace_asset`、`regenerate_shot`、`adopt_candidate` | 符合（均 `requires_approval=True`） |

**未发现需要降级或升级的工具**。本次改造只需保证导航工具被正确归入低风险，不改变其他工具等级。

需要补的缺口（需求第九章第 7 条『后端必须执行最终权限检查』）：

- 现有 `execute` 接口已做 `_confirmation_in_session` 校验（确认审批属于该会话）。
- 新增导航**不涉及**审批绕过：导航工具 `requires_approval=False`，但它不能修改任何业务数据，因此不存在越权风险。
- 建议补一条断言：`workspace_nav.build_nav_request` 拒绝 `page=project-*` 且 `project_id` 与 `session.project_id` 不一致的请求（除非显式传 `allow_cross_project=True`，用于「去任务中心看另一个项目的任务」这类场景）。

### 7.3 工具文档同步

`docs/API.md` §12 与 `docs/agent/` 下的工具注册表说明需补 `navigate_workspace` 条目。本次 Plan 不含文档改动，列入 Phase 5 交付项。

---

## 8. Workspace Event Bus 设计

### 8.1 事件类型定义

沿用需求第六章的建议，按本项目命名规范（全小写 + 点号分层 + 过去式/进行时统一）收敛为：

| 类别 | 事件类型 | 触发点 | payload 关键字段 |
|---|---|---|---|
| 工作区 | `workspace.nav_requested` | Agent 提交导航意图 | `page`、`tab`、`entity_type`、`entity_id`、`episode_no`、`focus`、`reason` |
| 工作区 | `workspace.nav_completed` | 前端导航成功 | `page`、`route_name`、`route_full_path` |
| 工作区 | `workspace.nav_failed` | 前端导航失败或被未保存保护阻断 | `page`、`error_code`、`error_message` |
| 工作区 | `workspace.focus_changed` | 用户切换页面 / Tab / 集数 / 选中实体 | `page`、`tab`、`episode_no`、`entity_type`、`entity_id` |
| 工作区 | `workspace.user_action` | 业务操作成功 | `action`、`entity_type`、`entity_id`、`revision` |
| Agent | `agent.plan.created` | 计划创建 | `task_id`、`steps[]` |
| Agent | `agent.plan.updated` | 计划步骤状态变化 | `task_id`、`step_index`、`status` |
| Agent | `agent.approval.required` | 需人工审批 | `confirmation_id`、`tool_name`、`risk_level` |
| 任务 | `task.*` | **全部复用现有 `task_event`**，不重复定义 | — |
| 业务 | `asset.*` / `storyboard.*` / `video.*` / `audio.*` / `export.*` | **不新增**，由 `task_event` + 业务查询覆盖 | — |

**关键裁剪**：需求第六章列出了 `asset.created`、`storyboard.image.generated`、`video.generated` 等业务事件，但本项目已有 `job_event` 与 `task_event` 完整覆盖这些事实，再建一套业务事件等于双重真相来源。因此**明确不建**，页面刷新时通过业务查询（TanStack Query）获取最新数据即可。

### 8.2 事件 Envelope

```json
{
  "event_seq": 1024,
  "event_type": "workspace.nav_requested",
  "schema_version": 1,
  "created_at": "2026-11-13T10:30:00+08:00",
  "session_id": "ags_xxx",
  "conversation_seq": 37,
  "project_id": "prj_xxx",
  "task_id": null,
  "payload": { "page": "project-assets", "tab": "character", "episode_no": 1 }
}
```

字段说明：

- `event_seq`：复用自增主键，全局唯一且递增，作为 `Last-Event-ID` 语义载体。
- `schema_version`：写成常量 1，未来不兼容变更时递增，前端按版本分支解析。
- `conversation_seq`：会话内递增，便于按会话回放；由 `workspace_event` 写入时计算 `max(conversation_seq)+1`（与 `agent_message.seq` 相同的取号策略，含唯一索引冲突重试）。
- `task_id`：导航事件为空，未来若计划事件走同一通道则填值。

### 8.3 去重与顺序

| 场景 | 处理 |
|---|---|
| 同一事件重复推送 | 前端 `event_seq > lastSeq` 才应用，天然去重（与 `useTaskEvents` 一致） |
| 乱序到达 | 同上，丢弃低序号事件，等待 backfill 补齐 |
| 丢事件 | 重连时先 backfill，再查任务快照 `GET /api/tasks/{id}` |
| 多标签页 | 每个标签独立 EventSource 与 lastSeq，互不影响 |
| 页面刷新 | lastSeq 从 `sessionStorage` 恢复；无记录则从 0 开始并 backfill |

**重要**：`lastSeq` 不存 localStorage，避免长期失效的旧 seq 造成大量无意义 backfill。用 `sessionStorage` 按会话键隔离（`workspace:lastSeq:{sessionId}`）。

### 8.4 与现有 task_event 的关系

两条事件流**并行存在、互不替代**：

```
/api/tasks/events          → 任务状态与进度（权威，已有）
/api/workspace/events      → 工作台交互（新增）
```

前端同时消费两条流，但**展示位置不同**：任务卡片在对话流里，导航状态在页面顶部提示条里。

---

## 9. SSE / WebSocket 设计

### 9.1 选型结论：继续用 SSE

| 候选 | 结论 | 理由 |
|---|---|---|
| 复用现有 SSE | **采用** | 项目已有两套 SSE（job / task），实现成熟，前端有现成消费 composable，运维无额外依赖 |
| 引入 WebSocket | 不采用 | 需新增连接管理、心跳、重连、反向代理配置；现有需求无客户端主动推送场景 |
| 轮询 | 仅作降级 | SSE 连续失败时降级为 3 秒轮询 `GET /api/workspace/events?since_seq=` |

### 9.2 新端点

```
GET /api/workspace/events?session_id=xxx&last_seq=0
GET /api/workspace/events/backfill?session_id=xxx&updated_since_seq=0&limit=500
```

实现**完全复用** `api/v1/events.py` 现有 SSE 生成器模式：

- 轮询间隔 1.0 秒，单连接最长 300 秒后主动结束（前端 EventSource 会自动重连）。
- 发 `: ping` 心跳，避免中间代理超时断连。
- 响应头 `Cache-Control: no-cache`、`X-Accel-Buffering: no`。
- 独立 `SessionLocal()`，不占用请求上下文。
- 不发送 `event:` 名称行，事件类型放 `data.event_type`。

### 9.3 前端消费

`src/composables/useWorkspaceEvents.ts` 【新增】，结构与 `useTaskEvents.ts` 保持一致：

```ts
const { events, lastSeq, connected, connect, disconnect, backfill } = useWorkspaceEvents()

// 内部逻辑：
// 1. EventSource(`/api/workspace/events?session_id=${sid}&last_seq=${lastSeq}`)
// 2. onmessage: JSON.parse → 若 e.event_seq > lastSeq 则应用并更新 lastSeq
// 3. onopen: 先调 backfill 补齐断线期间事件
// 4. onerror: EventSource 自动重连；连续失败 5 次降级为轮询
```

### 9.4 与页面状态恢复的配合

页面刷新后的恢复顺序（**必须按序**）：

1. 从 `sessionStorage` 读 `workspace:state`（当前项目、集数、页面、Tab、实体）。
2. 从 `sessionStorage` 读 `workspace:lastSeq:{sessionId}`。
3. `router.replace` 到恢复的页面（不产生历史记录）。
4. 调 `GET /api/agent/sessions/{id}` 拉会话，`GET /api/agent/sessions/{id}/messages?after_seq=` 增量拉消息。
5. 调 `GET /api/agent/sessions/{id}/plans` 拉执行计划。
6. 调 `GET /api/agent/sessions/{id}/approvals` 与 `/reviews` 拉待办。
7. 建立 SSE 连接，`last_seq` 用第 2 步的值。
8. 对每个仍在运行的任务调 `GET /api/tasks/{id}` 拉快照，用快照覆盖本地缓存。

任何一步失败都不能阻塞后续步骤，失败项在 Agent 面板显示『部分状态未恢复』提示。

---

## 10. 数据库变更方案

### 10.1 变更原则

1. **能加列就不建表**。现有 4 张编排表结构完整，只缺少数几个字段。
2. **必须写 Alembic 迁移且提供 downgrade**（AGENTS.md P0 第 4 条）。
3. **加列必须有默认值或可空**，避免 SQLite 大表重建失败。
4. **不动已有列语义**。`task.status` 的 `pending` 历史值继续按 `queued` 等价处理，不在迁移中改写数据。

### 10.2 变更清单（共 1 张新表 + 3 张表加列）

#### 10.2.1 新增表 `workspace_event`

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `event_seq` | Integer | PK, autoincrement | 全局递增，断线恢复锚点 |
| `session_id` | String(32) | NOT NULL, index | 所属 Agent 会话 |
| `project_id` | String(32) | NULL, index | 所属项目（可空） |
| `conversation_seq` | Integer | NOT NULL, default 0 | 会话内递增 |
| `event_type` | String(48) | NOT NULL | `workspace.*` |
| `payload` | JSON | NULL | 事件载荷 |
| `created_at` | DateTime | NOT NULL | 创建时间 |

索引：

- `ix_workspace_event_session_seq (session_id, conversation_seq)`
- `ix_workspace_event_project_type (project_id, event_type)`

迁移文件建议命名：`a1c4e7f2b8d9_workspace_event_table.py`。

#### 10.2.2 `task` 表加列

| 列 | 类型 | 默认 | 说明 | 必要性 |
|---|---|---|---|---|
| `episode_no` | Integer | NULL | 任务归属集数 | **必须**，多集并行归属判定 |
| `parent_task_id` | String(32) | NULL | 父任务 ID | **必须**，子任务汇总 |
| `tool_name` | String(64) | NULL | 发起任务的工具名 | 建议，审计与回放 |
| `input_snapshot` | JSON | NULL | 入参快照 | 建议，重试与幂等 |
| `retry_count` | Integer | 0 | 重试次数 | **必须**，需求 7.6 |
| `completed_at` | DateTime | NULL | 完成时间 | 建议，语义比 `updated_at` 精确 |

不加的字段及理由：

- `conversation_id`：`session_id` 已承担同一语义，重命名会波及 10+ 处代码，收益不抵成本。
- `task_status` 枚举扩展（`waiting_approval` / `paused` / `retrying`）：**暂不扩展**。现有 `queued → running → completed/failed/cancelled` 已覆盖真实执行语义；审批等待由 `agent_confirmation.status=pending` 表达，暂停能力当前无真实需求（ComfyUI 排队任务无法真正暂停）。需求第七章的 9 个状态中，`waiting_approval` 与 `paused` 属于『计划态』而非『执行态』，强行加进 `task.status` 会造成两套真相。若后续确有暂停需求，再单独评估。

#### 10.2.3 `character` / `scene` / `prop` 三张实体表加列（复核后修正）

| 表 | 列 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `character` | `revision` | Integer | 1 | 编辑修订号，`description` / `traits` / `appearance` 更新时递增 |
| `scene` | `revision` | Integer | 1 | 编辑修订号，`description` / `time_note` / `plate_text` 更新时递增 |
| `prop` | `revision` | Integer | 1 | 编辑修订号，`description` / `appearance` 更新时递增 |

**修正说明**：初版曾计划给 `shot` 与 `asset` 加 `revision`，复核后确认找错了对象。

- `shot`：分镜图页编辑提示词写的是 `storyboard_regen_config`（该表已有 `version`），不是 `shot.image_prompt`，详见 24.2 坑 6。
- `asset`：该表没有任何提示词字段，写入路径全是「生成新版本」（`version` + `parent_id` 链），不存在原地覆盖问题，详见 24.2 坑 7。
- 真正会被用户与 Agent 同时编辑的是实体表的描述类字段：`character.description` / `traits` / `appearance`、`scene.description` / `time_note` / `plate_text`、`prop.description` / `appearance`。它们对应 `PATCH /characters/{id}` 一类接口，当前没有任何版本校验。

字段名用 `revision` 而非 `version`：`version` 在 `asset` 表已有「产物版本号」语义，混用会违反 AGENTS.md P0 第 6 条「三套枚举禁止混用」的精神。

#### 10.2.4 `storyboard_regen_config` 的并发控制（不新增列）

该表已有 `version` 字段（`models/regen_config.py:55`）。只需在写入接口上加 `If-Match` 语义：请求带 `expected_version`，与库中 `version` 不符则返回 409 `revision_conflict`。**不需要新增任何列。**

### 10.3 是否需要迁移的判断

| 需求条目 | 是否必须迁移 | 说明 |
|---|---|---|
| 工作区事件 | 是 | 新表 |
| 任务集数归属 | 是 | 加列 |
| 父子任务 | 是 | 加列 |
| 重试计数 | 是 | 加列 |
| 对象版本冲突 | 是 | 加列 |
| 执行计划持久化 | **否** | `agent_plan_step` 表已存在，只需写入 |
| 审批机制 | **否** | `agent_confirmation` 表已完整 |
| 任务结果 | **否** | `task_result` 表已完整 |
| 事件续传 | **否** | `task_event` / `job_event` 已具备 |
| 会话上下文 | **否** | `context_state` JSON 可自由扩展字段 |

**结论：只在 `workspace_event` 上建表，其余全部加列。迁移文件 1 个，包含建表 + 3 表加列，可一次执行。**

### 10.4 迁移与回滚

```python
# upgrade()
op.create_table('workspace_event', ...)
op.create_index('ix_workspace_event_session_seq', ...)
op.add_column('task', sa.Column('episode_no', sa.Integer(), nullable=True))
op.add_column('task', sa.Column('parent_task_id', sa.String(32), nullable=True))
op.add_column('task', sa.Column('tool_name', sa.String(64), nullable=True))
op.add_column('task', sa.Column('input_snapshot', sa.JSON(), nullable=True))
op.add_column('task', sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'))
op.add_column('task', sa.Column('completed_at', sa.DateTime(), nullable=True))
op.add_column('shot', sa.Column('revision', sa.Integer(), nullable=False, server_default='1'))
op.add_column('asset', sa.Column('revision', sa.Integer(), nullable=False, server_default='1'))

# downgrade()：逐条 drop_column + drop_table（SQLite batch_alter_table）
```

SQLite 注意事项：

- 加列用 `server_default` 确保已有行有值，避免 `NOT NULL` 失败。
- `downgrade` 必须用 `op.batch_alter_table` 包裹，否则 SQLite 会报错。
- 迁移前备份 `backend/data/director_lab.db`（现有 `data/backups/` 目录已有备份习惯）。

---

## 11. API 接口清单

### 11.1 新增接口

#### 11.1.1 POST /api/workspace/page-context

| 项 | 内容 |
|---|---|
| 用途 | 页面上报用户动作与焦点变化，同步到会话上下文 |
| 方法 | POST |
| 请求体 | `session_id`（必填）、`project_id`（可选）、`page`（必填）、`tab`（可选）、`episode_no`（可选）、`entity_type`（可选）、`entity_id`（可选）、`action`（必填，白名单）、`revision`（可选）、`context_version`（可选） |
| 响应 | `{ok, context_version, accepted}` |
| 权限 | 会话存在且未归档；`entity_id` 必须属于 `project_id` |
| 错误码 | `session_not_found`(404)、`session_archived`(409)、`invalid_action`(422)、`entity_not_found`(404)、`entity_project_mismatch`(409) |
| 幂等 | **是**。键 = `session_id + action + entity_type + entity_id + revision`，60 秒窗口内重复返回 `accepted=false` |
| SSE 通知 | 写 `workspace.user_action` / `workspace.focus_changed` 事件（不推送给页面自己，只给其他标签页与 Agent 面板） |

#### 11.1.2 GET /api/workspace/events

| 项 | 内容 |
|---|---|
| 用途 | 工作区事件 SSE 流 |
| 方法 | GET |
| 查询参数 | `session_id`（必填）、`last_seq`（默认 0，≥0） |
| 响应 | `text/event-stream`，每行 `data: {json}` |
| 权限 | 会话存在 |
| 幂等 | 是（只读） |

#### 11.1.3 GET /api/workspace/events/backfill

| 项 | 内容 |
|---|---|
| 用途 | 断线补齐工作区事件 |
| 方法 | GET |
| 查询参数 | `session_id`（必填）、`updated_since_seq`（必填，≥0）、`limit`（默认 500，上限 1000） |
| 响应 | `{items: [...], last_seq}` |
| 幂等 | 是 |

#### 11.1.4 GET /api/agent/sessions/{session_id}/plans

| 项 | 内容 |
|---|---|
| 用途 | 查询执行计划步骤 |
| 方法 | GET |
| 查询参数 | `task_id`（可选；不传返回该会话最近一次计划） |
| 响应 | `{task_id, steps: [{step_index, step_type, tool_name, args, status, result}]}` |
| 权限 | 会话存在且未归档 |
| 幂等 | 是 |

#### 11.1.5 POST /api/tasks/{task_id}/stages/{stage_id}/retry

| 项 | 内容 |
|---|---|
| 用途 | 重试单个失败阶段，不重跑已成功阶段 |
| 方法 | POST |
| 请求体 | 无 |
| 响应 | `{ok, task_id, stage_id, status, resumed}` |
| 权限 | 阶段状态必须是 `failed`；任务未取消 |
| 错误码 | `task_not_found`(404)、`stage_not_found`(404)、`stage_not_retryable`(409) |
| 幂等 | **是**。同阶段重复调用返回当前状态，不重复建 Job |
| SSE 通知 | 现有 `task_event` 已覆盖，不需要新增 |

`resumed` 字段语义：外部生成任务固定返回 `false`（不支持断点续跑），用于前端如实展示。

### 11.2 修改接口

| 接口 | 改动 |
|---|---|
| `POST /api/agent/sessions/{id}/chat` | 响应 `assistant_message.payload` 增加可选 `nav_request`、`plan_task_id`；chat 内部增加导航分支与计划写入 |
| `GET /api/tasks` | 增加 `episode_no`、`parent_task_id` 过滤参数；返回值补 `episode_no` / `retry_count` / `completed_at` |
| `GET /api/tasks/{id}` | 返回值补上述新字段 |
| `GET /api/agent/sessions/{id}` | 返回值补 `context_state.version` |
| `POST /api/agent/sessions/{id}/messages` | 无改动 |

### 11.3 不新增的接口及理由

| 需求可能暗示的接口 | 结论 | 理由 |
|---|---|---|
| `POST /api/workspace/navigate` | **不建** | 导航由前端 Navigation Service 执行，后端只提供『校验后的导航意图』。若建此接口，意味着后端要操作前端路由，架构倒置 |
| `GET /api/workspace/state` | **不建** | 工作区状态是纯客户端状态（当前在看哪一页），不需要服务端持久化到数据库；刷新恢复用 `sessionStorage` 即可 |
| `POST /api/workspace/tasks/{id}/pause` | **不建** | ComfyUI 队列任务无法真正暂停，建接口等于承诺做不到的事 |
| `GET /api/assets/stream` 等业务 SSE | **不建** | 业务数据变化已由 `job_event` + `task_event` 覆盖，再建一套会造成双重真相 |

---

## 12. 页面联动方案

### 12.1 Agent 驱动页面（单向，第一阶段）

完整时序（以『生成第一集角色资产』为例）：

```
1. 用户：帮我生成第一集的角色
2. 后端 chat()
   ├─ 意图路由命中 generate_request，target=character，episode=1
   ├─ workspace_nav.build_nav_request(
   │     page="project-assets", tab="character", episode_no=1, reason="生成角色资产")
   │   → 校验通过，返回 nav_request
   ├─ 写 workspace_event(nav_requested)
   ├─ 返回助手消息，payload 含 nav_request + 待审批说明
3. 前端 Agent 面板收到消息
   ├─ 显示『正在打开资产库 · 角色』提示条
   ├─ navigationService.navigateWorkspace(nav_request)
   │   ├─ 校验 → router.push('/project/prj_xxx/assets?tab=character&episode=1')
   │   └─ 上报 POST /api/workspace/page-context(nav_result=success)
   └─ 左侧资产库自动切到角色 Tab
4. 用户点『批准并执行』
   ├─ resolveApproval → executeApproval → 真实 Job 创建
   └─ task_event 推送 → 左侧资产卡片状态实时变化（现有轮询也兼容）
```

关键约束：

- **导航先于业务**：让用户在等待前就看到目标页面。
- **导航不等于业务成功**：步骤 3 的导航成功只说明页面切换了，任务还没创建。Agent 回复必须分开表述。
- **不重复导航**：若用户已经在目标页面且 Tab、集数一致，`navigateWorkspace` 直接返回 `already_there`，不发路由跳转。

多步任务的连续导航：

```
角色生成中 → 左侧资产库 · 角色 Tab
角色全部完成 → Agent 写 workspace_event(nav_requested, tab=scene)
                → 前端再次切 Tab
场景全部完成 → 切 tab=prop
```

约束：**只有上一阶段真实完成后才切换**（由 `TaskStage` 状态驱动），不得按预设时间模拟推进。

### 12.2 页面驱动 Agent（双向，第二阶段）

用户在左侧页面的每个成功操作都会上报。上报内容与 Agent 侧消费方式：

| 用户操作 | 上报 action | Agent 侧影响 |
|---|---|---|
| 修改角色提示词并保存 | `asset_prompt_updated` | `focus` 指向该角色，`operation=modify_prompt`；下次生成自动读取最新提示词 |
| 上传新参考图 | `asset_uploaded` | `focus` 指向新资产；旧参考图失效标记由后端业务逻辑处理 |
| 删除道具 | `asset_deleted` | `focus` 清空（实体已不存在）；Agent 后续查询会拿到最新清单 |
| 修改分镜提示词 | `shot_prompt_updated` | `focus` 指向该镜头；`revision` 递增，Agent 若持有旧 revision 将拒绝自动覆盖 |
| 重新生成镜头 | `shot_regenerated` | `focus` + `operation=regenerate_shot`；Agent 查询任务状态时能看到新 Job |
| 审核通过资产 | `asset_approved` | 后续生成闸口放开；Agent 可据此推进下一步 |
| 审核驳回资产 | `asset_rejected` | Agent 明确知道闸口未开，不推进分镜 |
| 确认关键帧 | `keyframe_confirmed` | 视频生成闸口状态更新 |

**Agent 侧如何真正使用这些上下文**（避免『上报了但没用』）：

1. `focus` 参与 `build_focus_hint`，作为 LLM 系统提示的一部分（现有机制，只需扩展 `FocusContext` 支持 page/tab/episode）。
2. `operation` 参与下一轮意图解析的歧义消解（例如用户说『再改一下』，指的就是最近操作的那个实体）。
3. **最权威的数据永远是数据库**：Agent 执行任何工具前都重新查库，上下文只用于『理解用户指的是谁』，不用于『判断业务状态』。

### 12.3 事件环的彻底规避

| 潜在环 | 规避方式 |
|---|---|
| 页面操作 → Agent → 又导航回该页面 | 导航请求带 `reason`，若目标页与当前页完全相同则前端直接丢弃；后端 `nav_requested` 事件在 1 秒内对同一 page 去重 |
| Agent 修改数据 → 触发页面事件 → 又上报 | 页面只在**用户主动操作**的成功回调里上报，不监听数据变化自动上报 |
| SSE 事件触发页面刷新 → 页面又上报 | 上报接口的响应**不写 SSE 事件**（只广播给其他标签页），本标签页不回环 |
| 多标签页互相触发 | 每个标签页的 SSE 连接独立，上报去重键含 `session_id`，跨标签页的重复上报被后端幂等窗口拦截 |

---

## 13. 任务生命周期设计

### 13.1 实际采用的状态机

保持现有 `_TASK_TRANSITIONS` 语义，**不新增状态值**：

```
         ┌─────────────┐
         │   queued    │  （含历史 pending，读侧等价）
         └──────┬──────┘
                │ start_task
                ▼
         ┌─────────────┐
    ┌───►│   running   │◄───┐ 重试（新 Job，同 Task）
    │    └──────┬──────┘    │
    │           │            │
    │   ┌───────┼───────┐    │
    │   ▼       ▼       ▼    │
    │ completed failed cancelled
    │  （终态） （可重试）（终态）
    └───────────────────────┘
```

需求第七章提出的 9 个状态与现有实现的映射：

| 需求状态 | 本项目实现 | 说明 |
|---|---|---|
| pending | `queued`（历史 `pending` 等价） | 已受理待执行 |
| queued | `queued` | 同义，不重复建模 |
| running | `running` | 执行中 |
| waiting_approval | **不由 task.status 表达**，由 `agent_confirmation.status=pending` 表达 | 此时 Task 尚未创建，是审批阶段而非任务阶段 |
| paused | **不支持** | ComfyUI 队列无法真正暂停，不建空承诺 |
| retrying | `retry_count > 0` 且 `status=running` | 用计数表达，不用状态值 |
| completed | `completed` | 终态 |
| failed | `failed` | 可被 retry 拉回 running |
| cancelled | `cancelled` | 终态 |

`TaskStage.status` 独立枚举：`pending / running / succeeded / failed / skipped / cancelled`，与 Task 状态正交。

### 13.2 生命周期不依赖页面

| 场景 | 机制保证 |
|---|---|
| 页面切换 | 任务由后端 worker 执行，前端只是订阅者；离开页面只断开一条 SSE，不影响执行 |
| 浏览器刷新 | 同上；刷新后通过 `GET /api/tasks?session_id=` 重新拉快照 |
| 关闭 Agent 面板 | 同上；面板只是展示层 |
| 用户切换作品 | `Task.project_id` 在创建时固定，切换作品只改 WorkspaceState，不触碰已有 Task |
| 用户切换集数 | `Task.episode_no` 在创建时固定（本次新增字段保证这一点） |

### 13.3 幂等与防重复提交

| 层级 | 机制 |
|---|---|
| 审批 | `agent_confirmation.idempotency_key`（会话+项目+对象+版本+轮次+类型） |
| 任务创建 | `create_task` 按 `agent_task_id` 幂等；同 `agent_task_id` 返回已存在 Task |
| 任务执行 | `start_task` 幂等（已 running 返回 True） |
| 阶段重试 | 新增 retry 接口按 `stage_id` 幂等，已成功阶段拒绝重试 |
| 页面上下文 | `POST /api/workspace/page-context` 60 秒幂等窗口 |
| 导航 | 前端 `already_there` 判断 + 后端 1 秒同页去重 |

### 13.4 进度真实性

| 层级 | 进度来源 | 是否可信 |
|---|---|---|
| Task.progress | 各 TaskStage 进度均值（`_aggregate_progress`） | 可信，确定性代码计算 |
| TaskStage.progress | `update_stage_progress_by_job` 由 Job 里程碑桥接 | 可信 |
| Job 进度 | worker 执行器真实里程碑（如 `x/y 张图完成`） | 可信 |
| 前端展示 | 直接读 TS 快照，不做插值 | 可信 |

禁止事项（必须在 code review 中拦截）：

- 前端 `setInterval` 自增百分比。
- 后端 `time.sleep` 后按固定表推进进度。
- 已知任务数为 N 时按 `elapsed/total_estimated` 反推进度。

---

## 14. 状态管理设计

### 14.1 WorkspaceState（前端权威）

```ts
// src/stores/workspace.ts
export const useWorkspaceStore = defineStore('workspace', () => {
  // —— 定位状态 ——
  const currentProjectId = ref<string | null>(null)
  const currentProjectName = ref<string | null>(null)
  const currentEpisodeNo = ref<number | null>(null)
  const currentPage = ref<WorkspacePage | null>(null)     // 与 route.name 双向同步
  const currentTab = ref<string | null>(null)
  const currentEntityType = ref<string | null>(null)
  const currentEntityId = ref<string | null>(null)
  const currentFocus = ref<string | null>(null)

  // —— 布局状态 ——
  const leftRatio = ref(0.71)
  const agentCollapsed = ref(false)
  const leftFullscreen = ref(false)

  // —— 保护状态 ——
  const pendingGuard = ref<{ dirty: boolean; message: string } | null>(null)

  // —— 版本 ——
  const contextVersion = ref(0)

  return { /* ... */ }
})
```

持久化策略：

| 字段 | 存储 | 原因 |
|---|---|---|
| `currentProjectId` / `currentEpisodeNo` / `currentPage` / `currentTab` / `currentEntityId` | `sessionStorage['workspace:state']` | 刷新恢复；关标签即清，避免脏定位 |
| `leftRatio` / `agentCollapsed` | `localStorage['workspace:layout']` | 用户偏好，长期保留 |
| `pendingGuard` | 不持久化 | 仅在当前页面生命周期内有效 |

### 14.2 AgentState（客户端视图 + 服务端权威）

| 数据 | 来源 | 是否持久化 |
|---|---|---|
| 会话列表 / 会话信息 | `GET /api/agent/sessions` | 服务端 |
| 消息列表 | `GET /api/agent/sessions/{id}/messages?after_seq=` | 服务端 |
| 执行计划 | `GET /api/agent/sessions/{id}/plans` | 服务端（本次启用 `agent_plan_step`） |
| 待审批 | `GET /api/agent/sessions/{id}/approvals` | 服务端（`agent_confirmation`） |
| 待审核卡片 | `GET /api/agent/sessions/{id}/reviews` | 服务端（`agent_confirmation`） |
| `activeSessionId` | 用户选择 | `sessionStorage['workspace:session']` |
| `connectionStatus` | SSE 连接状态 | 不持久化 |
| `pendingNav` | 从消息 payload 中提取 | 不持久化（刷新后从消息重放） |

**关键约束**：AgentState 中**不允许**出现任何业务数据（资产、镜头、任务进度）。这些一律从对应 API 与 SSE 读，避免出现第二份真相。

### 14.3 TaskState（后端权威，前端只订阅）

前端不新增 task store。现有 `useTaskEvents` 的 `taskSnapshots` 提升为模块级单例：

```ts
// src/composables/useTaskEvents.ts 改造
const taskSnapshots = reactive<Record<string, TaskSnapshot>>({})
let sharedSource: EventSource | null = null
let refCount = 0

export function useTaskEvents() {
  // connect() 时 refCount++，refCount 从 0→1 时建连接
  // disconnect() 时 refCount--，归零时关闭连接
  // 返回共享的 taskSnapshots
}
```

这样做的收益：

- 任务卡片在 Agent 面板、任务中心页面看到的状态**完全一致**。
- 页面切换不会重复建立 SSE 连接。
- 刷新后 `registerCard` 从 `GET /api/tasks` 重建快照。

### 14.4 三态隔离校验清单（实施时逐条自检）

| 检查 | 期望 |
|---|---|
| 切换页面后 `Task.project_id` 是否变化 | 不变 |
| 切换作品后旧任务的 SSE 是否停止推送 | 不停，仍推进 |
| 切换集数后旧集任务的进度是否串到新集 | 不串，按 `episode_no` 过滤 |
| 关闭 Agent 面板后任务是否停止 | 不停止 |
| 刷新后 `workspace:lastSeq` 是否导致重复执行任务 | 不会，seq 只影响事件应用 |
| Agent 是否使用前端传入的进度值 | 不使用，只信后端 |

---

## 15. 审批和权限设计

### 15.1 现有机制（保留不动）

| 机制 | 实现 | 说明 |
|---|---|---|
| 工具审批门禁 | `agent_governance.evaluate_tool_policy` | `requires_approval=True` 且未批准 → `approval_required`，无副作用 |
| 审批两步 | `resolve`（裁决） + `execute`（执行） | 执行幂等，复用已存在任务 |
| 业务审核 | `agent_confirmation.kind=artifact_review` | 剧本/资产/关键帧/视频审核，绑定对象与版本 |
| 审核幂等键 | `idempotency_key` | 会话+项目+对象+版本+轮次+类型 |
| 审核消息 | `AgentMessage(role='approval')` | 落审计，不进 LLM 上下文 |
| 过期处理 | `expire_stale_pending` | 待确认事项超时自动过期 |
| 拒绝后阻断 | `review_confirmation.is_downstream_blocked` | 下游任务被阻断，不静默放行 |

### 15.2 新增：导航不参与审批

| 项 | 设计 |
|---|---|
| 导航工具风险等级 | `low`，`requires_approval=False` |
| 理由 | 导航不改数据，只改用户看到什么；若导航也需审批，会把『打开资产库』变成需要点击确认的操作，严重破坏体验 |
| 兜底 | 导航白名单 + 实体存在性校验 + 未保存内容保护，三重拦截 |

### 15.3 新增：跨项目访问控制

Agent 可能被要求『去看作品 B 的任务』，而此时会话绑定的是作品 A。规则：

| 场景 | 允许 | 处理 |
|---|---|---|
| 导航到当前会话项目 | 允许 | 直接执行 |
| 导航到其他项目（只读取查看） | 允许 | 导航可以，但 Agent 不能对那个项目发起写操作 |
| 对非当前会话项目发起写操作 | **拒绝** | 工具层校验 `project_id == session.project_id`，否则返回 `project_scope_violation` |

实现位置：

- `workspace_nav.build_nav_request` 允许跨项目导航，但 payload 记 `cross_project=true`。
- `execute_tool` / `execute_pipeline_tool` / `execute_regen_tool` 三处统一加 scope 校验（在 `evaluate_tool_policy` 之后、preconditions 之前）。

### 15.4 审批展示规范

Agent 面板的审批卡片必须包含（需求第九章第 3 条）：

| 字段 | 来源 | 示例 |
|---|---|---|
| 操作名称 | `TOOL_REGISTRY[name].description` | 提交生成申请（角色资产） |
| 目标对象 | 从 `args` 提取并补全名称 | 角色 · 孙悟空（chr_xxx）· 第 1 集 |
| 影响范围 | 工具元数据 + 目标类型 | 新建 1 个生成任务，消耗 ComfyUI 算力 |
| 风险等级 | `spec.risk_level` | 中 |
| 审批编号 | `confirmation.id` | cnf_xxx |
| 有效期 | `confirmation.expires_at` | 30 分钟内有效 |

**不得**只显示『是否批准』而没有对象信息。

### 15.5 一次授权不覆盖未来操作

| 约束 | 实现 |
|---|---|
| 每次高风险工具调用独立审批 | `execute_tool` 每次检查 `approved` 参数，不缓存 |
| 审批绑定具体参数 | `args_fingerprint(tool_name, args)`，参数变了审批失效 |
| 会话内不自动继承 | `find_approved` 只匹配完全相同的指纹与幂等键 |

---

## 16. 异常恢复方案

### 16.1 故障矩阵

| 故障 | 影响 | 恢复策略 |
|---|---|---|
| SSE 断线 | 前端收不到新事件 | EventSource 自动重连；`onopen` 触发 backfill |
| SSE 连续失败 | 一直重连不上 | 连续 5 次失败降级为 3 秒轮询 `backfill` |
| 事件丢失 | 本地状态与数据库不一致 | 重连后 backfill；仍缺则对运行中任务调 `GET /api/tasks/{id}` 拉快照 |
| 浏览器刷新 | 页面状态丢失 | `sessionStorage` 恢复定位 + 拉会话/计划/审批 + 拉任务快照 |
| 后端重启 | 内存态丢失 | 所有状态在数据库；`reconcile_orphan_tasks` 启动对账；SSE 重连自动补齐 |
| Worker 崩溃 | Job 卡在 running | 现有 `scheduler` 对账机制（未改动） |
| LLM 超时 | chat 无回复 | 现有指数退避重试 3 次；最终失败保留用户消息，前端可重试 |
| 导航失败 | 页面没跳 | 前端捕获错误，回传 `nav_failed`，Agent 面板显示系统提示，Agent 如实告知 |
| 未保存保护阻断 | 导航被拦 | 弹确认框，用户选择；选择取消则回传 `nav_blocked` |
| 并发编辑冲突 | 保存被拒 | 返回 `revision_conflict` + 服务端最新值，前端提示用户选择 |
| 部分子任务失败 | 父任务结果不完整 | `TaskResult.result_type=partial`，列出失败项；`POST .../retry` 只重试失败阶段 |

### 16.2 页面刷新恢复的完整时序（可直接作为验收用例）

```
T0  用户正在资产库 · 角色 Tab · 第 1 集，Agent 正在生成 3 个角色
T1  用户按 F5

恢复流程：
1. 读 sessionStorage['workspace:state'] → {projectId, episodeNo:1, page:'assets', tab:'character'}
2. 读 sessionStorage['workspace:lastSeq:ags_xxx'] → 812
3. router.replace('/project/prj_xxx/assets?tab=character&episode=1')
4. GET /api/agent/sessions/ags_xxx           → 会话信息 + context_state.version
5. GET /api/agent/sessions/ags_xxx/messages?after_seq=<本地最大seq-20>
6. GET /api/agent/sessions/ags_xxx/plans
7. GET /api/agent/sessions/ags_xxx/approvals + /reviews
8. GET /api/tasks?session_id=ags_xxx&status=running → 找到 task_xxx
9. GET /api/tasks/task_xxx                   → 真实进度（不执行任何操作）
10. EventSource('/api/tasks/events?last_seq=812')
11. EventSource('/api/workspace/events?session_id=ags_xxx&last_seq=812')

期望：
- 左侧回到资产库角色 Tab，3 个角色的真实状态立刻可见
- 右侧对话、计划、进度全部恢复
- 不产生任何新任务（步骤 9 只读）
```

### 16.3 冲突处理（需求第八章）

#### 16.3.1 版本字段策略

| 对象 | 字段 | 递增时机 |
|---|---|---|
| `shot.revision` | 新增 | `image_prompt` / `video_prompt` / `dialogue` 等提示词字段更新 |
| `asset.revision` | 新增 | 资产的元数据更新（如名称、提示词快照）；生成新图不改 revision，改 `version` |

#### 16.3.2 保存时的冲突检测

```
客户端提交 {entity_id, revision, changes}
   ↓
SELECT ... FOR UPDATE（SQLite 下用事务内读取）
   ↓
if db_revision != client_revision:
    409 revision_conflict
    body: {latest: {...}, your_revision, latest_revision}
else:
    revision += 1
    commit
```

#### 16.3.3 用户优先原则

| 场景 | 处理 |
|---|---|
| Agent 与用户同时编辑同一对象 | `revision` 不同则 Agent 的写入被拒，返回 `revision_conflict`，Agent 如实告知『你刚刚改过，我需要重新读取』 |
| 用户手动编辑优先 | 用户保存总是成功（读的是最新 revision）；Agent 使用旧 revision 必失败 |
| 不可合并冲突 | 前端弹窗展示两版差异，用户选择保留哪一版 |
| 修改来源记录 | 写 `asset.input_snapshot` 或新增审计字段记录 `source`（user / agent / system / background_task） |

关于 `source` 字段：本次**不加新列**，采用轻量方案——在 `input_snapshot` JSON 里记录 `{"updated_by": "agent", "tool": "regenerate_asset"}`，避免为审计元数据再加一列。

#### 16.3.4 编辑锁边界

- **不实现长时间编辑锁**。Agent 生成任务最长可能跑几分钟，若锁住资产，用户就无法修改提示词。
- 采用乐观并发：不加锁，靠 `revision` 检测冲突。
- 只有 `asset.locked=True`（用户主动锁定资产）时，Agent 的写操作被拒，返回 `asset_locked`。这是既有语义，本次不改。

---

## 17. 测试计划

### 17.1 后端单元与集成测试

| 编号 | 测试项 | 覆盖点 | 现有测试 |
|---|---|---|---|
| BE-01 | `workspace_nav` 白名单校验 | 非法 page 被拒 / 缺 project_id 被拒 | 新增 `test_workspace_nav.py` |
| BE-02 | `workspace_nav` 实体存在性 | 伪造 entity_id 被拒 / 跨项目 entity 被拒 | 新增 |
| BE-03 | `navigate_workspace` 工具幂等 | 重复调用返回相同 nav_request，无副作用 | 新增 `test_agent_tools.py` 用例 |
| BE-04 | 页面上下文幂等 | 60 秒内重复上报 `accepted=false` | 新增 `test_workspace_context.py` |
| BE-05 | 上下文防环 | 上报只更新 `context_state`，不产生 Task | 新增 |
| BE-06 | `workspace_event` 顺序 | 并发写入 `conversation_seq` 不冲突 | 新增 |
| BE-07 | SSE 续传 | `last_seq` 严格大于判定，重复事件不重放 | 复用 `test_progress` 模式 |
| BE-08 | 任务 `episode_no` 归属 | 不同集任务不串 | 新增 `test_task_episode_scope.py` |
| BE-09 | 父子任务汇总 | 父任务 `partial` 时结果含失败项 | 扩展 `test_agent_tasks.py` |
| BE-10 | 阶段重试幂等 | 同 stage 重复 retry 不重复建 Job | 新增 |
| BE-11 | 计划持久化 | `agent_plan_step` 真实写入且可查询 | 新增 `test_agent_plan.py` |
| BE-12 | 冲突检测 | 旧 revision 写入返回 409 | 新增 `test_revision_conflict.py` |
| BE-13 | 跨项目写保护 | 非会话项目写操作被拒 | 新增 |
| BE-14 | 审批不继承 | 一次批准不影响下一次调用 | 现有 `test_agent_governance.py` 已覆盖，补导航用例 |

### 17.2 前端测试

| 编号 | 测试项 | 方法 |
|---|---|---|
| FE-01 | 双栏渲染 | 工作区路由下左右两栏均渲染 |
| FE-02 | 分栏拖拽 | `leftRatio` 变化且在 0.5–0.85 内 |
| FE-03 | 折叠/展开 | Agent 折叠后左栏 100% |
| FE-04 | Agent 常驻 | 路由切换后面板 DOM 节点不重建（检查 sessionId 输入框不丢焦点） |
| FE-05 | 移动端 | ≤1024px 转为抽屉，可切换 |
| FE-06 | 定位参数 | `/assets?tab=character` 打开即选中角色 Tab |
| FE-07 | 未保存保护 | 有未保存内容时导航被阻断并弹窗 |
| FE-08 | 动作上报 | 保存资产提示词后发出 1 次 `page-context` 请求 |
| FE-09 | 上报去重 | 连续两次相同操作只发 1 次（1 秒窗口） |
| FE-10 | 刷新恢复 | 刷新后定位、消息、计划、任务状态全部恢复 |

### 17.3 端到端场景（对应需求第十三章）

| 场景 | 关键断言 |
|---|---|
| **S1 新增作品** | Agent 识别创建意图 → 左侧作品列表弹出新建弹窗 → 真实创建 → 自动进详情 → Agent 回复含作品 ID |
| **S2 生成资产库** | 左侧切到资产库角色 Tab → 每个角色状态来自真实 Job → 完成后显示真实图片 → 结束等待审核 → **未审核不进入分镜** |
| **S3 生成分镜图** | 左侧切到分镜图 → 自动选第一集 → 每镜头独立状态 → 失败可单镜头重试 |
| **S4 用户修改资产** | 保存后 `context_state.focus` 更新 → `revision` 递增 → Agent 下次生成读取新提示词（验证：旧提示词不再出现在生成入参） |
| **S5 用户切换作品** | 左侧变作品 B → 作品 A 任务继续 → A 结果不写入 B（验证：B 的 asset 表无新增） |
| **S6 浏览器刷新** | 见 16.2 时序，无新任务产生 |
| **S7 Agent 执行失败** | 显示真实 error_code → 保留已成功阶段 → 重试只重跑失败阶段 |
| **S8 同时编辑** | 旧 revision 写入被拒 → 提示冲突 → 数据库以最新提交为准 |
| **S9 事件断线** | 断开网络 10 秒 → 恢复后自动 backfill → 无重复事件、无缺失状态 |
| **S10 多任务并行** | 两个不同集任务同跑 → 各自进度独立 → 父任务正确汇总 |

### 17.4 回归测试（必须全部通过）

| 回归项 | 命令 / 范围 |
|---|---|
| 现有后端测试基线 | `pytest`（基线 **672 passed / 0 failed / 0 skipped**，改后不得低于） |
| 资产纯净性 | `test_asset_purity.py`、`test_scene_cleanliness.py` |
| 资产审核闸口 | `test_assets_gate.py` |
| 关键帧闸口 | `test_keyframe_gate.py` |
| TTS 与 H3 解耦 | `test_audio_policy.py` |
| 合成与导出 | `test_compose_export.py`、`test_compose_export_task_integration.py` |
| 任务编排 | `test_agent_tasks.py`、`test_pipeline_state.py` |
| 审批治理 | `test_agent_governance.py`、`test_pending_confirmation.py` |
| 前端构建 | `npm run build`（vue-tsc 类型检查必须 0 error） |

---

## 18. 分阶段实施任务

### Phase 0：审计补充（✅ 已完成，2026-11-13）

目标：补齐本次审计中此前标注待核实的项，形成最终开工基线。

| 任务 | 验收 |
|---|---|
| 核实 `_handle_generate_request` 与 LLM 工具循环收敛策略 | ✅ 不存在独立函数；`generate_request` 走 `NextAction.GUIDE` 确定性分派，不进入主对话 |
| 核实 `parent_agent.aggregate_task_results` 的 incomplete 判定 | ✅ `incomplete = unfinished > 0`，unfinished 统计 pending/running 阶段 |
| 核实 `character` / `scene` / `prop` 三个实体表字段 | ✅ 均在 `narrative.py`，`TimestampMixin` 提供 `updated_at`，更新接口无版本校验 |
| 核实 `assets.py` 现有写接口版本校验 | ✅ 无 PATCH 接口；写入走 upload/replace/regenerate/rollback，均无 If-Match |
| 跑 `pytest` 记录真实基线 | ✅ 672 passed / 0 failed / 0 skipped（65 warnings 均为 Pillow deprecation） |

**不产出业务代码。Phase 0 已完成，无需额外工期。**

### Phase 1：双栏工作台外壳（2~3 天）

| 任务 | 产出 |
|---|---|
| 新建 `DirectorWorkspaceLayout.vue` | 工作区白名单判断 + 双栏渲染 |
| 新建 `WorkspaceSplitPanel.vue` | 拖拽分栏、折叠、全屏、移动端抽屉 |
| 新建 `workspace` store | 布局状态 + 定位状态（先不含事件） |
| 修改 `App.vue` | 用 Layout 包住 `router-view` |
| 修改 `AppLayout.vue` | 移除 `ProjectProgressRail`（迁到左栏），其余不动 |
| 新建 `DirectorAgentPanel` 骨架 | 先只承载现有 `DirectorConsoleView` 的对话区（复制而非重写，标记 TODO） |

验收：工作区路由下双栏显示，`/settings` 仍单栏；切换路由 Agent 面板不卸载；`npm run build` 通过。

### Phase 2：导航与状态（3~4 天）

| 任务 | 产出 |
|---|---|
| 后端 `workspace_nav.py` | 白名单 + 校验 + 错误码 |
| 后端 `navigate_workspace` 工具注册 | 加入 `_READ_ONLY_TOOLS` |
| 前端 `workspaceNavigation.ts` | 统一导航入口 + 未保存保护 |
| 前端 `agent` store | 会话 / 消息 / 计划 / 审批 |
| 业务页面接定位参数 | `AssetsView` / `StoryboardView` / `StoryboardImagesView` / `ProjectListView` / `ComposeView` |
| `context_manager` 扩展 `FocusContext` | 增加 `page` / `tab` / `episode_no` |

验收：从对话里说『打开第一集的资产库角色』能真实定位；`/project/x/assets?tab=character&episode=1` 直接访问也能定位。

### Phase 3：事件与实时同步（3~4 天）

| 任务 | 产出 |
|---|---|
| 迁移：`workspace_event` 表 + `task` 加列 | Alembic 迁移 + downgrade |
| 后端 `workspace_event.py` | 写入 + 查询 + 会话内 seq |
| 后端 `/api/workspace/events` + backfill | 复用现有 SSE 生成器 |
| 前端 `useWorkspaceEvents.ts` | 消费 + backfill + 降级轮询 |
| `useTaskEvents` 改单例 | refCount 管理 |
| `task_orchestrator.create_task` 扩展 | `episode_no` / `parent_task_id` / `tool_name` |

验收：断网 10 秒恢复后无事件丢失；任务 `episode_no` 正确归属。

### Phase 4：双向交互与冲突（3~4 天）

| 任务 | 产出 |
|---|---|
| 后端 `workspace_context.py` | 上报接口 + 幂等窗口 |
| 后端 `POST /api/workspace/page-context` | 完整实现 |
| 前端 `workspaceContextReporter.ts` | 业务成功回调上报 + 1 秒去重 |
| 业务页面接上报 | `AssetsView` / `StoryboardView` / `StoryboardImagesView` |
| 迁移：`shot.revision` / `asset.revision` | 加列 + 写入路径递增 |
| 冲突检测 | 写接口校验 revision，409 返回最新值 |
| 三套执行器加项目作用域校验 | `project_scope_violation` |

验收：S4 / S8 两个场景通过；无事件环（连续操作 20 次不产生额外任务）。

### Phase 5：计划持久化与业务流程接入（3~4 天）

| 任务 | 产出 |
|---|---|
| `agent_plan_step` 写入 | chat 流程中多步请求建计划 |
| `GET /api/agent/sessions/{id}/plans` | 查询接口 |
| 前端 `AgentPlanCard.vue` | 计划展示 + 实时状态 |
| 按顺序接入业务流程 | 作品 → 剧本 → 资产 → 分镜图 → 分镜视频 → 音频 → 合成 → 导出 |
| `POST /api/tasks/{id}/stages/{sid}/retry` | 阶段重试 |

验收：S1 / S2 / S3 / S7 场景通过；资产未审核不能进入分镜生成（回归闸口）。

### Phase 6：测试与优化（2~3 天）

| 任务 | 产出 |
|---|---|
| 补 14 项后端测试 | 见第 17.1 节 |
| 补 10 项前端测试 | 见第 17.2 节 |
| 跑 10 个端到端场景 | 见第 17.3 节 |
| 跑全部回归 | 见第 17.4 节 |
| 移动端适配复核 | ≤1024px、≤768px 两档 |
| 性能复核 | 首屏不因双栏增加额外请求 |

**总工期估算：17~23 个工作日**（Phase 0 审计补充已于 2026-11-13 完成，Phase 1.5 地基修正已纳入工期）。

---

## 19. 文件级修改清单

### 19.1 前端新增文件

| 文件 | 职责 | 优先级 |
|---|---|---|
| `frontend/src/components/workspace/DirectorWorkspaceLayout.vue` | 工作区容器：白名单判断 + 双栏 or 单栏 | P0 |
| `frontend/src/components/workspace/WorkspaceSplitPanel.vue` | 拖拽分栏 + 折叠 + 全屏 + 移动端抽屉 | P0 |
| `frontend/src/components/workspace/WorkspaceContainer.vue` | 左栏视口，承载 `router-view` 与进度轨 | P0 |
| `frontend/src/components/agent/DirectorAgentPanel.vue` | 右侧 Agent 面板外壳 | P0 |
| `frontend/src/components/agent/AgentHeader.vue` | 会话切换 / 模型 / 折叠 | P1 |
| `frontend/src/components/agent/AgentTimeline.vue` | 对话 + 计划 + 进度统一时间线 | P0 |
| `frontend/src/components/agent/AgentPlanCard.vue` | 执行计划卡片 | P1 |
| `frontend/src/components/agent/AgentApproval.vue` | 待审批 / 待审核 | P1 |
| `frontend/src/components/agent/AgentInput.vue` | 输入框 + 发送 | P0 |
| `frontend/src/services/workspaceNavigation.ts` | 统一导航服务（唯一改路由入口） | P0 |
| `frontend/src/services/workspaceContextReporter.ts` | 页面动作上报 | P1 |
| `frontend/src/stores/workspace.ts` | WorkspaceState | P0 |
| `frontend/src/stores/agent.ts` | AgentState | P0 |
| `frontend/src/composables/useWorkspaceEvents.ts` | 工作区事件 SSE 消费 | P1 |
| `frontend/src/types/workspace.ts` | 工作区相关 TS 类型 | P0 |

### 19.2 前端修改文件

| 文件 | 当前职责 | 改造内容 | 是否新增 | 优先级 |
|---|---|---|---|---|
| `frontend/src/App.vue` | 主题 + NMessage + `router-view` | 用 `DirectorWorkspaceLayout` 包住 `router-view` | 修改 | P0 |
| `frontend/src/components/AppLayout.vue` | 顶部导航 + 面包屑 + 进度轨 | 移除 `ProjectProgressRail` 渲染（迁至左栏），导航逻辑不变 | 修改 | P0 |
| `frontend/src/router/index.ts` | 16 条扁平路由 | **不改**（方案 A 不需要改路由） | 不改 | — |
| `frontend/src/views/DirectorConsoleView.vue` | 导演台三栏页面 | 拆分为 `DirectorAgentPanel` 系列组件；原文件保留为薄壳转发，保证 `/director` 仍可访问 | 修改 | P0 |
| `frontend/src/views/ProjectListView.vue` | 作品列表 + 新建弹窗 | 支持 `?create=1` 自动打开弹窗 | 修改 | P1 |
| `frontend/src/views/project/AssetsView.vue` | 资产库四类 + 制作 + 审核 | 支持 `?tab=&episode=&entityId=`；上报 `asset_prompt_updated` / `asset_uploaded` / `asset_deleted` / `asset_approved` / `asset_rejected` | 修改 | P0 |
| `frontend/src/views/project/StoryboardView.vue` | 分镜看板 + 视频生成 | 支持 `?episode=&shotId=&focus=`；上报 `shot_prompt_updated` / `shot_regenerated` | 修改 | P0 |
| `frontend/src/views/project/StoryboardImagesView.vue` | 分镜图 + 关键帧确认 | 支持 `?episode=&shotId=`；上报 `keyframe_confirmed` | 修改 | P0 |
| `frontend/src/views/project/ComposeView.vue` | 合成与导出 | 支持 `?stage=compose|export` 定位区块 | 修改 | P2 |
| `frontend/src/views/project/ScriptView.vue` | 剧本编辑 | 支持 `?focus=edit`（可选） | 修改 | P2 |
| `frontend/src/composables/useTaskEvents.ts` | 任务事件 SSE | 改模块级单例 + refCount 管理 | 修改 | P1 |
| `frontend/src/stores/index.ts` | store 导出 | 补导出 `workspace` / `agent` | 修改 | P0 |
| `frontend/src/api/agent.ts` | Agent API 封装 | 补 `listPlans` | 修改 | P1 |
| `frontend/src/api/tasks.ts` | 任务 API 封装 | 补 `retryStage`；`listTasks` 参数补 `episodeNo` / `parentTaskId` | 修改 | P1 |
| `frontend/src/components/TaskCard.vue` | 任务卡片 | 补 `episode_no` 展示、`retry_count` 展示、阶段重试按钮 | 修改 | P1 |
| `frontend/src/styles/global.css` | 全局样式 | 补工作区布局 CSS 变量（栏宽、分隔条） | 修改 | P2 |

### 19.3 后端新增文件

| 文件 | 职责 | 优先级 |
|---|---|---|
| `backend/app/api/v1/workspace.py` | 工作区路由：page-context、events、backfill | P0 |
| `backend/app/services/workspace_nav.py` | 导航白名单 + 校验 + 构建导航请求 | P0 |
| `backend/app/services/workspace_event.py` | 工作区事件写入 / 查询 / 会话内 seq | P1 |
| `backend/app/services/workspace_context.py` | 页面上下文同步 + 幂等 + 防环 | P1 |
| `backend/app/models/workspace.py` | `WorkspaceEvent` 模型 | P1 |
| `backend/app/schemas/workspace.py` | 请求/响应 Pydantic 模型 | P0 |
| `backend/alembic/versions/<hash>_workspace_event_and_task_columns.py` | 迁移（建表 + 加列 + downgrade） | P0 |
| `backend/tests/test_workspace_nav.py` | 导航校验测试 | P1 |
| `backend/tests/test_workspace_context.py` | 上下文上报与幂等测试 | P1 |
| `backend/tests/test_workspace_events.py` | 工作区事件 SSE 与续传 | P1 |
| `backend/tests/test_task_episode_scope.py` | 任务集数归属 | P1 |
| `backend/tests/test_revision_conflict.py` | 并发冲突 | P1 |

### 19.4 后端修改文件

| 文件 | 当前职责 | 改造内容 | 是否新增 | 优先级 |
|---|---|---|---|---|
| `backend/app/main.py` | 应用装配 + 路由注册 | `include_router(workspace.router)` | 修改 | P0 |
| `backend/app/api/v1/agent.py` | 聊天主流程 + 审批 + 计划 | chat 增加导航分支与计划写入；新增 `GET /plans`；`_confirmation_out` 补对象信息 | 修改 | P0 |
| `backend/app/api/v1/events.py` | job/task 事件 SSE | 复用其 SSE 生成器实现工作区事件流（抽公共函数） | 修改 | P1 |
| `backend/app/api/v1/tasks.py` | 任务详情 / 列表 / 取消 | 列表补 `episode_no` / `parent_task_id` 过滤；新增阶段重试端点 | 修改 | P1 |
| `backend/app/services/agent_tools.py` | 28 个工具注册表 + 执行器 | 注册 `navigate_workspace`；`execute_tool` 增加项目作用域校验 | 修改 | P0 |
| `backend/app/services/agent_tool_impls.py` | 工具实现 | 新增 `tool_navigate_workspace` 实现 | 修改 | P0 |
| `backend/app/services/pipeline_tools.py` | 10 个流程工具 | 项目作用域校验 | 修改 | P0 |
| `backend/app/services/regen_tools.py` | 5 个返工工具 | 项目作用域校验 | 修改 | P0 |
| `backend/app/services/task_orchestrator.py` | 任务与阶段生命周期 | `create_task` 扩展 4 个参数；`task_payload` 补新字段；新增 `retry_stage` | 修改 | P0 |
| `backend/app/services/context_manager.py` | 会话上下文 | `FocusContext` 补 `page` / `tab` / `episode_no`；新增 `update_page_focus` | 修改 | P1 |
| `backend/app/services/parent_agent.py` | AgentTask 生命周期 | 补齐计划步骤创建与聚合 | 修改 | P1 |
| `backend/app/services/intent_router.py` | 意图分类 | 补 `nav_request` 解析（纯导航意图） | 修改 | P1 |
| `backend/app/services/agent_governance.py` | 策略引擎 | 无需改动（风险等级已支持低/中/高） | 不改 | — |
| `backend/app/services/approvals.py` | 审批记录 | 补 `_confirmation_out` 扩展字段所需的数据组装 | 修改 | P2 |
| `backend/app/models/task.py` | Task / TaskStage / TaskEvent / TaskResult | Task 补 6 列（`episode_no` / `parent_task_id` / `tool_name` / `input_snapshot` / `retry_count` / `completed_at`） | 修改 | P0 |
| `backend/app/models/narrative.py` | Shot 等叙述层模型 | Shot 补 `revision` 列 | 修改 | P1 |
| `backend/app/models/production.py` | Asset / Job 等产物层模型 | Asset 补 `revision` 列 | 修改 | P1 |
| `backend/app/models/__init__.py` | 模型导出 | 导出 `WorkspaceEvent` | 修改 | P1 |
| `backend/app/api/v1/assets.py` | 资产管理 | 写接口校验 `revision`；返回最新 revision | 修改 | P1 |
| `backend/app/api/v1/shots.py` | 镜头管理 | 提示词更新校验 `revision` | 修改 | P1 |

### 19.5 明确不改的文件

| 文件 / 目录 | 原因 |
|---|---|
| `backend/app/worker/*` | 执行层不重写，任务生命周期由编排层包装 |
| `backend/app/integrations/comfyui*.py` | ComfyUI 工作流契约不得改动 |
| `backend/app/services/prompt_compiler.py` | 提示词编译是既有 P0 契约 |
| `backend/app/services/audio_plan.py`、`api/v1/audio.py`（除 scope 校验） | TTS 与 H3 解耦方案不得改动 |
| `backend/app/services/compose.py`、`export_executor.py` | 合成与导出两阶段不得合并 |
| `backend/data/workflows/*` | 标准工作流 JSON 不得改动 |
| `frontend/src/api/*.ts`（除 agent / tasks） | 业务 API 封装无需改动 |
| `frontend/src/views/series/*` | 系列页面本期不动，仅导航可达 |

---

## 20. 风险和兼容性分析

### 20.1 主要风险

| 编号 | 风险 | 影响 | 概率 | 应对 |
|---|---|---|---|---|
| R1 | `DirectorConsoleView` 拆分为组件时行为丢失 | 高（核心交互） | 中 | 拆分采用『先复制后裁剪』，每拆一个组件跑一次人工回归；保留原文件薄壳保证 `/director` 可用 |
| R2 | Agent 面板常驻导致 SSE 连接数增加 | 中（资源） | 中 | `useTaskEvents` 改单例，多个页面共享 1 条连接；`useWorkspaceEvents` 同样单例 |
| R3 | 页面接定位参数时与现有 `watch` 冲突 | 中（定位失败） | 中 | 定位参数只在 `onMounted` 与 `route.query` 变化时处理；不覆盖现有 `watch(projectId)` |
| R4 | `shot.revision` / `asset.revision` 加列后旧写入路径不递增 | 中（冲突检测失效） | 高 | 实施时逐一 grep 所有 UPDATE 语句，确保所有提示词写入路径都 `revision += 1`；测试覆盖 |
| R5 | 工作区事件表增长过快 | 低（存储） | 低 | `workspace_event` 只记交互，量级远低于 `task_event`；提供按会话清理的运维脚本（不在本期） |
| R6 | 导航工具被 LLM 滥用（频繁切页） | 中（体验） | 中 | `ToolBudget` 复用现有预算控制；前端 `already_there` 短路 |
| R7 | 双栏布局在小屏上挤压业务页面 | 中（可用性） | 高 | ≤1024px 强制抽屉形态，不做双栏；≤768px 底部 Tab 切换 |
| R8 | 迁移在已有数据库上失败 | 高（无法启动） | 低 | 迁移前备份 db；`server_default` 保证已有行；先在本机 `alembic upgrade head` 验证再上 |
| R9 | 上下文上报造成额外请求压力 | 低 | 中 | 1 秒去重 + 只在成功回调触发；上报失败静默（不影响业务） |
| R10 | 未保存保护被页面遗漏 | 中（数据丢失） | 中 | 只对已知有编辑态的页面注册 guard：`ScriptView`、`AssetsView`（提示词编辑）、`StoryboardView`（提示词编辑）；其余页面无需 |

### 20.2 兼容性分析

| 维度 | 结论 | 依据 |
|---|---|---|
| URL 兼容 | **完全兼容** | 方案 A 不改 `router/index.ts`，所有现有 URL 可直接访问 |
| 直接访问页面 | 兼容 | 双栏布局对单页面访问同样生效（左栏渲染该页面，右栏显示 Agent） |
| `/director` 访问 | 兼容 | 保留薄壳页面，行为等价 |
| 现有 API | **完全兼容** | 新增字段均有默认值；新增接口独立路径；`create_task` 新参数均有默认值 |
| 现有 Agent 工具 | 兼容 | 只新增一个工具，其余工具签名与行为不变 |
| 现有工作流 | 兼容 | `integrations/comfyui*` 与 `data/workflows/*` 不改 |
| 现有 TTS 架构 | 兼容 | 音频相关代码除 scope 校验外不改 |
| 资产审核门槛 | 兼容 | `assets_gate` 与 `review_confirmation` 不改 |
| 数据库 | 需迁移 | 1 张新表 + 8 个新列，全部向后兼容 |
| 部署 | 无额外依赖 | 不引入 Redis / WebSocket / 新前端库 |

### 20.3 过度设计风险自查

| 可能的过度设计 | 本次是否采用 | 理由 |
|---|---|---|
| 引入独立 Workspace 微服务 | 否 | 单机自托管，加服务只增加运维成本 |
| 引入消息队列（Redis / Kafka） | 否 | SQLite outbox 已满足 |
| 引入 WebSocket | 否 | SSE 已满足单向推送需求 |
| 引入前端状态机库（XState） | 否 | Pinia + 明确的状态表足够 |
| 为每类业务对象建独立事件表 | 否 | `task_event` + 业务查询已覆盖 |
| 全库加 revision 列 | 否 | 只在真正并发编辑的对象上加 |
| 建 `workspace_state` 服务端表 | 否 | 纯客户端状态 |
| 建任务暂停接口 | 否 | ComfyUI 无法真正暂停，不做空承诺 |

---

## 21. 验收标准

### 21.1 功能验收

| 编号 | 验收项 | 判定标准 |
|---|---|---|
| A-01 | 双栏工作台 | 任意工作区路由下左右两栏同时渲染，右栏不因路由切换重建 |
| A-02 | 分栏调整 | 拖拽后栏宽变化，刷新后保持（localStorage） |
| A-03 | Agent 折叠 | 折叠后左栏 100%，展开恢复原宽度 |
| A-04 | 移动端 | ≤1024px 为抽屉，≤768px 为底部 Tab，功能不缺失 |
| A-05 | Agent 导航定位 | 从对话请求导航，左侧定位到指定页面 + Tab + 集数 + 具体实体 |
| A-06 | 导航失败反馈 | 非法导航请求时，Agent 明确告知失败原因，不谎报成功 |
| A-07 | 页面动作上报 | 左侧保存资产提示词后，Agent 下一轮能识别『刚改的那个角色』 |
| A-08 | 真实进度 | 所有进度条数值均可在 `task_stage.progress` 找到对应记录 |
| A-09 | 刷新恢复 | 刷新后页面、会话、计划、任务状态全部恢复，无新任务产生 |
| A-10 | 切换作品隔离 | 作品 A 任务运行中切到 B，A 任务继续且结果不写入 B |
| A-11 | 冲突检测 | 旧 revision 保存返回 409，前端提示冲突 |
| A-12 | 阶段重试 | 失败阶段可单独重试，已成功阶段不重跑 |
| A-13 | 审批对象完整 | 审批卡片显示操作名、目标对象（含名称）、影响范围、风险等级 |
| A-14 | 资产闸口 | 资产未审核时无法进入分镜生成（回归） |

### 21.2 性能验收

| 项 | 标准 |
|---|---|
| 首屏请求数 | 双栏相比单栏不增加超过 2 个请求 |
| SSE 连接数 | 同一标签页最多 2 条（task + workspace） |
| 事件延迟 | 后端写入到前端展示 < 1.5 秒（现有轮询间隔 1 秒） |
| 页面切换 | < 300ms 完成（不含数据加载） |

### 21.3 安全验收

| 项 | 标准 |
|---|---|
| 导航白名单 | 非白名单 page 一律拒绝 |
| 跨项目写保护 | 非会话项目的写操作被拒绝 |
| 审批不可绕过 | 高风险工具无审批记录时返回 `approval_required` |
| 不泄露敏感信息 | 错误响应不含密钥、内部路径、堆栈 |

---

## 22. 回滚方案

### 22.1 分级回滚

| 级别 | 触发条件 | 回滚动作 | 影响 |
|---|---|---|---|
| L1 功能开关回滚 | 双栏布局出现严重问题 | 通过配置开关（新增 `VITE_WORKSPACE_ENABLED`）关闭双栏，退回单栏 + `/director` 旧形态 | 仅前端，无需重启后端 |
| L2 前端版本回滚 | 前端整体异常 | 部署上一个前端构建产物 | 前端 |
| L3 后端版本回滚 | 新接口异常 | 回滚后端代码到上一版本；`workspace` 路由未注册即不生效 | 后端 |
| L4 数据库回滚 | 迁移导致数据异常 | `alembic downgrade -1`（drop 新表 + drop 新列），再从备份恢复 db | 数据库 |
| L5 数据恢复 | 数据损坏 | 从 `backend/data/backups/` 恢复最近备份 | 全量 |

### 22.2 开关设计

```ts
// frontend/src/config/features.ts  【新增】
export const FEATURES = {
  workspace: import.meta.env.VITE_WORKSPACE_ENABLED !== 'false',
  workspaceEvents: import.meta.env.VITE_WORKSPACE_EVENTS_ENABLED !== 'false',
}
```

`DirectorWorkspaceLayout.vue` 中：`FEATURES.workspace === false` 时直接渲染 `<router-view />`，等价于改造前行为。这保证出问题时可以**一个环境变量切回旧形态**。

### 22.3 回滚前置条件

| 条件 | 说明 |
|---|---|
| 迁移前备份 | `cp backend/data/director_lab.db backend/data/backups/director_lab.db.bak_workspace_v2_<ts>` |
| 迁移可回退 | 每个 `upgrade()` 有对称 `downgrade()`，并在测试库验证过 |
| 旧数据可读 | 新列全部可空或有 `server_default`，旧代码读取时不会因缺列报错 |
| 前端可降级 | 新增代码全部在 `FEATURES.workspace` 开关之后 |

### 22.4 不建议的回滚方式

- **不要**通过删数据回滚（`workspace_event` 可清空，但 `task` 新列不要 drop 后重建表）。
- **不要**在前端代码回滚后仍保留新后端接口（无害，可保留）。
- **不要**在双栏异常时直接回退整个前端架构（用开关即可）。

---

## 23. 待确认事项（需产品/技术负责人拍板）

| 编号 | 事项 | 建议 | 需要谁确认 |
|---|---|---|---|
| Q1 | `task.status` 是否扩展 `waiting_approval` / `paused` | 不扩展（本方案立场） | 技术负责人 |
| Q2 | `session_id` 是否改名为 `conversation_id` | 不改（改动面大，收益低） | 技术负责人 |
| Q3 | 是否本期就做系列页面（`series/*`）的工作台接入 | 不做（先单项目工作台跑通） | 产品 |
| Q4 | 移动端是否需要完整的双栏体验 | 不需要（抽屉形态即可） | 产品 |
| Q5 | `workspace_event` 是否需要保留期策略 | 暂不设（量大时再定） | 技术负责人 |
| Q6 | 导航是否允许跨项目（只读查看） | 允许导航，禁止写操作 | 产品 |

---

## 附录 A：现有可复用能力清单（实施时优先用）

| 能力 | 位置 | 复用方式 |
|---|---|---|
| 三套工具执行器 | `agent_tools` / `pipeline_tools` / `regen_tools` | 直接调用，不重写 |
| 策略门禁 | `agent_governance.evaluate_tool_policy` | 新增工具自动受其约束 |
| 审批记录 | `approvals.create_confirmation` / `resolve_confirmation` | 直接复用 |
| 业务审核 | `review_confirmation` | 直接复用 |
| 会话上下文 | `context_manager` | 扩展字段，不重写 |
| 任务编排 | `task_orchestrator` | 扩展参数，不重写 |
| SSE 生成器 | `api/v1/events.py` | 抽公共函数复用 |
| 任务事件消费 | `useTaskEvents.ts` | 改单例后复用 |
| 生成进度轮询 | `useGenerationProgress.ts` | 保持不变，作为 SSE 的补充 |
| 任务卡片 | `TaskCard.vue` | 直接复用，补字段 |
| 进度轨 | `ProjectProgressRail.vue` | 迁到左栏，不改逻辑 |
| API 错误处理 | `api/client.ts` 的 `ApiError` | 新接口统一用 |
| 设计令牌 | `App.vue` 的 `themeOverrides` | 新组件沿用 |

## 附录 B：本次审计的取证清单

| 结论 | 取证位置 |
|---|---|
| 16 条扁平路由，无嵌套 | `frontend/src/router/index.ts` |
| Layout 仅导航 + 单栏插槽 | `frontend/src/components/AppLayout.vue` 全文 |
| 导演台三栏且任务卡片走 SSE | `frontend/src/views/DirectorConsoleView.vue` 129–148、782–786、620–976 |
| 页面跳转仅 `goProject(page)` | `DirectorConsoleView.vue:602-604` |
| 业务页面只读 `route.params.id` | `grep route.params` 全量结果 |
| SSE seq 严格大于判定 | `frontend/src/composables/useTaskEvents.ts` |
| 进度轮询间隔 2500ms | `frontend/src/composables/useGenerationProgress.ts` |
| 28 个注册工具 | `backend/app/services/agent_tools.py` 中 28 处 `"name": {` |
| 通用循环只开放只读工具 | `INTENT_TOOL_MAPPING` 定义 |
| 10 个流程工具 | `pipeline_tools.py` 注册表键 |
| 5 个返工工具 | `regen_tools.py` 注册表键 |
| 审批门禁在确定性代码中 | `execute_tool` 中 `evaluate_tool_policy` 分支 |
| 任务表缺 `episode_no` 等字段 | `backend/app/models/task.py` 全文 |
| 镜头表无 revision | `backend/app/models/narrative.py:145` Shot 类 |
| 资产表 `version` 是产物版本 | `backend/app/models/production.py` Asset 类注释 |
| `agent_plan_step` 从未写入 | 全仓 grep 仅 purge 与导出引用 |
| 事件端点与实现特征 | `backend/app/api/v1/events.py` 全文 |
| `app/workspace/` 为空目录 | `find backend/app/workspace -type f` 无输出 |
| `FocusContext` 无 page/tab | `backend/app/services/context_manager.py:32-40` |
| 工作区、系列、Agent 既有文档 | `docs/agent/`（25 篇）、`docs/series/`（10 篇） |

---

---

## 24. 复核发现的坑与设计修正（2026-11-13 追加审计）

针对 Plan 的关键前提假设做了定向取证。结论：整体方向成立，但有 5 个 P0 级假设与实际代码不符。

### 24.1 P0 级：会让方案直接失效的坑

#### 坑 1：不存在 LLM 工具调用循环（最致命）

取证：

- `app/integrations/llm.py` 的 `LLMClient` 只有 `chat` / `chat_messages` / `chat_stream` / `chat_json` 四个方法，签名里没有 `tools` 参数；`chat_messages` 构造的请求体只有 `model` + `messages`。
- `api/v1/agent.py` 里的工具调用来自 `build_read_tool_calls(payload.content, s.project_id)`，本质是正则关键词匹配（`_PROGRESS_HINTS` / `_ENTITY_HINTS` / `_SHOT_SEQ_PATTERN` 等）。
- `format_tool_context` 把工具结果压成一行文本，通过 `system_prompt +=` 注入，模型只负责措辞。
- 全仓检索 tool_calls / tools= / function_call / tool_choice，仅命中局部变量 `tool_calls`（记录本轮已执行工具），没有一处是 LLM API 参数。

影响：第 7.1 节初版写「加入 `_READ_ONLY_TOOLS` 使 LLM 通过工具循环调用导航工具」是错的。加了白名单能通过校验，但没有任何代码会主动调用它 —— `build_read_tool_calls` 永远不返回 `navigate_workspace`。

修正：

- 方案 A（推荐）：导航不走 `TOOL_REGISTRY`，走 `intent_router`。在 `pipeline_action` 下新增动作键 `navigate_workspace`，`_handle_pipeline_action` 加一个分支：校验通过后返回 `nav_request`，塞进 `assistant.payload`。
- 方案 B（不推荐）：在 `build_read_tool_calls` 加导航分支。该函数返回 `(tool_name, args)` 由 `_run()` 直接执行，需要从自然语言抽 page / tab / episode，可靠性差；返回的 `nav_request` 还要另找路径塞回 payload。
- 方案 C（本次不做）：给 `LLMClient` 加真正的 function calling（tools + tool_choice + 解析 tool_calls + 多轮循环）。这是框架级改造，应单独立项。

**选择方案 A。** 这也解释了为什么导航必须与业务动作分开记录：它是意图层产物，不是工具执行产物。

#### 坑 2：`INTENT_CATEGORIES` 是封闭集合且有回归基线

取证：`intent_router.py` 的 `INTENT_CATEGORIES` 只有 query / analysis / generate_request / pipeline_action / ambiguous / unsafe 六类；`route_action` 对未匹配项兜底 `return NextAction.CHAT`；`tests/test_intent_router_eval.py` 对 `expected_intent` 与 `expected_action` 逐条断言。

影响：新增 navigate 意图父类会落进兜底 CHAT，导航永不触发；而改动意图枚举会波及 `INTENT_SUBCATEGORIES`、`_ACTION_SUBCATEGORY`、`_INTENT_SYSTEM_PROMPT` 与 eval 基线。

修正：不新增意图父类，在 `pipeline_action` 下加动作键。`NextAction` 枚举与 `route_action` 一律不动，存量 eval 用例无需改。

#### 坑 3：`AgentPlanStep.task_id` 指向的不是 `Task.id`

取证：`models/agent.py` 的 `AgentPlanStep.task_id`；`services/project_purge.py` 明确注释「AgentPlanStep 的外键是 task_id（指向 agent_task.id），不是 session_id」，purge 时用 `agent_task_ids` 过滤。

影响：第 6.3 节写 `GET /api/agent/sessions/{id}/plans?task_id=` 时，若传 `Task.id`（`tsk_` 前缀）会查不到任何记录。两套 ID 前缀不同、不会报错，只静默返回空 —— 这类 bug 最难查。

修正：

- 查询参数改名 `agent_task_id`，语义明确。
- 同时提供 `unified_task_id`，内部先由 `Task.agent_task_id` 反查 `agent_task.id` 再查计划。
- 前端类型定义里区分 `agent_task_id` 与 `task_id`，禁止都叫 `task_id`。

#### 坑 4：`review_confirmation.get_pending` 会短路导航

取证：`chat()` 第 3 步 `_handle_pending_confirmation`，存在 pending 确认事项且本条可判定时直接 return，不进入后续任何分支。

影响：场景 2「生成结束后等待用户审核」时，Agent 想再切 Tab，但此时有 pending 审核会被短路，导航指令根本发不出去。

修正：导航要挂在短路点之前。两种落地方式：

1. 在 chat 入口最前先做一次轻量导航意图识别（只产出 `nav_request`，不改业务状态），与后续分支并行。
2. 在 `_handle_pending_confirmation` 内部也返回 `nav_request`（审核通过后自动导航到下一阶段）。

推荐方式 1，因为它不侵入审核逻辑。

#### 坑 5：`chat()` 有多个助手消息返回分支，`task_card` 只在 2 处注入

取证：`AgentMessage(` 在 `agent.py` 出现于行 190 / 1293 / 1330 / 1361 / 1406 / 1464 / 1622 / 1734 / 1867，而 `task_card` 只在两处注入。

影响：这是既有不一致。若改造时只在主对话末尾加 `nav_request`，视频工作流、联网搜索、pipeline 动作三个高频分支都不会带导航。

修正：抽 `_build_assistant_payload(base, *, unified_task=None, nav_request=None) -> dict`，各分支统一调用。这一步应在 Phase 1.5 完成，否则后面每加一个能力都要重复改多处。

### 24.2 P1 级：会导致功能不正确或返工的坑

#### 坑 6：提示词编辑的真实落点是 `StoryboardRegenConfig`

取证：`models/regen_config.py` 的 `StoryboardRegenConfig` 有 `prompt_sections` / `final_prompt` / `version` / `updated_at`；`StoryboardImagesView.vue` 注释写明「重新生成工作台：提示词 + 参考图 + 槽位绑定统一编辑」「工作台提示词（来自基础版本原始配置，可编辑）」。

影响：初版要给 `shot` 加 `revision` 防提示词并发冲突，找错了表。

修正：冲突检测落在 `storyboard_regen_config.version`（已有字段），加 `expected_version` 语义即可，不需要给 shot 加列。`shot.image_prompt` 仍可经 `PATCH /shots/{id}` 修改且无版本校验，但前端主流程不走它；若要防，用 `updated_at` 比较即可。

#### 坑 7：可编辑提示词字段在 `character` / `scene` / `prop`，不在 `asset`

取证：`Character` 有 `description` / `traits` / `appearance`；`Scene` 有 `description` / `time_note` / `plate_text`；`Prop` 有 `description` / `appearance` / `appearance_attrs`；`characters.py` 有 `PATCH /characters/{character_id}`。

影响：初版要给 `asset` 加 `revision`，但 `asset` 表没有提示词字段，用户改的是实体表。

修正：`revision` 加在 `character` / `scene` / `prop` 三张实体表（见 10.2.3）；`asset` 不加，其写路径都是「生成新版本」，天然无原地覆盖。

#### 坑 8：`useTaskEvents` 的 `onUnmounted(disconnect)` 与单例改造直接冲突

取证：`useTaskEvents.ts` 末尾 `onUnmounted(disconnect)`；`disconnect()` 里 `stopped = true`；`connect()` 开头 `if (eventSource || stopped) return`。

影响：改单例后，任意使用该 composable 的页面卸载就会把全局连接关掉，且 `stopped` 永久阻止重连。表现为「切页面后任务卡片不再更新」，而且是间歇性的，极难复现。

修正：

- 移除 `onUnmounted(disconnect)`，改 `onUnmounted(release)`，`release()` 只做 `refCount--`。
- `refCount` 归零才真正 `disconnect`。
- `stopped` 改 `disposed`，只在显式销毁时置位，`release` 不置位。
- 此项必须在 Phase 3 前完成并单独测试。

#### 坑 9：`EventSource` 硬编码 `/api` 前缀

取证：`useTaskEvents.ts` 里 `new EventSource('/api/tasks/events?last_seq=...')`；而 `api/client.ts` 提供 `apiUrl()`，注释写明「避免子路径部署 404」。

影响：当前根路径部署未暴露，新 SSE 若照抄，子路径部署会挂。

修正：`useWorkspaceEvents` 必须用 `apiUrl()`；顺手把 `useTaskEvents` 也改掉（一行，零风险）。

#### 坑 10：`ToolBudget` 默认上限 12 次 / 30 秒，导航挤不进去

取证：`agent_governance.py` 中 `MAX_TOOL_CALLS_PER_TURN = 12`、`MAX_TOOL_TOTAL_DURATION_MS = 30_000`、`MAX_CONTEXT_CHARS = 24_000`。

影响：每轮固定先跑 3 个 base_tools，再跑 `build_read_tool_calls` 的结果（进度 3 + 实体 2 + 解析 1 + 合成 2 + 失败 1，最坏 9 个），合计已到 12。导航若也走工具会被 `check_before` 拒绝，而且只记 violation 不报错，属于静默失效。

修正：导航走意图分支，不进 `ToolBudget`（与坑 1 方案 A 一致）。

#### 坑 11：`system_prompt` 的上下文预算会从尾部丢弃

取证：`agent.py` 中 `budget.context_allowed(...)` 不满足时记 `context_overflow:dropped_line_chars` 并丢弃该行，保留前面已注入的行。

影响：导航说明若拼在 `system_prompt` 靠后位置，业务数据多时会被丢掉。

修正：工作区相关说明拼在 `system_prompt` 靠前位置（紧接角色定义之后），不跟在工具上下文后面。

### 24.3 P2 级：会拖工期或降低质量的坑

#### 坑 12：`ComposeView` 没有可定位的区块锚点

取证：`ComposeView.vue` 只有 `composeEpisode` / `composeJobId` 等独立 ref，单列布局，没有 section id。

修正：本期不支持下钻，导航只到页面级；若确实要定位，需先给区块加 id。原计划中的 `?stage=compose|export` 降级为不做。

#### 坑 13：`ProjectListView` 的 `?create=1` 会反复弹窗

取证：`ProjectListView.vue` 的 `openCreateModal`，弹窗为 `NModal`。

影响：刷新时 query 未清会无限弹窗。

修正：打开弹窗后立即 `router.replace({ query: {} })` 清参数；关闭后不再重开。

#### 坑 14：`docs/prompts/*.md` 与运行时常量强一致测试

取证：`tests/test_prompt_docs_sync.py` 对 5 个模板 + 6 个负向词做 strip 后全等断言。

影响：给 `system_prompt` 加导航说明时若误改 `prompt_compiler` 模板常量，会连带要求改 `docs/prompts`，漏改即测试红。

修正：导航说明只加在 `api/v1/agent.py` 的 `system_prompt` 拼接处，不碰 `prompt_compiler`。

#### 坑 15：`TimestampMixin` 不会自动填 `completed_at`

取证：`Task` 用 `TimestampMixin`，只提供 `created_at` / `updated_at`。

修正：`complete_task` / `fail_task` / `cancel_task` 三处显式写 `task.completed_at = datetime.now()`，并加测试断言非空。

#### 坑 16：页面白名单两处定义容易不同步

取证：`execute_tool` 先 `evaluate_tool_policy` 再 `input_model.model_validate`，校验失败统一返回 `tool_invalid_args`。

修正：单一真相放 `workspace_nav.NAV_PAGE_WHITELIST`，入参模型用普通字符串类型 + 函数内校验，不在 Pydantic 里重复枚举。

### 24.4 对实施顺序的影响

| 原计划 | 调整 |
|---|---|
| Phase 1 双栏外壳 | 不变 |
| **新增 Phase 1.5 地基修正** | 坑 1（导航走意图分支）、坑 2（复用 pipeline_action）、坑 5（统一 payload 构造）、坑 8（useTaskEvents 单例安全化）。不做完，Phase 2 导航无法真正接通 |
| Phase 2 导航与状态 | 依赖 Phase 1.5 |
| Phase 3 事件与实时同步 | 增加坑 9（apiUrl） |
| Phase 4 双向交互与冲突 | 修正对象：`storyboard_regen_config` + `character` / `scene` / `prop`，不再改 `asset` / `shot` |
| Phase 5 计划持久化 | 明确 `agent_task_id` 与 `unified_task_id` 两个参数（坑 3） |

工期影响：Phase 1.5 约 1 天；Phase 4 因修正对象反而略减（少改两张表）。整体仍为 17~23 个工作日。

### 24.5 结论

Plan 方向成立（双栏 + 导航 + 事件 + 三态隔离），但有 5 个 P0 级前提假设与实际代码不符。其中「不存在 LLM 工具调用循环」最致命，它决定导航必须走意图分支而不是工具注册表，直接改变 Phase 2 的实现方式。第 7.1 节与第 10.2.3 / 10.2.4 节已按本节修正。

Phase 0 三项核实已于 2026-11-13 完成，详见本文档 Phase 0 节与 1.3.2 / 1.3.3 节。

**文档结束。本阶段未修改任何业务代码、未执行数据库迁移、未覆盖任何配置文件。**

