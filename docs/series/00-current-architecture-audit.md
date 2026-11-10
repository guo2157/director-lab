# Director-Lab 系列化改造 · 现有架构审计报告

> 审计基线：commit `0fb155d`（2026-11-10）  
> 审计方式：只读检查 `backend/app`、`frontend/src`、`docs` 与 36 个 Alembic 迁移，未修改任何业务代码  
> 事实约定：未标注者均为代码或文档直接可见；推断项显式标注「推断」

---

## 1. 结论摘要

1. 现有系统是**单机、单用户**的 FastAPI + SQLite 单体应用。`Project` 是唯一的生产工作空间，承载剧本、解析、资产、分镜、视频、音频、合成与导出的完整链路。
2. 已存在 `EpisodeContinuity` 表，但它解决的是**跨集视频首尾帧连续性**（末帧提取、首帧注入、上游重合成后版本失效），**不是剧情连续性**。系列化不能把它当作叙事状态载体，只能复用其帧连续能力。
3. 缺失的核心实体：`Series`、叙事维度的 `Episode`（含剧本状态与生产状态分离）、`Series Bible` 版本、`Story Event Ledger`、`Plot Thread`、`Context Manifest`、正式状态提交服务、剧本确认门禁。
4. 已有 `Task` / `TaskStage` / `TaskEvent` + SSE `/api/events`，可直接承载续写长任务与真实进度，**不需要新建异步框架**。
5. 已有 `Asset` / `AssetRef` / `AssetPlan`，其中 `Asset` 具备 `version` / `parent_id` / `validity` 版本化能力，可作为共享资产基础，**不应再建第二套资产库**。
6. 后端**没有任何鉴权与多租户隔离**（无 `get_current_user`、无 `Authorization` 依赖）。系列权限只能在单用户前提下做服务层校验，不能宣称已实现多用户权限。

---

## 2. 技术栈与运行形态

| 维度 | 现状 |
|---|---|
| 后端框架 | FastAPI >= 0.115，SQLAlchemy 2.0，Pydantic v2，Python >= 3.11 |
| 数据库 | SQLite（WAL 模式 + `PRAGMA foreign_keys=ON`，每个连接设置） |
| 迁移 | Alembic，共 36 个 revision；启动自检自动 `upgrade head`（D-12），失败仅告警不阻断 |
| 任务执行 | 独立 worker 进程 `app/worker/scheduler.py` 轮询 `job` 表；并发 = 可用 ComfyUI 实例数（D-18） |
| 渲染 | ComfyUI 远端（H3 工作流，FLF2V 严格首帧 / Ref2V 参考图），支持 `DLAB_RENDER_MOCK` |
| LLM | OpenAI 兼容接口，单实例或多实例 JSON 配置，密钥仅从环境变量读取 |
| 事件推送 | `GET /api/events` SSE，从 `job_event` outbox 轮询，`event_seq` 全局递增支持断线续传，单连接上限 300 秒 |
| 前端 | Vue 3.5 + TypeScript + Vite 6 + naive-ui + Pinia + vue-router 4 + @tanstack/vue-query + axios |

---

## 3. 目录结构（已核实）

```text
backend/app/
├── api/v1/          21 个路由模块 + settings 健康检查
├── core/            config.py（Settings 集中配置）、database.py（引擎/会话）
├── models/          narrative / production / continuity / task / agent / audio / prop_lifecycle / asset_plan / search
├── schemas/         project / script / storyboard / agent / prompt_compilation
├── services/        40+ 服务（script_parse、asset_plan、intent_router、prompt_compiler、context_manager 等）
├── worker/          scheduler.py、executor.py（91KB）、parse_executor.py、compose_executor.py、export_executor.py
├── integrations/    comfyui.py、comfyui_pool.py、llm.py
├── jobs.py、progress.py
└── main.py          应用装配 + 统一异常格式 + 启动自检

frontend/src/
├── api/             按域拆分（projects/script/assets/storyboard/audio/compose/jobs/tasks/agent/settings/...）
├── components/      10 个通用组件（AppLayout、FlowNav、TaskCard、各类 Panel/Drawer）
├── composables/     useGenerationProgress、useTaskEvents
├── router/index.ts  10 条路由
├── stores/          preferences、project、scriptFlow
├── types/           与后端 schema 对应
└── views/           ProjectListView、DirectorConsoleView、JobsView、SettingsView、project/*（8 个）
```

