# Director-Lab 重构执行进度

更新日期：2026-11-22

状态口径：`已完成` 必须有代码与测试证据；`进行中` 表示已有实施但未验收；`未开始` 不代表仓库完全没有相关历史能力。

## 总览

| Phase | 状态 | 当前证据/下一步 |
|---|---|---|
| 0 源码审计与基线 | 已完成 | 审计报告已落盘；后端、Ruff、前端基线已实测 |
| 1 Script Intelligence 拆分 | 进行中 | 第一切片：原文区块索引；第三切片：结构分析契约、证据校验与只读 Shadow Run；语义分析仍需外部文档 |
| 2 Storyboard Planning | 进行中 | 第一切片：规划契约校验、Shot 兼容 dry-run Adapter、覆盖/顺序/锁定冲突检查；第二切片：显式 `apply` 安全 upsert 写入 Shot；第三切片：看板「分镜规划」导入入口（校验预览 + 显式写入）；第四切片：写入前逐镜头字段差异预览（只读）；规划自动生成接入未做 |
| 3 Dependency/Snapshot | 进行中 | 只读图查询与诊断；单节点影响预演已接入镜头/资产详情；项目级待处理总览已接入分镜看板；批量出图前已接依赖预检；生成快照已可只读查看并提示参考图版本差异；资产变更影响已有独立只读列表页；快照支持以历史版本为基准重做（仅传 `base_asset_id`）；工作流版本影响只读预检已接入资产变更影响页与看板角标；失效标记写路径已接入（复用 `DependencyRef.validity`，用户显式确认后写入）；不新建依赖表 |
| 4 Reference Binding | 已有实现，溢出阻断已验证 | 七角色超六槽回归证明 Resolver 留痕、Compiler 报错、生成硬阻断 |
| 5 Prompt/Workflow Compiler | 已有实现，未改 | 保持 D-24 单一编译层 |
| 6 分镜图生产 | 已有实现，未改 | 保留单有效版本，不实施 Plan 通用候选图库建议 |
| 7 Storyboard Board | 进行中 | 镜头详情只读影响面板 + 生成快照查看 + 看板级「依赖影响」卡 + 镜头卡片依赖影响常驻角标 + 工作流版本影响角标 + 分镜图批量出图前依赖预检 + 按依赖状态筛选 + 资产变更影响列表页（手动触发，不自动重做）+ 快照历史版本一键进入分镜图工作台重做 |
| 8 连续性与视频依赖 | 已有实现，未改 | 不改 H3/Ref2VA/TTS/合成导出约定 |
| 9 Job/Event/Recovery | 已有实现，未改 | 不新增任务框架 |
| 10 前端全流程体验 | 进行中 | 依赖影响预演已接入镜头/资产详情、看板卡片标记、分镜图批量出图前预检与资产变更影响列表页；镜头详情可查看生成快照，并可一键带历史版本进入分镜图工作台重做；看板镜头卡片新增工作流版本影响角标；均由真实接口驱动，不做虚假状态 |
| 11 DB/API 兼容 | 进行中 | 第一批 additive API，无 DB 迁移 |
| 12 E2E 与验收 | 进行中 | mock 与真实环境验证分开记录；样例 A（短剧本快速路径）、样例 B（中途失败 → 显式重试）、样例 C（多集复杂路径）已落地；测试矩阵与人工验收清单已落盘 `director_lab_refactor_test_plan.md`；真实图片链路已实测通过（4/4 任务成功，产物落盘），视频/TTS/BGM 被模型与自定义节点缺失阻塞；解析页客户端跳转白屏与「画面」列取数已修复；分镜图阶段放宽为「≥1 张即可进入」；`/api/health` 改为读取 worker 心跳并返回 `ok/stale/not_running/unknown`；分镜图 6/6 实测为单帧（修复镜头编号被误读为「第 1 到第 6 镜」导致的多格拼图）；道具锚定图改为单视图；后端全量 1339 passed |

## Phase 0 验收记录

- Git：`main@aee23cf`，审计开始时无未提交改动。
- 后端：`1281 passed, 7 failed, 92 warnings`。
- 前端：类型检查及生产构建通过。
- Ruff：既有全仓问题未清零，不作为本次重构通过项。
- 真实 ComfyUI/GPU：未验证。
- 详细代码位置、工作流槽位和既有失败见 `director_lab_refactor_audit.md`。

## 基线失败清偿（2026-11-22）

审计基线记录的 7 项后端失败已全部定位并修复，全量后端测试归零：

- 全量复核：`1308 passed, 92 warnings`（656.44s），无 failed、无 error。
- 较基线 `1281 passed / 7 failed` 净增通过数，含本轮新增切片测试。
- 前端本轮未改动，沿用既有构建通过结论。

### 失败归因与修复

| # | 失败项 | 根因 | 修复方式 |
|---|---|---|---|
| 1-2 | `test_project_purge_fk_coverage.py` 两例 | 测试种子插入 `Shot` 时未给非空无默认值的 `shot_kind`、`duration`；补齐后又暴露 `StoryboardRegenConfig.created_at` 同为必填 | 只补测试种子数据，与生产两处创建点显式传 `created_at` 的既有约定对齐；不改模型、不加默认值 |
| 3 | `test_prompt_compiler.py` 模板残留占位符 | 分镜图模板 v2 新增 `action_moment`、`aspect_desc`、`ref_duty_block` 三个占位符，测试 mapping 未覆盖，`render_template` 抛 `prompt_template_missing_field` | 补齐三个字段映射，不放宽 `render_template` 的缺字段校验 |
| 4 | `test_prompt_docs_sync.py` 文档同步 | `docs/prompts/compile_storyboard_image.md` 仍是旧版文案，与运行时常量不一致 | 以 `workspace/prompts/` 外置 v2 文件为真值同步文档（`prompt_store.get_prompt` 外置优先、常量为回退），不改模板实现 |
| 5-6 | `test_prompt_empty_fill.py` 两例 | 用例依赖真实 `data/media` 资产文件与资产质检状态，环境缺失即触发 `reference_asset_file_missing` / `reference_asset_quality_failed` 并 409 硬阻断 | 新增 autouse fixture 隔离 `_media_file_exists`；`ctx` fixture 内会话级把质检 failed 资产置 pending 并 rollback；断言由旧模板分段改为 v2 分段 |

### 变更文件

```
backend/tests/test_project_purge_fk_coverage.py   +2 -2
backend/tests/test_prompt_compiler.py             +4 -0
backend/tests/test_prompt_empty_fill.py           +19 -2
docs/prompts/compile_storyboard_image.md          +13 -12
```

4 个文件、37 insertions / 17 deletions，全部为测试与文档改动，未触碰生产代码、模型、迁移、API 与工作流 JSON。

### 回滚

```bash
git checkout -- backend/tests/test_project_purge_fk_coverage.py backend/tests/test_prompt_compiler.py backend/tests/test_prompt_empty_fill.py docs/prompts/compile_storyboard_image.md
```

### 未验证项

- 真实 ComfyUI/GPU 生成、H3/Ref2VA、TTS、合成导出仍未实机验证。
- 修复只做测试侧隔离，未验证真实 `data/media` 目录下参考图文件与资产质检状态是否合法；该验证需在有真实资产的环境执行。
- Ruff 全仓历史问题按原口径不清零，不作为本次通过项。

## 当前实施清单

### Slice 1：原文溯源（Phase 1）

- [x] 确定性原文区块索引，保留原文及来源版本。
- [x] 稳定区块 ID、顺序、字符范围、类型与证据文本。
- [x] 证据 ID/范围/文本校验，拒绝跨版本或伪造引用。
- [x] additive 查询与校验 API，无 DB 迁移。
- [x] 聚焦单元/API 测试：`tests/test_source_index.py`，5 passed。

