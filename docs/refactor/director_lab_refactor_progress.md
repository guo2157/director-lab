# Director-Lab 重构执行进度

更新日期：2026-11-22

状态口径：`已完成` 必须有代码与测试证据；`进行中` 表示已有实施但未验收；`未开始` 不代表仓库完全没有相关历史能力。

## 总览

| Phase | 状态 | 当前证据/下一步 |
|---|---|---|
| 0 源码审计与基线 | 已完成 | 审计报告已落盘；后端、Ruff、前端基线已实测 |
| 1 Script Intelligence 拆分 | 进行中 | 第一切片：原文区块索引；第三切片：结构分析契约、证据校验与只读 Shadow Run；语义分析仍需外部文档 |
| 2 Storyboard Planning | 进行中 | 第一切片：规划契约校验、Shot 兼容 dry-run Adapter、覆盖/顺序/锁定冲突检查；未接规划生成与写入 |
| 3 Dependency/Snapshot | 进行中 | 只读图查询与诊断；新增变更影响预演（版本比较、重做/重绑分类、只读策略）；不新建依赖表 |
| 4 Reference Binding | 已有实现，溢出阻断已验证 | 七角色超六槽回归证明 Resolver 留痕、Compiler 报错、生成硬阻断 |
| 5 Prompt/Workflow Compiler | 已有实现，未改 | 保持 D-24 单一编译层 |
| 6 分镜图生产 | 已有实现，未改 | 保留单有效版本，不实施 Plan 通用候选图库建议 |
| 7 Storyboard Board | 已有实现，未改 | 待统一依赖查询验收后决定最小 UI 接入 |
| 8 连续性与视频依赖 | 已有实现，未改 | 不改 H3/Ref2VA/TTS/合成导出约定 |
| 9 Job/Event/Recovery | 已有实现，未改 | 不新增任务框架 |
| 10 前端全流程体验 | 未开始 | 不整页重写 |
| 11 DB/API 兼容 | 进行中 | 第一批 additive API，无 DB 迁移 |
| 12 E2E 与验收 | 未开始 | mock 与真实环境验证分开记录 |

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

### 随后核查

- [x] 角色超过可用参考槽位时不得静默丢弃必需角色：七角色/六角色槽回归验证 Resolver 记录 `slot_limit/skip`，Compiler 返回 `reference_capacity_exceeded(capacity=6)`，生成层抛出 409 `prompt_validation_failed`；与既有容量测试合计 2 passed。
- [x] Phase 1 Shadow Run 已以只读对比形式落地；语义分析 Adapter 与持久化仍待后续切片，Phase 1 不标记完成。

## 本批验证记录

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