---

## 4. 数据模型清单（已核实）

### 4.1 叙事域（`models/narrative.py`）

| 模型 | 关键字段 | 约束 |
|---|---|---|
| `Project` | `name`、`status`、`creation_type=novel`、`episode_plan`(JSON)、`current_script_version_id`、`current_parse_version_id`、`assets_review_status`(pending/approved/rejected)、`assets_reviewed_at`、`deleted_at` | 软删除 |
| `ScriptVersion` | `project_id`、`version`、`content_snapshot`、`content_hash` | `UNIQUE(project_id, version)` |
| `ScriptEstimate` | `user_confirmed`、`total_episodes`、`locked_at` | 确认时锁定总集数 |
| `Character` | `name`、`normalized_name`、`portrait_asset_id` | `UNIQUE(project_id, name)` |
| `Scene` | `seq`、`episode`、`normalized_name`（地点+时段）、`baseline_asset_id`、`plate` / `plate_text` | `UNIQUE(project_id, seq)` |
| `SceneEpisode` | `scene_id`、`episode`、`seq_in_episode` | `UNIQUE(scene_id, episode)`，同一地点多集共用一条 scene |
| `Prop` | `name`、`category`（handheld/carried/scene_fixed/shared/key_prop/background）、`is_key_prop`、`anchor_required`、`anchor_asset_id`、`appearance_attrs`(JSON) | `UNIQUE(project_id, name)` |
| `Shot` | `scene_id`、`episode`、`seq`、`keyframe_confirmed`、`status`、`duration` / `actual_duration`、`time_jump`、`requires_prev_frame`、`start_state` / `end_state`(JSON)、`last_frame_asset_id` | `UNIQUE(scene_id, episode, seq)`，索引 `(scene_id, episode)` |
| `EpisodeParseRecord` | `project_id`、`episode_no`、`asset_type`、状态机 `pending→running→succeeded/failed` | `UNIQUE(project_id, episode_no, asset_type)` |

### 4.2 生产域（`models/production.py`）

| 模型 | 关键字段 |
|---|---|
| `Asset` | `project_id`、`asset_type`、`version`、`parent_id`、`file_path` / `thumb_path`、`source_job_id`、`ref_count`、`locked`、`validity`(默认 current)、`input_snapshot`、`quality_status` / `quality_issues`；索引 `(project_id, asset_type, validity)` |
| `AssetRef` | `shot_id`、`asset_id`、`role`、`source_version` |
| `Workflow` / `Job` / `JobEvent` / `JobInputRef` / `DependencyRef` | 工作流定义、任务、事件 outbox（`event_seq`）、幂等输入、依赖 |
| `ParseVersion` / `Composition` / `Export` / `UploadRecord` | 解析版本、合成、导出、上传 |

### 4.3 跨集帧连续性（`models/continuity.py`）

`EpisodeContinuity`：`project_id`、`episode`、`previous_episode`、`next_episode`、末帧来源（`source_composition_id` / `source_video_path` / `source_frame_path` / `source_frame_hash` / `source_frame_timestamp` / `source_video_duration` / `source_video_fps`）、`opening_frame_path` / `opening_frame_asset_id`、`continuity_status`(pending/extracted/passed/failed)、`continuity_version`（上游重合成递增并使旧版本失效）、`continuity_summary`(JSON)、`verification_result`(JSON)、`error_detail` / `fallback_used` / `fallback_reason`。索引 `(project_id, episode)`、`(project_id, continuity_status)`。

> **重要区分**：该表的 `episode` 是「项目内部集号」，语义与系列化中的 `Episode` 实体不同；`continuity_status` 也只描述帧连续校验，**不能复用为剧情一致性状态**。

### 4.4 任务与 Agent

- `Task` / `TaskStage` / `TaskEvent` / `TaskResult`：阶段化任务、进度、事件、结果。
- `AgentSession` / `AgentMessage` / `AgentTask` / `AgentPlanStep` / `AgentConfirmation` / `AgentMemory`：会话、消息、子任务、计划步骤、人工确认、记忆。
- `AssetPlan` / `AssetPlanDependency`：资产生成计划与槽位依赖。
- `PropLifecycleEvent` / `PropShotState` / `PropEpisodeSnapshot` / `CharacterPropBinding`：道具生命周期与角色-道具绑定，**可作为剧情道具状态的局部数据基础**。
- 音频域：`VoiceProfile` / `AudioSegment` / `VoiceLibrary` / `AudioReview`。