### Slice 2：统一依赖只读图（Phase 3）

- [x] 聚合 `DependencyRef`、`AssetRef`、`AssetPlanDependency`、`JobInputRef`。
- [x] 明确“上游依赖 → 下游受影响对象”的统一方向。
- [x] 支持前置查询、影响遍历、环检测和断链/失效诊断。
- [x] additive 查询 API，已注册到 `app/main.py`，跨项目记录隔离。
- [x] 聚焦单元/API 测试：`tests/test_dependency_graph.py`，3 passed；与原文溯源合计 8 passed，新增文件 Ruff 通过。

### Slice 3：结构分析与 Shadow Run（Phase 1）

- [x] 从原文索引确定性派生集/场景/Beat 骨架与实体提及，不调用语言模型、不写库。
- [x] 场景标题保守切分地点/时段/内外景；无法判断的字段保持 None，不做语义猜测。
- [x] 分析对象（`aep_`/`asc_`/`abt_`）ID 由算法、剧本版本与区块派生，同输入同结果。
- [x] 外部分析文档校验：分区完整性、稳定 ID 唯一、Beat→场景归属、原文证据存在性、`origin=source` 必须有证据、风险级别和数量上限。
- [x] 只读 Shadow Run：结构分析与现有 Scene/Shot/Character/Prop 并行比较，输出场景/角色/道具差异、未关联引用与计数，不覆盖生产数据。
- [x] additive API：`GET .../analysis/shadow-run`、`POST .../analysis/validate`，跨项目与缺失版本返回 404。
- [x] 聚焦测试：`tests/test_script_analysis.py`，5 passed；新增文件 Ruff 通过。
- [ ] 分析结果持久化（需后续 DB 迁移）与 Story/Scene-Beat 语义分析（需外部模型）未实施。

### Slice 4：分镜规划 Adapter（Phase 2）

- [x] 规划文档契约校验：稳定镜头 ID、场景归属、序号唯一/连续、时长上限、`shot_kind` 枚举、画面描述、原文证据。
- [x] 资产引用使用真实 ID 并校验项目归属与类型（角色/道具）；场景 ID 必须存在。
- [x] 覆盖检查：场景覆盖率、Beat 覆盖率、对白/旁白区块未覆盖告警、场景回跳告警。
- [x] 锁定保护：已确认镜头只报告 `locked_shot_modified` / `sequence_conflict`，Adapter 明确 `writable=false`，不写 Shot。
- [x] Shot 兼容 Adapter dry-run：映射现有字段；`image_prompt` 仅在规划显式提供时透传，Prompt 仍由生产编译层生成。
- [x] additive API：`POST .../storyboard-plan/validate`，跨项目与缺失版本 404。
- [x] 聚焦测试：`tests/test_storyboard_plan.py`，4 passed；与结构分析合计 9 passed；新增文件 Ruff 通过。
- [ ] 规划自动生成、持久化与重规划写入未实施（保持现有 parser/Shot 生产路径不变）。

### Slice 5：依赖变更影响预演（Phase 3）

- [x] 资产按「类型 + 名称」识别血缘，用边缘记录的上游版本与血缘当前版本比较，判断下游是否使用旧版本。
- [x] 下游分类：已有输出且输入过期 → `redo_required`；尚无输出 → `not_generated`；镜头绑定旧资产 → `rebind_required`；链路传播上游重做状态。
- [x] 输出受影响对象、最短路径、触发边、过期输入明细与汇总计数。
- [x] 只读策略显式声明 `auto_regenerate=false`、`mark_stale=false`、`delete_existing=false`、`writes=false`。
- [x] additive API：`GET .../dependency-graph/{node_type}/{node_id}/impact-preview`；未知/跨项目节点 404，无下游节点返回空影响而非报错。
- [x] 聚焦测试：`tests/test_dependency_impact.py`，2 passed；四个切片联合回归 14 passed；新增文件 Ruff 通过。
- [ ] 自动标记失效、重规划写入与前端影响范围展示未实施。

### Slice 6：依赖影响预演接入前端（Phase 3 / 7 / 10）

- [x] 新增 `frontend/src/api/dependencies.ts`，封装 `GET .../dependency-graph/{node_type}/{node_id}/impact-preview`，类型与后端返回结构一一对应。
- [x] 新增共用组件 `frontend/src/components/ui/DependencyImpactPanel.vue`：手动触发查询、按层级展示下游对象、状态标签（需重生成/需重绑定/未生成/已最新）、过期输入原因与只读策略说明。
- [x] 接入镜头详情（`ShotDetailModal.vue`，`node_type=shot`）与资产详情（`AssetDetailModal.vue`，`node_type=asset`）。
- [x] 组件默认不自动请求，切换节点时清空旧结果，避免打开详情即打一次依赖图或展示上一个节点的数据。
- [x] 仅消费后端只读接口，前端不推断依赖、不提供自动重做/删除入口，与后端 `auto_regenerate=false / writes=false` 策略一致。
- [x] 批量操作影响范围预览已由 Slice 9 落地（分镜图批量出图前预检）。
- [ ] 生成快照查看页未实施（JobInputRef / input_snapshot 只读展示待后续切片）。

### Slice 7：项目级依赖待处理总览（Phase 3 / 7）

- [x] 后端新增 `preview_project_impact`：以「所有已过期/版本不一致依赖的上游」为变化源，一次算出全项目待处理对象。
- [x] 抽出 `_collect_affected` / `_summarize` / `_READ_ONLY_POLICY`，与单节点预演共用同一套分类与只读策略，避免两份逻辑漂移。
- [x] additive API：`GET .../dependency-graph/impact-preview`；返回 `changed_roots` 与受影响清单；未知项目 404。
- [x] 前端新增 `DependencyImpactOverview.vue`，只看 `redo_required` / `rebind_required` / `stale_reference` 三类待办，默认收起，无待办时只显示「未影响任何下游产物」。
- [x] 接入分镜看板第三张卡「依赖影响」；手动触发，不自动请求、不自动重做。
- [x] 测试：`tests/test_dependency_impact.py` 新增项目级用例（聚合、分类、只读性、404），6 passed。

### Slice 8：依赖状态进入看板镜头卡片常驻标记（Phase 3 / 7 / 10）

- [x] `DependencyImpactOverview.vue` 新增 `loaded` 事件，把项目级预演原始数据抛给页面，不重复请求。
- [x] `StoryboardView.vue` 消费 `loaded`：镜头节点直接映射；job 等产物节点通过 `path` 中的 `shot:` 键回映射到镜头，`redo_required` 优先于其他状态。
- [x] 受影响镜头卡片缩略图右上角常驻警示角标（`--state-warning` / `--state-danger`，无硬编码色值），hover 提示具体原因（需重做 / 需重绑定 / 引用失效）。
- [x] 保持手动触发口径：仅在卡三点击「检查依赖影响」后出现并随每次检查刷新；不自动请求、不推断依赖、无自动重做入口。
- [x] 前端验证：`npm run build`（含 `vue-tsc -b`）通过；`check-colors.mjs` 0 处硬编码色值、`check-contrast.mjs` 全部令牌达标。
- [x] 后端回归：`tests/test_dependency_impact.py`、`tests/test_dependency_graph.py` 6 passed，接口契约未变、无后端改动。

### Slice 9：分镜图批量出图前依赖影响预检（Phase 3 / 7 / 10）