---

## 5. 现有生产链路（已核实入口）

```text
剧本（api/v1/script.py + ScriptVersion）
  → 评估（script_evaluation、ScriptEstimate 锁定总集数）
  → 解析（services/script_parse：analyze_script_outline / normalize_parse_output / apply_parse_result；worker/parse_executor；EpisodeParseRecord 记录分集分类型状态）
  → 资产计划（services/asset_plan.build_asset_plans → AssetPlan + 依赖）
  → 资产生成（api/v1/generate.py 触发 Job；integrations/comfyui）
  → 资产审核闸口（services/assets_gate；Project.assets_review_status）
  → 分镜图（Shot.keyframe_confirmed 静图先行闸口，D-13）
  → 分镜视频（worker/executor.py + H3 FLF2V / Ref2V）
  → TTS / 音频（api/v1/audio.py、voice_matching.py，与 H3 解耦）
  → 合成（worker/compose_executor.py；成功后调用 continuity.extract_episode_last_frame）
  → 导出（worker/export_executor.py）
```

事件链路：`JobEvent` 写入 outbox → `GET /api/events` SSE 增量推送（`last_seq` 断线续传，1 秒轮询，单连接 300 秒）。

---

## 6. Agent 体系现状

- `api/v1/agent.py`（约 92KB）提供会话式导演台接口；配套 `AgentSession` / `AgentMessage` / `AgentTask` / `AgentPlanStep` / `AgentConfirmation` / `AgentMemory`。
- `services/intent_router.py` / `intent_resolver.py`：产出 `intent`、`reason`、`risk_level`、`confidence`、`subcategory`、`route_exit`；已存在 `generate_request`、`pipeline_action`、`ambiguous` 等意图，未知意图走澄清。
- `services/pipeline_tools.py` / `regen_tools.py` / `agent_tools.py`：工具声明自带 `permission=read|write|generate`，写操作经 `AgentConfirmation` 人工审批。
- `services/task_orchestrator.py`：阶段调度、失败跳过、进度聚合、Job 事件桥接、孤立任务对账。
- `services/context_manager.py` 是 **Agent 会话上下文**（历史消息/记忆裁剪），与剧情上下文无关。

结论：已具备「意图识别 + 计划确认 + 工具审批 + 异步任务 + 真实进度事件」的骨架，续写功能应接入该骨架，而不是另起一套任务系统。

---

## 7. 前端现状

| 项 | 现状 |
|---|---|
| 路由 | `/director`、`/`（项目列表）、`/project/:id/script`、`/estimate`、`/parse`、`/storyboard-images`、`/storyboard`、`/assets`、`/compose`、`/jobs`、`/settings` |
| 导航 | `AppLayout.vue` + `FlowNav.vue`，以项目为中心的单集流水线 |
| 大文件视图 | `AssetsView.vue` 79KB、`StoryboardView.vue` 77KB、`StoryboardImagesView.vue` 58KB、`ParseView.vue` 40KB |
| 状态 | Pinia（`project` / `scriptFlow` / `preferences`）+ vue-query |
| 进度 | `useGenerationProgress` / `useTaskEvents`，基于 SSE 与任务查询 |
| 系列能力 | 无页面、无路由、无 API 模块、无导航入口 |

---

## 8. 权限与数据安全现状

1. 无用户表、无登录、无 `Authorization` 依赖、无中间件鉴权；全站按单用户本地工具设计。
2. Agent 工具的 `permission` 级别与 `AgentConfirmation` 审批是**操作安全机制**，不是多用户权限模型。
3. 前端隐藏按钮不构成安全边界（与需求 §22 一致）。
4. `DLAB_LLM_API_KEY` 仅从环境变量读取，`core/config.py` 注释明确禁止写入代码与日志。
5. 剧本、外部素材、检索结果均会进入 LLM Prompt，续写上下文必须继续按「不可信内容」处理，禁止让剧本内容改写 Agent 系统规则。