- [x] 把「预演结果 → 镜头状态」映射抽为共享的 `buildShotImpactMap` / `IMPACT_SHOT_LABEL`（`frontend/src/api/dependencies.ts`），`StoryboardView.vue` 改为复用，看板角标与批量预检使用同一口径。
- [x] 分镜图页新增「依赖预检」入口，手动触发项目级 `GET .../dependency-graph/impact-preview`；只读、不写库、不自动重做。
- [x] 勾选镜头与预检结果求交：显示「所选 N / M 个镜头受上游变化影响」，并标注检查时刻，避免把旧结果当当前状态。
- [x] 批量生成前若勾选镜头受影响，先弹确认框逐条列出受影响镜头（最多 6 条，其余合并计数），确认后才提交；取消则不提交。
- [x] 预检失败不阻断批量生成，但明确提示本次未做依赖影响检查，不伪造「无影响」。
- [x] 无自动请求：仅在点击「依赖预检」或提交批量生成时请求一次；结果保留到下次预检。

### Slice 10：生成快照只读查看（Phase 3 / 7 / 10）

- [x] 复用既有 `GET /api/jobs/{job_id}/snapshot`（已返回 `snapshot` 编译快照、`refs` 参考图索引与 `frozen`），未新增重复路由。
- [x] `GET /api/jobs` 增加可选 `shot_id` 过滤（`app/api/v1/jobs.py`），使镜头详情能只取本镜头任务，不必拉全项目再筛；`total` 随过滤条件收敛。
- [x] 新增 `JobSnapshotPanel.vue`：手动点击「加载快照」才请求；下拉切换该镜头历次任务，展示模式、快照哈希、冻结时间、提示词、模型参数、参考图（槽位 / 资产类型 / 版本 / 必需 / 未注入）与工作流节点数。
- [x] 接入镜头详情 `ShotDetailModal.vue`，切换镜头即清空上一个镜头的快照，避免错位展示。
- [x] 只读口径：不提供「按快照重跑 / 改依赖」入口；快照为历史记录，不随当前资产变化，重做仍走既有重生成链路。
- [x] 早期任务无快照时显示明确空态，不伪造数据。
- [x] 测试：`tests/test_jobs_list_filter.py` 2 passed（按镜头过滤、与项目过滤取交集）。

### Slice 11：快照参考图版本比对（Phase 3 / 7）

- [x] `GET /api/jobs/{job_id}/snapshot` 的每条参考图新增 `current_version` 与 `version_stale`：批量查询资产当前版本，与快照冻结版本比对。
- [x] 只报告差异：资产已删除时 `current_version=null`、`version_stale=null`（无法判断）；不标记失效、不重生成、不改资产版本与快照记录。
- [x] `JobSnapshotPanel.vue` 展示「快照 v2 → 当前 v3」与「资产已删除」标签，标题汇总「N 张已非当前版本」，并在底部说明不会自动重做。
- [x] 测试：`tests/test_job_snapshot_versions.py` 2 passed（版本差异与删除资产、只读性）。

### Slice 12：看板按依赖状态筛选（Phase 3 / 7 / 10）

- [x] `StoryboardView.vue` 在卡三返回检查结果后显示「依赖影响」筛选条：全部镜头 / 仅受影响（N）。
- [x] 筛选只影响显示：按 `shotImpactByShotId` 过滤镜头卡片并隐藏无受影响镜头的场景；场景标题显示「受影响 / 总数」。
- [x] 无检查结果时不显示筛选条；新一轮检查若无受影响镜头则自动回到「全部镜头」，避免空白看板被误读。
- [x] 不本地推断依赖、不改选中集合、不触发任何请求或重做，数据仍只来自卡三手动触发的项目级预演。

### Slice 13：资产变更影响列表页（Phase 3 / 7 / 10）

- [x] 新增 `frontend/src/views/project/AssetImpactView.vue`，路由 `/project/:id/assets/impact`，资产库页提供「查看资产变更影响」入口。
- [x] 复用项目级 `GET .../dependency-graph/impact-preview`，按变化源（上游资产）分组，只列 `redo_required` / `rebind_required` / `stale_reference` 三类待处理对象。
- [x] 每行展示层级、节点类型、名称（资产经 `listAssets` 全量索引还原为「类型 + 名称」）、状态标签与过期输入来源；资产索引加载失败时退化为 ID，不阻断列表。
- [x] 顶部概览展示变化源数与三类计数，并显示最近检查时刻与只读策略说明；无待办时显示明确空态。
- [x] 依赖状态与节点类型文案抽到 `api/dependencies.ts` 的 `IMPACT_STATUS_LABEL` / `IMPACT_NODE_TYPE_LABEL`，与 `DependencyImpactOverview.vue` 共用，消除重复常量。
- [x] 顺带修正 `DependencyImpactOverview.vue` 中未定义的 `--state-error` 令牌为 `--danger-text`。
- [x] 只读：进入页面即检查一次（用户显式导航），不标记失效、不删除产物、不提供自动重做入口。

### Slice 14：以历史版本为基准重做（Phase 3 / 7 / 10）

- [x] 后端快照接口 `GET /jobs/{job_id}/snapshot` 新增 `result_asset`（`asset_id` / `asset_type` / `version` / `validity`），由 `Asset.source_job_id` 反查该任务产出的资产版本；无产物时为 `null`。
- [x] 快照面板（`JobSnapshotPanel.vue`）在分镜图任务且有产出资产时显示「以 vN 为基准重做」，事件只携带 `shotId / assetId / version`。
- [x] 看板（`ShotDetailModal.vue`）转发 `rebase` 事件；`StoryboardView.vue` 跳转 `storyboard-images` 并携带 `?shot=&base=&basev=`，关闭当前弹窗。
- [x] `StoryboardImagesView.vue` 消费 query：先按 `getShot` 确定镜头所属集数，切集后等该集镜头加载完成再打开详情；打开后 `router.replace` 清除 query，避免刷新重复弹出。
- [x] `useKeyframeRegen.ts` 新增 `regenBaseAsset` 状态：`openDetail(shot, base?)`、config 请求传 `base_asset_id`、预检与提交 payload 均带 `base_asset_id`；无当前分镜图但有 base 时仍载入该版本配置。
- [x] `ShotDetailDialog.vue` 的 `open(shot, base?)` 透传 base，并新增提示条：提示词/工作流来自该版本，参考图按当前镜头定义重新解析，不复用快照里的参考图版本；无当前分镜图时用 base 资产作预览。
- [x] 不改 `build_base_config` 的既有决定：参考图始终以当前 `AssetPlanDependency` 覆盖历史快照 refs（修复「提示词两个角色、参考图只有一个槽位」），因此不提供「按快照参考图重做」。
- [x] 不新增任务/快照重放写路径，不自动重做；仅在用户显式点击后跳转并预填工作台，提交仍需用户确认。

### Slice 15：分镜规划安全写入（Phase 2）

- [x] 新增 `POST /api/projects/{project_id}/script-versions/{script_version_id}/storyboard-plan/apply`，复用既有规划校验契约；只有零 error 时才写入。
- [x] 写入语义为安全 upsert：按 `shot_id` 复用既有镜头，新镜头使用规划提供的稳定 ID 创建（`status=pending`，`episode` 取场景集数）。
- [x] 只写规划显式提供的字段（sequence/duration/shot_kind/shot_type/机位/运镜/dialogue/角色/道具/节拍/起止状态/声音/`image_prompt` 等）；缺省字段保留既有值，空 `image_prompt` 不覆盖，避免清空生产层已编译提示词。
- [x] 锁定镜头保护：`keyframe_confirmed` 镜头的 `seq/duration/shot_kind` 不被覆盖，写入结果列入 `skipped_locked` 并返回字段差异。
- [x] 不删除规划未包含的旧镜头，列入 `orphan_shot_ids`；如需删除由后续显式切片处理。
- [x] 写入前统一预检：镜头 ID 合法性/跨项目冲突、镜头换场、与保留镜头的 `(scene, episode, seq)` 冲突；冲突返回 409 且零写入。
- [x] 序号变更采用两阶段写入（先负序号、后最终值），支持同场景内换序而不触发唯一约束。
- [x] 不重建资产计划、不重算依赖、不触发任何生成任务；规划生成接入留待后续切片。