---

## 9. 与系列化需求的差距

| 需求模块 | 现状 | 结论 |
|---|---|---|
| Series | 无 | 新增 |
| Episode（叙事 + 双状态机） | 仅有 `Shot.episode` / `EpisodeContinuity.episode` / `EpisodeParseRecord.episode_no` 的整数集号 | 新增实体，需与项目内部集号显式区分 |
| Series Bible 版本 | 无；`style_config` 是全局风格配置，不是系列设定 | 新增 |
| 共享资产 | `Asset` 以 `project_id` 归属，具备版本化 | 扩展 `series` 关联，复用现有表与 API |
| Story Event Ledger | 无；`PropLifecycleEvent` 仅覆盖道具状态变化 | 新增 |
| 剧情连续性快照 | 无；`EpisodeContinuity` 为视频帧连续性 | 新增，命名必须与帧连续区分 |
| Plot Threads | 无 | 新增 |
| Context Builder | 无；`context_manager` 为会话上下文 | 新增独立确定性服务 |
| 剧本确认门禁 | 有 `ScriptVersion` 不可变快照与 `ScriptEstimate.user_confirmed`，但无「确认即入账」事务 | 扩展 |
| 正式状态提交 | 无 `series_state_version`、无幂等键、无提案表 | 新增 |
| 异步任务与进度 | `Task` / `TaskStage` / `TaskEvent` + SSE 完整 | 直接复用 |
| 人工审批 | `AgentConfirmation` + 资产审核闸口 | 复用并扩展确认类型 |

---

## 10. 命名冲突与兼容风险（实施前必须处理）

1. **Episode 一词已被占用**：`Shot.episode`、`EpisodeContinuity.episode`、`EpisodeParseRecord.episode_no` 均表示项目内部集号。新表建议命名 `series_episodes`，代码与 API 使用 `episode`（系列维度）时必须在文档中标注作用域，避免误写。
2. **历史项目可能包含多集**：`Project.episode_plan` 与 `ScriptEstimate.total_episodes` 表明一个 Project 可覆盖多集。需求要求「一个 Episode 对应一个 Project」，但历史项目不得拆分。建议 `series_episodes` 增加可空字段 `project_episode_no`：新剧集固定为 1；历史项目按实际集号关联，不迁移、不重建、不改 ID。
3. **状态语义不可复用**：`Project.status`（draft/…）不等同于 `script_status` 与 `production_status`，三者必须独立字段。
4. **乐观锁位置**：`Project` 无 `state_version`，系列状态版本应放在 `series` 表，避免污染既有 Project 语义。
5. **资产归属**：`Asset.project_id` 为现有归属键（推断为必填，实施时需以模型定义确认）。共享资产必须新增关联（`series_id` 字段或 `series_assets` 关联表），**禁止复制资产记录**，也禁止仅按名称合并。
6. **前端路由**：现有路由全部以 `project` 为中心，系列页面应新增 `/series`、`/series/:id` 等路由，不改写既有路径，保证旧链接可用。
7. **相似模块误用**：`services/continuity.py`（帧连续）、`services/context_manager.py`（会话上下文）与新模块名称相近，实施时须在新模块 docstring 中写明边界。

---

## 11. 可复用 / 需扩展 / 需新增

### 11.1 可直接复用
- 任务编排与进度：`Task` / `TaskStage` / `TaskEvent` / `task_orchestrator` / SSE `/api/events`。
- 任务执行：`Job` / `worker/scheduler` / `executor` / `compose_executor` / `export_executor`。
- 资产体系：`Asset`（版本化）/ `AssetRef` / `AssetPlan` / `assets_gate`。
- 剧本快照：`ScriptVersion`（内容哈希 + 唯一版本号）。
- 分集解析状态：`EpisodeParseRecord`。
- Agent 骨架：会话、计划、确认、工具权限、意图路由。
- 集成层：ComfyUI 池、LLM 客户端、媒体服务。
- 道具状态：`PropLifecycleEvent` / `PropEpisodeSnapshot` 可作为剧情道具状态的补充数据源。