### Slice 16：分镜规划导入入口（Phase 2 / Phase 10）

- [x] 新增 API 客户端 `storyboardPlan.ts`：校验/写入类型与 `validateStoryboardPlan`、`applyStoryboardPlan`。
- [x] 新增 `StoryboardPlanImportDialog.vue`：粘贴规划 JSON → 客户端 JSON 校验 → 服务端只读校验预览 → 通过后才允许写入。
- [x] 预览展示：错误/警告数量、场景与节拍覆盖率、镜头数与总时长、复用/锁定冲突/规划外旧镜头列表、逐条 issue（路径 + 说明）。
- [x] 写入结果展示：新增/更新/无变化/跳过锁定/规划外旧镜头，以及服务端 notes；写入成功后触发看板场景与镜头刷新。
- [x] 入口放在分镜看板「分镜规划」卡，明确提示「校验只读；写入不覆盖锁定镜头、不删除旧镜头、不触发生成」。
- [x] 剧本版本由 `GET /projects/{id}/script-versions/current` 实时解析，无版本时阻止校验并提示先保存剧本。
- [x] 不新增后端接口、不改生成链路；仍为显式人工操作。

### Slice 17：工作流版本影响预检（Phase 3 / 7 / 10）

- [x] 新增只读服务 `workflow_impact.py`：比对已成功任务的 `job.workflow_version` 与 `workflow.version`，输出 `workflow_changed` / `workflow_disabled` / `unknown_workflow_version` 分类与项目级汇总。
- [x] 新增 `GET /api/projects/{project_id}/workflow-impact`：仅读，无写库、无失效标记、无重生成；项目不存在返回 404。
- [x] 影响项包含镜头定位（集/镜）、工作流名与版本变化（执行时 → 当前）、最新产出资产摘要；按集/镜排序。
- [x] 前端 `dependencies.ts` 增加类型与 `getWorkflowImpact`；资产变更影响页新增「工作流版本影响」卡，与资产级影响并列展示。
- [x] 空状态口径调整为「无资产影响且无工作流影响」才显示；工作流预检失败不阻断资产影响主列表。
- [x] 不改生成链路、不自动重做；是否重做仍由用户在分镜看板显式触发。

### Slice 18：工作流版本影响看板角标（Phase 3 / 7 / 10）

- [x] 分镜看板接入 `getWorkflowImpact`（TanStack Query），按 `shot_id` 派生工作流影响映射。
- [x] 镜头缩略图左下角新增文字型角标「工作流已更新」/「工作流已停用」，悬停显示工作流名与 `v执行时 → v当前`。
- [x] 同镜头多条任务时停用优先于版本变化；无镜头绑定的任务不进角标，避免误标。
- [x] 纯只读提示：不自动重做、不标记失效；重做仍走既有生成入口。
- [x] 与左上角依赖影响角标、右下角生产状态徽章分位置展示，避免遮挡。

### Slice 19：依赖失效标记写路径（Phase 3 / 6.4）

- [x] 新增 `dependency_mark.py`：用户确认后把来源下游仍引用旧版本的 `DependencyRef` 边置为 `validity="stale"`；不重生成、不删除产物、不改资产有效性。
- [x] 资产按同名版本序列处理：引用旧版本资产的边全部标记；当前版本边保持 `current`；已失效边幂等跳过。
- [x] 非资产来源（如镜头）无版本概念，标记其全部 `current` 边。
- [x] 新增 `POST /api/projects/{project_id}/dependency-graph/{node_type}/{node_id}/invalidate`；节点不属于本项目返回 404。
- [x] 项目级影响预演的 `changed_roots` 增加只读字段 `markable_edge_count`，前端仅对存在可标记边的根显示「标记失效」。
- [x] 资产变更影响页新增「标记失效」操作（确认框列明可标记边数/下游数/不重生成），写入后刷新列表。
- [x] 写后读路径联动：依赖图边 `validity=stale`，影响预演出现 `dependency_invalid` 诊断。

### Slice 20：分镜规划写入前字段差异预览（Phase 2 / Phase 10）

- [x] 校验接口的 `comparison` 新增只读 `field_changes`：逐镜头列出写入将变化的字段与 `from → to` 值。
- [x] 差异口径与 `apply` 实际写入一致：只比较规划显式提供的字段（`_write_fields`），未提供的字段不进入差异；值相同不列出。
- [x] 锁定镜头若 `seq/duration/shot_kind` 变化，标记 `will_skip=true`，与 `skipped_locked` 判定一致，避免「预览说会改、写入却跳过」。
- [x] 导入弹窗新增「写入将改动 N 个既有镜头的字段」区块，字段名转中文、长文本截断、空值显式标注为「空」。
- [x] 只读：校验阶段不写库；仍须先校验通过才能写入，写入语义未变。

### Slice 21：E2E 样例 A「短剧本快速路径」（Phase 12）

- [x] 新增 `backend/tests/test_pipeline_e2e.py`：剧本版本 → 大纲评估 → 解析任务 → 实体落库 → 公共资产生成与审核 → 分镜规划只读校验 → 分镜图 → 视频 → 只读影响预检。
- [x] LLM 与渲染全部替身：解析用假 `LLMClient`，场景板 LLM 编译替换为抛错走规则兜底，渲染走 `render_mock` + 临时 `media_dir`，不触达外网、不写真实媒体目录。
- [x] 断言主链路：角色/道具/场景/镜头落库数量与字段正确；公共资产审核后分镜图可生成；视频依赖分镜图就绪；clip 资产落库并回填 `source_job_id`。
- [x] 断言只读性：规划校验 dry-run 前后镜头数不变；影响预检前后任务数与镜头快照完全一致。
- [x] 边界：真实 ComfyUI/GPU 验收不在本测试范围，需按人工验收清单单独执行并记录。

### Slice 22：E2E 样例 B「中途失败 → 显式重试」（Phase 12）

- [x] `test_pipeline_e2e.py` 新增失败重试用例：mock 渲染首次抛错 → 任务 `failed`、`error_code=mock_render_failed`、`retryable=true`、无资产落库。
- [x] 显式重试：`POST /api/jobs/{id}/retry` 生成新任务（`queued`、继承 `attempts=1`），执行后 `succeeded` 且 `attempts=2`。
- [x] 断言重试只落一份资产，且 `source_job_id` 指向新任务；原失败任务保持 `failed`，不被覆盖。
- [x] 边界：不自动重试、不清理失败任务；重试由用户显式触发，符合既有任务语义。

### Slice 23：E2E 样例 C「多集复杂路径」（Phase 12）

- [x] `test_pipeline_e2e.py` 新增复杂路径用例：两集 / 三场景 / 双角色 / 双道具 / 6 镜头，覆盖解析落库、公共资产、场景批量分镜图、同集链式尾帧与跨集拦截。
- [x] 解析断言：2 角色、2 道具、3 场景（`(1, 教室) / (1, 走廊) / (2, 屋顶)`）、6 镜头（3 / 1 / 2）。
- [x] 公共资产：双角色定妆图、三场景基准图、双道具锚定图全部经审核闸口后生成。
- [x] 场景批量分镜图：教室 3 镜走 `POST /api/scenes/{id}/generate`，`count=3` 且逐任务成功；走廊 / 屋顶单镜单独生成。
- [x] 同集链式尾帧：教室第 1 镜 clip 后 `last_frame_asset_id` 非空；第 2、3 镜 `prev_frame.injected=true`，`prev_shot_id` / `frame_asset_id` 严格指向前一镜实际尾帧。
- [x] 集首镜口径：第 1 集首镜 `prev_frame.injected=false`、`reason=first_shot_handled_by_continuity`，不错误引用同集其他镜头。
- [x] 跨集拦截：第 1 集未合成、无连续性记录时，第 2 集首镜生成返回 422 `continuity_check_failed`，不创建任务、不写尾帧，避免跨集串帧。
- [x] 只读预检：依赖图影响预演与工作流影响接口仍返回 200，镜头数不变。
- [x] 边界：跨集正向衔接（合成 → 末帧提取 → 首帧注入）已由 `tests/test_continuity.py` 的六条用例覆盖；本 E2E 只验证生成入口在缺少连续性记录时的显式拦截，不重复合成链路。