### 11.2 需扩展
- `Asset`：新增系列归属与跨集使用记录（不改变现有 `project_id` 语义）。
- `ScriptVersion`：新增确认状态与 `context_manifest_id` 关联（或由新表 `episode_script_versions` 承载，需在 Phase 1 决策）。
- `Project`：新增可空 `series_id`，仅用于反查，不作为唯一关联来源。
- Agent 工具：新增续写意图与系列上下文查询工具。
- 前端：新增「系列管理」导航与页面，复用 naive-ui 与现有设计语言。

### 11.3 需新增
`series`、`series_episodes`、`series_bible_versions`、`story_events`、`continuity_snapshots`、`continuity_change_proposals`、`plot_threads`、`episode_context_manifests`、`series_assets`（或 `asset.series_id`）、`episode_script_versions`（若不复用 `ScriptVersion`）；对应服务：`SeriesService`、`EpisodeService`、`AssetRegistryService`、`ContextBuilderService`、`StoryEventService`、`NarrativeContinuityService`、`StateCommitService`；对应 Agent：续写编排、事件抽取、连续性检查、资产匹配。

---

## 12. 迁移与兼容策略

1. **只增不改**：全部使用新增表或可空新列，禁止修改既有列类型、禁止重建表、禁止删除历史数据。
2. **单 head 线性迁移**：新增 revision 的 `down_revision` 必须以 `alembic heads` 实测结果为准，不凭记忆填写。
3. **SQLite 约束**：新增列一律可空或带默认值；批量回填分批执行；不改主键。
4. **回滚**：每个 revision 提供 `downgrade`（drop 新表 / 新列）；回填数据不自动回滚。
5. **备份**：迁移前执行 `cp data/director_lab.db data/backups/director_lab.db.bak_series_<ts>`，WAL 模式下同时备份 `-wal` / `-shm` 或先 checkpoint（实施时确认）。
6. **启动即迁移**：`main.py` 启动自动 `upgrade head`（D-12），因此新迁移必须与旧代码兼容（旧代码忽略新表即可），避免同一发布窗口内新旧进程混跑出错。
7. **API 兼容**：现有 `/api/projects/*` 全部保持路径与响应不变；系列接口使用新前缀，不复用同名路径。
8. **旧项目不变**：不默认把历史项目合并为同一个 Series；提供显式「系列化」入口，由用户选择。

---

## 13. 风险清单

| 编号 | 风险 | 影响 | 应对 |
|---|---|---|---|
| R1 | Episode 语义冲突 | 数据错关联、上下文串集 | 表名 `series_episodes`，全链路显式命名 |
| R2 | 历史多集项目无法一一拆分 | 迁移破坏历史数据 | `project_episode_no` 兼容字段，不拆分 |
| R3 | 无鉴权体系 | 无法实现真实多用户权限 | 沿用单用户前提，只做服务层校验，文档明示 |
| R4 | 上下文成本与噪声 | Prompt 超限、剧情漂移 | Context Builder 白名单 + 预算裁剪 + Manifest 记录实际使用 |
| R5 | SQLite 并发写 | 状态提交锁竞争 | 单事务 + 乐观锁 + 幂等键，避免长事务 |
| R6 | 重复提交事件 | 事件重复入账 | 唯一键 `(episode_id, script_version_id, event_key)` + 幂等表 |
| R7 | 与现有 continuity/context_manager 混淆 | 误用错误状态源 | 新模块命名加 `narrative_` / `story_` 前缀并写清边界 |
| R8 | 前端单文件体积大 | 改动回归面广 | 新页面独立文件，不改写既有视图 |
| R9 | 测试基线 40+ 文件 | 回归遗漏 | 每阶段执行全量 pytest，记录结果 |
| R10 | 同名资产误合并 | 角色被拆分或合体 | 必须走身份描述 + 人工确认，禁止仅按名称 |
| R11 | 迁移自动执行 | 线上启动即改结构 | 迁移幂等、可 downgrade、迁移前备份 |

---

## 14. 下一步

1. 输出 `01-implementation-plan.md`（数据库变更、后端模块、前端页面、API、Agent 工作流、异步任务、迁移、测试、优先级）。
2. Phase 1 仅新增表与基础 CRUD，不改动现有生产链路，并以 `pytest` 全量回归确认旧功能未受影响。
3. 每阶段完成后在本目录追加实施记录，最终汇总为 `09-implementation-report.md`，严格区分已完成 / 部分完成 / 未实现。