### Slice 24：分镜图多格拼图修复与真实图片链路复验（Phase 6 / 12）

- [x] 根因定位：`workspace/prompts/image_prompt.md` 首行渲染为「生成第 1-6 镜」，被模型理解为「第 1 到第 6 镜」而输出多格竖条拼图；改为「生成第 {episode} 集第 {seq} 镜的单帧分镜图（只画这一个镜头的一个瞬间）」。
- [x] 同步 `script_parse.py` 的 `image_prompt` / `video_prompt_first` / `video_prompt_body` 默认模板与 `workspace/prompts/video_prompt_*.md`，统一为「第 {episode} 集第 {seq} 镜」，避免图像与视频提示词口径漂移。
- [x] 道具锚定图改为单视图：`PROP_TEMPLATE` 删除「并排展示正视与侧视」，`_DUTY_LINE["prop"]`、`prompt_qc.py` 的 `_ROLE_KEEP["prop"]`、`docs/prompts/compile_prop_asset.md` 同步，模板版本 `TEMPLATE_PROP` 1→2、`TEMPLATE_STORYBOARD` 2→3；避免下游分镜图复制双视图分格排版。
- [x] 资产质检误报修复：`image_quality.py` 增加外接框宽高均 ≥2 格的约束（`_SKIN_MIN_BLOCK_SIDE=2`），排除书口等 1 格宽细条被误判为人物肤色块。
- [x] 真实复验（项目 `prj_a10bc7d70728`，5090 实例）：道具锚定图重生成 19s 单视角干净；分镜图逐镜重生成后 6/6 为单帧、`validity=current`、`quality_status=passed`，产物位于 `backend/data/media/storyboards/storyboard_{1..6}_img.png`。
- [x] 测试同步：`test_asset_prompt_hardening.py` 断言改为单视图口径，`test_two_stage_parse.py` 断言改为「第 1 集第 1 镜」，`test_scene_cleanliness.py` 新增细条不误判用例；后端全量 1339 passed（322.09s）。
- [x] 未验证项：视频 / TTS 真实链路仍被远端模型缺失阻塞（需 45.4 GiB，远端可用 26G），见 `director_lab_refactor_test_plan.md` §5.5。

### 基线失败清偿（Windows 路径断言）

- [x] `tests/test_shot_chain.py::test_resolve_prev_frame_returns_dict_when_asset_exists` 在 Windows 上失败：`resolve_prev_frame` 有意把路径分隔符归一为 `/`（修复历史 `ast_frames\...` 反斜杠路径导致 `os.path.exists` 恒 False），测试仍按原生 `str(path)` 断言。
- [x] 修正测试断言为归一化后的路径，不改生产逻辑；`tests/test_shot_chain.py` 12 passed。

### 随后核查

- [x] 角色超过可用参考槽位时不得静默丢弃必需角色：七角色/六角色槽回归验证 Resolver 记录 `slot_limit/skip`，Compiler 返回 `reference_capacity_exceeded(capacity=6)`，生成层抛出 409 `prompt_validation_failed`；与既有容量测试合计 2 passed。
- [x] Phase 1 Shadow Run 已以只读对比形式落地；语义分析 Adapter 与持久化仍待后续切片，Phase 1 不标记完成。

## 本批验证记录

### Slice 15（分镜规划安全写入）

- 聚焦测试：`python -m pytest -q tests/test_storyboard_plan.py` → 11 passed；新增创建、校验拒绝、缺 `plan_version`、锁定跳过、更新+孤儿保留、同场景换序、保留镜头序号冲突 409、项目隔离共 8 条。
- 全量回归：`python -m pytest -q` → 1322 passed，无既有行为回归。
- 写入边界：校验失败/ID 冲突/换场/序号冲突均零写入；锁定镜头关键字段不变；旧镜头不删除；不触发资产计划与生成。
- 回滚：删除 `apply_storyboard_plan` 与 `_write_fields`、`apply` 路由与错误映射、对应测试，并将服务模块 docstring 恢复为只读表述。

### Slice 16（分镜规划导入入口）

- 前端：`npm run build`（含 `vue-tsc -b`）通过，4364 modules，无类型错误；新增 `api/storyboardPlan.ts`、`StoryboardPlanImportDialog.vue` 与 `StoryboardView.vue` 入口改动均通过编译。
- 设计规范：`node scripts/check-colors.mjs` 0 处硬编码色值；`node scripts/check-contrast.mjs` 全部文本令牌 ≥ 4.5:1。
- 交互边界：导入弹窗只做「粘贴 JSON → 只读校验 → 显式写入」，校验不通过不允许写入；写入后仅刷新场景与当前集镜头，不自动重生成、不删除旧镜头。
- 后端：无改动，复用 Slice 15 的 `validate` / `apply` 接口契约。
- 回滚：删除 `frontend/src/api/storyboardPlan.ts`、`frontend/src/components/storyboard/StoryboardPlanImportDialog.vue`，并移除 `StoryboardView.vue` 的导入、`planDialogVisible` / `refreshStoryboard`、分镜规划 `StageCard` 与弹窗绑定。

### Slice 17（工作流版本影响预检）

- 后端：`python -m pytest -q tests/test_workflow_impact.py` → 3 passed（版本变化/停用分类、只读幂等与项目隔离、未知项目 404）；全量 `1325 passed`。
- Ruff：新增服务与测试无问题；`dependencies.py` 仍为既有 `Depends` 默认参数（B008），非本次引入。
- 前端：`npm run build`（含 `vue-tsc -b`）通过，4364 modules；`check-colors.mjs` 0 处硬编码；`check-contrast.mjs` 全部文本令牌 ≥ 4.5:1。
- 边界：只比对 `status=succeeded` 且带 `workflow_version` 的任务；工作流记录缺失（外键删除受限）归为 `unknown_workflow_version`，仅在数据异常时出现。
- 回滚：删除 `backend/app/services/workflow_impact.py`、`backend/tests/test_workflow_impact.py`，移除 `dependencies.py` 的导入与新路由；前端移除 `dependencies.ts` 的工作流影响类型/函数与 `AssetImpactView.vue` 的卡、状态和 `load` 分支。

### Slice 18（工作流版本影响看板角标）

- 前端：`npm run build`（含 `vue-tsc -b`）通过，4364 modules；`check-colors.mjs` 0 处硬编码；`check-contrast.mjs` 全部文本令牌 ≥ 4.5:1（角标使用 `--state-warning` / `--danger-text` 与 `--bg-elevated`）。
- 后端：无改动，复用 Slice 17 的 `GET /projects/{id}/workflow-impact`。
- 边界：角标仅由已成功任务驱动；查询失败时 `workflowImpactByShot` 为空，看板其他信息不受影响。
- 回滚：移除 `StoryboardView.vue` 的 `getWorkflowImpact` / `WORKFLOW_IMPACT_LABEL` 导入、`workflowImpactQuery` 与派生函数、缩略图角标与 `.workflow-badge` 样式。

### Slice 19（依赖失效标记写路径）

- 后端：`python -m pytest -q tests/test_dependency_invalidate.py` → 4 passed（旧版本边标记/当前边保持、幂等、非资产来源、外来/未知节点 404）；全量 `1329 passed`。
- 读路径联动：测试同时断言 `GET /dependency-graph` 中该边 `validity=stale` 且产生 `dependency_invalid` 诊断；标记后 `changed_roots[].markable_edge_count` 归零。
- 前端：`npm run build`（含 `vue-tsc -b`）通过，4364 modules；`check-colors.mjs` 0 处硬编码；`check-contrast.mjs` 全部文本令牌 ≥ 4.5:1。
- Ruff：新增服务/测试无问题；`dependencies.py` 仍为既有 `Depends` 默认参数（B008），非本次引入。
- 边界：只写 `DependencyRef`，不触碰 `Asset.validity`、`Shot.status`、任务与产物；无迁移。
- 回滚：删除 `backend/app/services/dependency_mark.py`、`backend/tests/test_dependency_invalidate.py`，移除 `dependencies.py` 的导入与 POST 路由；前端移除 `invalidateDependencyRefs`、`markable_edge_count` 与 `AssetImpactView.vue` 的按钮/确认逻辑。

### Slice 20（分镜规划写入前字段差异预览）

- 后端：`python -m pytest -q tests/test_storyboard_plan.py` → 13 passed（新增「字段差异 + 锁定 will_skip + 校验只读」「未提供/同值字段不进入差异」2 条）。
- Ruff：`app/services/storyboard_plan.py`、`tests/test_storyboard_plan.py` 无问题。
- 前端：`npm run build`（含 `vue-tsc -b`）通过；`check-colors.mjs` 0 处硬编码；`check-contrast.mjs` 全部文本令牌 ≥ 4.5:1。
- 口径一致性：差异只覆盖 `_write_fields` 实际会写的字段；`will_skip` 与 `skipped_locked` 判定同源，避免预览与实际写入不一致。
- 边界：校验仍为零写入；写入语义、锁定保护、旧镜头保留均未变。
- 回滚：移除 `storyboard_plan.py` 的 `_field_changes` 调用与函数、`storyboardPlan.ts` 的差异类型字段、导入弹窗的差异区块与 `FIELD_LABELS` / `formatChangeValue`，并删除两条新测试。

### Slice 21（E2E 样例 A：短剧本快速路径）

- 后端：`python -m pytest -q tests/test_pipeline_e2e.py` → 1 passed（4.71s）。
- 替身边界：解析 LLM、场景板 LLM 编译均为替身；渲染走 `render_mock` + 临时 `media_dir`；测试不触达外网，不写 `backend/data/media`。
- 覆盖：实体落库（1 角色 / 1 道具 / 2 场景 / 3 镜头）、公共资产审核闸口、规划 dry-run 零写入、分镜图 → 视频依赖、clip 资产落库。
- 只读断言：影响预检前后任务数与镜头快照完全一致。
- 边界：真实 ComfyUI/GPU 未验证，不能据此认为真实渲染链路通过。
- 回滚：删除 `backend/tests/test_pipeline_e2e.py`；无生产代码改动。

### Slice 22（E2E 样例 B：中途失败 → 显式重试）

- 后端：`python -m pytest -q tests/test_pipeline_e2e.py` → 2 passed（4.53s）。
- 失败注入：仅替换 `mock_render.mock_outputs` 首次调用抛错，执行器其余状态机保持真实。
- 断言：失败任务 `error_code/error_msg/retryable` 正确且零资产；重试新任务 `attempts` 继承并递增；成功后资产唯一且归属新任务。
- 边界：未验证真实 ComfyUI 断连/超时分支；该分支需在真实环境按人工清单验收。
- 回滚：删除 `test_generation_failure_then_retry_succeeds` 用例；无生产代码改动。

### Slice 23（E2E 样例 C：多集复杂路径）

- 后端：`python -m pytest -q tests/test_pipeline_e2e.py` → 3 passed（9.41s）。
- 全量回归：`python -m pytest -q` → 1334 passed（435.65s），无既有行为回归。
- Ruff：`tests/test_pipeline_e2e.py` 无问题。
- 覆盖：双角色 / 双道具 / 三场景 / 六镜头的解析落库、公共资产审核、场景批量分镜图（`count=3`）、同集链式尾帧、集首镜口径与跨集拦截。
- 跨集口径修正：初版用例假设第 2 集首镜可直接生成，实测被 `continuity_check_failed` 拦截——第 1 集未合成时不存在连续性记录，这是既有的显式安全门禁。用例改为断言 422 且不创建任务、不写尾帧，避免用假数据绕过真实约束。
- 边界：跨集正向衔接（合成 → 末帧提取 → 首帧注入）由 `tests/test_continuity.py` 覆盖，本 E2E 只验证缺少连续性记录时的生成入口拦截。
- 回滚：删除 `test_complex_multi_episode_pipeline` 用例及复杂路径常量；无生产代码改动。

### Slice 24（测试计划与验收矩阵文档）

- 新增 `docs/refactor/director_lab_refactor_test_plan.md`：§15.1 矩阵逐项映射到现有测试，§15.2 记录 E2E 样例 A/B/C，§15.2 之后补真实环境人工验收清单与已知缺口。
- 状态口径：只把有直接用例的项标「已覆盖」；实现有校验但缺专项用例的标「部分覆盖」；无自动化用例的标「未覆盖」。
- 复核结果：已修正文档中不存在的测试文件名引用（改为 `test_prompt_compiler.py` / `test_ref_and_parallel.py`），其余引用均按 `backend/tests` 实际文件核对。
- 已知缺口：对白时长冲突、Beat 聚合/拆分、续集复用既有资产缺专项用例；本重构未新增迁移，无迁移测试；真实 ComfyUI/GPU 未验收。
- 边界：本次仅新增文档，不改生产代码、不改测试。
- 回滚：删除 `docs/refactor/director_lab_refactor_test_plan.md`。

### Slice 26（分镜图阶段进入门槛放宽为「≥1 张」）

- 需求：分镜图不必全部生成才能进分镜看板。
- 决策：`storyboard_images` 阶段改为「至少 1 张已生成即可进入」；`done == 0` 仍拦截。分镜视频与合成导出阶段维持全量要求。
- 安全性论证：分镜看板批量出视频只提交已有分镜图的镜头，其余跳过并提示；单镜生成另有 `_check_keyframe_ready` 校验；`derive_progress` 在关键帧缺失时不会推进到合成阶段。因此放宽入口不会产出错误视频或残缺成片，最坏结果只是部分镜头暂时无法出视频。
- 后端：`stage_readiness.py` 新增 `_STAGE_MIN_DONE = {STAGE_STORYBOARD_IMAGES: 1}`；`check_storyboard_images_ready` 的 ready 判定改为 `done >= min_done`，未完成镜头仍进 `blockers`，并在 `message` 中说明「已生成 x/y，还有 N 镜未完成；这些镜头进入后暂不能生成视频」。
- 前端：`StageTransitionDialog` 新增 `partialHint`，已就绪但存在未完成项时以警示样式列出缺失镜头；`useStageTransition` 透传该提示；`StoryboardImagesView` 提供文案并把拦截标题从「尚未全部生成完成」改为「尚未生成任何分镜图片」。
- 一致性修复：左侧「制作流程」栏原先只看 `current_stage`，部分生成时仍显示「已锁定：请先完成分镜图」，与页内按钮已可点击矛盾。`useProjectFlow` 改为对 `storyboard-images` 前置项读取同一 `stage-readiness` 接口的 `ready`。
- 测试：`test_stage_readiness.py` 三个原「缺一张即拦截」用例改写为放行断言（缺 1 张 / 在途 / 失败），新增「0 张仍拦截」用例；`pytest tests/test_stage_readiness.py tests/test_ref_slot_validity_and_seq.py` 24 passed。
- 验证：真实项目 `prj_a10bc7d70728`（6 镜仅 1 张分镜图）接口返回 `ready=true / done=1 / blockers=5`；浏览器实测弹窗出现且列出 5 个缺失镜头、控制台 0 错误；左侧第 05 步由「已锁定」变为「未开始」且可点击跳转，第 06 步仍按分镜视频锁定。
- 边界：不改生成逻辑、不改资产判定、不改合成门槛。
- 回滚：删除 `_STAGE_MIN_DONE` 与其分支（回到 `done == total`），并还原前端 `partialHint` 与 `useProjectFlow` 的 `storyboard-images` 分支。

### Slice 27（worker 心跳与 `/api/health` 真实状态）

- 缺陷：`/api/health` 的 `worker` 字段硬编码 `"ok"`。worker 未启动时任务长期停在 `queued`，健康检查仍报 `healthy`，前端只能靠「排队超过 60 秒」猜测后端没在消费任务，用户完全看不出问题。
- 方案：worker 主循环落心跳文件，健康检查读心跳判定 worker 四态。
- 后端：
  - `app/worker/scheduler.py` 新增 `heartbeat_path()`（`data_dir/worker_heartbeat`）与 `_write_heartbeat()`；启动后写一次，之后每轮主循环、并发满等待循环各写一次。写失败只打日志，不中断调度。
  - `app/api/v1/settings.py` 的 health 改读心跳：文件存在且距今 ≤ `worker_heartbeat_stale_seconds` → `ok`；超阈值 → `stale`；文件不存在 → `not_running`；读取异常 → `unknown`。同时返回 `worker_last_beat`。`status` 三态保持：DB 不可用 `down`；其余任一依赖（含 worker 非 `ok`）异常 `degraded`；全正常 `healthy`。
  - `app/core/config.py` 新增 `worker_heartbeat_stale_seconds: float = 120.0`，必须明显大于 poll 间隔（2s），避免正常轮询间隙被误判。
- 测试：`tests/test_smoke.py` 新增 `test_health_worker_status_reflects_heartbeat`，用 `tmp_path` + monkeypatch 覆盖三态：无文件 → `not_running`；新鲜心跳 → `ok` 且 `worker_last_beat` 非空；过期 1 小时 → `stale` 且 `status == "degraded"`。
- 验证（真实环境 2026-11-22）：
  - worker 运行：`status=healthy`、`worker=ok`、`worker_last_beat=1795353724.04`。
  - 停掉 worker 并等待超过 120s：`status=degraded`、`worker=stale`、`worker_last_beat` 保持旧值（1795353560.02），与心跳文件 `LastWriteTime` 一致。
  - 重启 worker 后恢复 `healthy` / `ok`，心跳时间戳前移。
  - 后端全量 `python -m pytest -q` → 1335 passed（1683.75s）。
- 边界：心跳只反映「调度进程是否在轮询」，不代表任务执行成功；`degraded` 不阻断 API 可用性（worker 不参与 API 存活判定）。桌面版 `desktop.py` 已以子进程方式拉起 worker，无需额外改动。
- 回滚：删除 `heartbeat_path` / `_write_heartbeat` 及其调用、`worker_heartbeat_stale_seconds` 配置，并把 health 的 `worker` 恢复为固定值（不推荐，会重新掩盖 worker 未运行）。

### Slice 25（解析页客户端跳转白屏与分镜「画面」列修复）

- 缺陷 1：从剧本页点「去解析」进入解析页时整页空白，必须手动刷新才显示内容。
- 根因：`ParseView.vue` 的 `agentStatusData` 查询声明在文件末尾，而 `characters` / `props` 查询的 `refetchInterval` 会在 setup 阶段同步求值，经 `needsPersistPoll → parsePersisted` 读取 `agentStatusData`。客户端路由进入时 vue-query 缓存中已有 `parse-status`，`statusTotalEpisodes === 0` 的短路不再生效，访问未初始化变量抛 `ReferenceError: Cannot access 'fe' before initialization`（TDZ），组件渲染中断。全量刷新时缓存为空、短路生效，因此旧代码只在跳转路径暴露。
- 修复：把 `agentStatusData` 的 `useQuery` 上移到 `parseStatus` 之后、`parsePersisted` 之前，删除末尾重复声明，并加注释说明声明顺序约束。
- 缺陷 2：解析结果分镜表「画面」列 6 行全空。
- 根因：列取 `sh.description`，而 `job.result.scenes[].shots[]` 没有 `description` 字段（实际为 `shot_purpose` / `beats` / `start_state` / `end_state`）；DB 中 6 条 `shot.image_prompt` 均非空（660–688 字符），说明数据侧正常。
- 修复：取数改为 `sh.shot_purpose ?? sh.description`，纯文本导出同口径调整。
- 验证：`npm run build`（含 `vue-tsc -b`）通过，4364 modules；浏览器实测剧本页 → 点「去解析」不刷新，body 文本 36 → 372 字符，AI Agent 卡片「已完成 / 已解析 1/1 集」、解析结果 1 场景 / 1 角色 / 1 道具 / 6 分镜 / 1 台词正常渲染，控制台 0 错误，`navigation` 仅 1 条记录证明未 reload。
- 边界：仅前端字段与声明顺序调整，不改后端接口、模型、迁移与工作流 JSON。
- 回滚：还原 `ParseView.vue` 的 `agentStatusData` 声明位置与 `desc` / 文本导出的取数字段。

### Slice 14（以历史版本为基准重做）

- 后端：`python -m pytest -q tests/test_job_snapshot_versions.py tests/test_jobs_list_filter.py` → 6 passed；新增 `result_asset` 有产出/无产出两条用例，既有字段语义未变。
- 前端：`npm run build`（含 `vue-tsc -b`）通过；路由 query 消费、base 版本透传与提示条无类型错误。
- 设计规范：`check-colors.mjs` 0 处硬编码色值；`check-contrast.mjs` 全部文本令牌 ≥ 4.5:1。
- 回滚：还原快照接口 `result_asset` 查询与返回、`JobSnapshotPanel.vue` 按钮/事件、`ShotDetailModal.vue` 与 `StoryboardView.vue` 的 `rebase` 转发、`StoryboardImagesView.vue` 的 query 消费、`useKeyframeRegen.ts` 的 `regenBaseAsset` 与 payload 字段、`ShotDetailDialog.vue` 的 `open` 参数与提示条。
- 已知边界：仅分镜图（`generate_keyframe`）任务提供重做入口；旧版本被标记 `superseded` 但记录保留，可直接作为 `base_asset_id`；参考图不会复用快照版本，这是既有 `build_base_config` 决定的明确取舍。

### Slice 13（资产变更影响列表页）

- 前端：`npm run build`（含 `vue-tsc -b`）通过；新增页面、路由与资产库入口无类型错误。
- 设计规范：`check-colors.mjs` 0 处硬编码色值；`check-contrast.mjs` 全部文本令牌 ≥ 4.5:1。
- 后端：无改动，沿用项目级 `impact-preview` 接口契约；纯只读页面，不新增表、不改迁移与工作流 JSON。
- 回滚：删除 `AssetImpactView.vue`、还原路由与 `AssetsView.vue` 入口、还原 `dependencies.ts` 常量抽取与 `DependencyImpactOverview.vue` 文案引用。
- 已知边界：变化源仍以「已过期依赖记录的上游」为起点，依赖记录从未写入的早期资产不会被识别；资产名称索引依赖 `listAssets` 分页，资产量极大时首屏会多几次请求。

### Slice 12（看板按依赖状态筛选）

- 前端：`npm run build`（含 `vue-tsc -b`）通过。
- 设计规范：`check-colors.mjs` 0 处硬编码色值；`check-contrast.mjs` 全部文本令牌 ≥ 4.5:1。
- 纯前端切片，未改后端与接口；回滚仅需还原 `StoryboardView.vue`。
- 已知边界：筛选基于最近一次手动检查结果，页面刷新或集数切换后需重新检查；筛选不改变批量选择的勾选集合。

### Slice 11（快照参考图版本比对）

- 后端：`tests/test_job_snapshot_versions.py`、`tests/test_jobs_list_filter.py` 合计 4 passed；快照接口仅新增 `current_version` / `version_stale` 两个字段，未改既有字段语义。
- 前端：`npm run build`（含 `vue-tsc -b`）通过。
- 设计规范：`check-colors.mjs` 0 处硬编码色值；`check-contrast.mjs` 全部文本令牌 ≥ 4.5:1。
- 只读：比对为一次批量资产查询，不写库；资产删除时明确返回无法判断，不猜测。
- 回滚：还原 `app/api/v1/regeneration.py` 快照接口片段与 `JobSnapshotPanel.vue`，删除 `tests/test_job_snapshot_versions.py`。

### Slice 10（生成快照只读查看）

- 后端：`tests/test_jobs_list_filter.py` 2 passed；`GET /api/jobs` 仅新增可选 `shot_id` 查询参数，未改响应结构与既有过滤语义。
- 前端：`npm run build`（含 `vue-tsc -b`）通过，`JobSnapshotPanel.vue`、`ShotDetailModal.vue` 无类型错误。
- 设计规范：`check-colors.mjs` 0 处硬编码色值；`check-contrast.mjs` 全部文本令牌在四档背景上 ≥ 4.5:1。
- 未新增表、未改迁移、未改工作流 JSON、未改生成链；回滚需还原 `app/api/v1/jobs.py`、`frontend/src/api/jobs.ts`、`ShotDetailModal.vue` 并删除 `JobSnapshotPanel.vue`。
- 已知边界：快照只覆盖执行器/重生成链路已落库的 `input_snapshot`；早期任务无快照时只显示空态。`app/jobs.py` 为未挂载的历史副本，本次未改其行为。

### Slice 9（分镜图批量出图前依赖影响预检）

- 前端：`npm run build`（含 `vue-tsc -b`）通过，`StoryboardImagesView.vue`、`StoryboardView.vue` 与 `api/dependencies.ts` 无类型错误。
- 设计规范：`check-colors.mjs` 0 处硬编码色值；`check-contrast.mjs` 全部文本令牌在四档背景上 ≥ 4.5:1。
- 后端：无改动；`tests/test_dependency_impact.py`、`tests/test_dependency_graph.py` 6 passed。
- 纯前端切片，不新增表、不改 API、不改工作流 JSON；回滚只需还原上述三个前端文件。
- 已知边界：预检结果是点击时刻的项目级快照，勾选变化或上游再次变更后需重新预检；预检只提示，不阻止提交，也不触发任何重做。

### Slice 8（看板镜头卡片依赖影响常驻标记）

- 前端：`npm run build`（含 `vue-tsc -b`）通过；`StoryboardView.vue`、`DependencyImpactOverview.vue` 改动无类型错误。
- 设计规范：`check-colors.mjs` 0 处硬编码色值；`check-contrast.mjs` 全部文本令牌在四档背景上 ≥ 4.5:1。
- 后端：无改动；`tests/test_dependency_impact.py`、`tests/test_dependency_graph.py` 回归 6 passed。
- 纯前端切片，不新增表、不改 API、不改工作流 JSON；回滚仅需还原两个前端文件。
- 已知边界：角标来自手动触发的项目级检查结果，页面刷新或集数切换后需重新检查；job 节点经 `path` 回映射镜头，若依赖记录缺失则该镜头不会被标记（与项目级总览同一边界）。

### Slice 7（项目级依赖待处理总览）

- 后端：`tests/test_dependency_impact.py`、`tests/test_dependency_graph.py` 6 passed。
- 全量后端：1309 passed、92 warnings（403.39s），无 failed、无 error。
- 前端：`npm run build`（含 `vue-tsc -b`）通过；新增 `DependencyImpactOverview.vue` 与看板接入无类型错误。
- 未新增表、未改迁移、未改工作流 JSON、未改生成链；项目级接口与前端卡片均可独立回退。
- 项目级总览的遍历以「过期依赖的上游」为起点，若依赖记录本身从未写入（如早期资产无 ref），不会被识别为变化源，属已知边界。

### Slice 6（依赖影响预演接入前端）

- 前端：`npm run build`（含 `vue-tsc -b`）通过，`DependencyImpactPanel.vue`、`dependencies.ts` 与两处接入无类型错误。
- 后端：`tests/test_dependency_impact.py`、`tests/test_dependency_graph.py` 5 passed，接口契约未变。
- 全量后端：1307 passed、1 failed、92 warnings（380.56s），唯一失败为本机 Windows 路径分隔符断言；修正测试断言后 `tests/test_shot_chain.py` 12 passed。
- 未改后端生产代码、模型、迁移与工作流 JSON；前端仅新增只读面板，可单独回退。
- 真实 ComfyUI/GPU 未参与，依赖预演为纯只读计算，无生成行为变化。

### Slice 5（依赖变更影响预演）

- 新增聚焦测试：`tests/test_dependency_impact.py`，2 passed（血缘版本传播、隔离空影响、404）。
- 依赖图/结构分析/分镜规划联合回归：14 passed。
- 新增 `dependency_impact.py` 与路由改动 Ruff 通过。
- 全量后端：1301 passed、7 failed、92 warnings（323.77s），失败仍为基线七项，无新增失败。

### Slice 4（分镜规划 Adapter）

- 新增聚焦测试：`tests/test_storyboard_plan.py`，4 passed（有效规划映射、结构化错误、锁定冲突、项目隔离）。
- 四个增量切片联合回归：17 passed。
- 新增 `storyboard_plan.py`（服务/路由）与测试 Ruff 通过。
- 全量后端：1299 passed、7 failed、92 warnings（325.86s），失败仍为基线七项，无新增失败。

### Slice 3（结构分析与 Shadow Run）

- 新增聚焦测试：`tests/test_script_analysis.py`，5 passed（结构确定性、场景标题切分、证据校验/伪造拒绝、Shadow Run 对比、API 隔离与 404）。
- 相关切片联合回归（原文溯源、依赖图、结构分析及提示词编译子集）：19 passed。
- 新增 `script_analysis.py`、`analysis.py` 与测试 Ruff 通过；`main.py`/`schemas/script.py` 的 Ruff 失败均为既有 import 排序、BLE001、UP017 历史问题。
- 全量后端：1295 passed、7 failed、92 warnings（295.27s），失败与基线七项完全一致，无新增失败。
- 无 DB 迁移、无模型层变更、无前端源码变更；Shadow Run 结果不落库。
- 真实 ComfyUI/GPU 与语言模型语义分析未执行。

### 第一批（原文溯源 / 依赖图 / 槽位溢出）

- 原文溯源与依赖图聚焦测试：8 passed；新增服务、API 与测试文件 Ruff 通过。
- 角色槽位容量聚焦测试：2 passed；主体角色/Resolver 相关回归：6 passed；`tests/test_prompt_compiler.py` Ruff 通过。
- 全量后端：1290 passed、7 failed、92 warnings（338.60s）。失败仍为基线中的两个 project purge、一个模板上下文、一个提示词文档同步、两个空提示词填充及一个 Windows 路径断言，未出现新增失败。
- 前端：`npm run build` 通过，包含 `vue-tsc -b` 与 Vite 生产构建；本批未修改前端源码。
- `git diff --check` 通过。既有文件及全仓 Ruff 历史问题未清零，不声称全仓 lint 通过。
- 真实 ComfyUI/GPU 生成、H3/Ref2VA、TTS、合成与导出未执行实机验证。

## 变更与回滚

当前第一批设计不新增表、不修改既有工作流 JSON、不改动任务执行链。每个新增服务和 API 均可独立移除；若后续出现数据库迁移，将在此记录迁移版本、备份和 downgrade 验证结果。
