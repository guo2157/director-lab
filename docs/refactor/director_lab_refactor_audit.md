# Director-Lab 全链路重构源码审计

审计日期：2026-11-22  
执行依据：`director_lab_full_pipeline_refactor_execution_plan.md`  
审计基线：`main` / `aee23cf`，审计开始时工作区干净。

## 1. 审计结论

当前仓库不是“单次 LLM 解析后直接拼工作流”的空白状态。参考图绑定、提示词编译、工作流版本、生成快照、任务恢复、分镜工作台、连续性和事件机制均已有实际实现。本次重构应补齐真实缺口，不重复建设已有框架。

优先缺口：

1. `ScriptVersion.content_snapshot` 虽保存不可变原文，但没有稳定的原文区块索引及证据校验。
2. Story Analysis、Scene/Beat、Asset Analysis 与 Storyboard Planning 尚未形成独立、可 Shadow Run 的数据契约。
3. 依赖数据分散在 `dependency_ref`、`asset_ref`、`asset_plan_dependency`、`job_input_ref` 和 Job/Asset 快照中，缺少统一只读查询、影响分析和图诊断服务。
4. `reference_resolver.py` 对超过角色槽位上限的角色记录 `slot_limit/skip`，需要确认生成门禁是否必然阻断，避免静默丢角色。

## 2. 基线验证

| 检查 | 实际结果 | 说明 |
|---|---|---|
| 后端 `python -m pytest -q` | **1281 passed / 7 failed / 92 warnings** | 当前 checkout 实测，耗时 320.21 秒 |
| 后端 `python -m ruff check app tests` | 失败 | 大量既有 import 排序、未使用 import 等问题，不能记为重构回归 |
| 前端 `npm run build` | 通过 | `vue-tsc -b && vite build`，4350 个模块，8.42 秒 |
| 真实 ComfyUI/GPU 生成 | 未执行 | 后续只能以 mock/集成测试验证的部分不得标记为真实环境通过 |

既有 7 个后端失败：

- `test_project_purge_fk_coverage.py` 2 个：夹具创建 Shot 时未提供非空字段 `shot_kind`、`duration`。
- `test_prompt_compiler.py` 1 个：模板缺少 `action_moment`、`aspect_desc`、`ref_duty_block` 渲染上下文。
- `test_prompt_docs_sync.py` 1 个：文档模板与运行时常量不一致。
- `test_prompt_empty_fill.py` 2 个：`prompt_compilation_not_ready`。
- `test_shot_chain.py` 1 个：Windows 下 `/` 与 `\\` 路径表示断言不一致。

这些失败均发生在任何重构代码编辑之前，修复与否须单独记录，不得混入新功能“测试通过”的统计。

## 3. 现有生产架构事实

### 3.1 剧本与分镜

- `backend/app/models/narrative.py`
  - `ScriptVersion` 以 `content_snapshot + content_hash + version` 保存剧本快照；当前保存流程会覆盖同一项目的现有记录并递增 `version`，不是保留多条不可变版本记录。
  - `Shot` 已有结构化对白、道具、Beat、起止状态、连续性字段与审核字段。
  - 当前没有原文区块 ID、Shot 到原文证据的显式关联。
- `backend/app/services/script_parse.py`
  - 主解析输出仍同时包含项目分析、角色、道具、场景、剧集及场景内镜头。
  - `normalize_parse_output()` 及 lint 逻辑负责确定性归一化和校验。
  - Prompt 明确禁止解析层生成最终 `image_prompt` / `video_prompt`。
  - 空镜头结果、截断输出和重试属于现有硬约束，不得回归。
- `backend/app/services/prompt_compiler.py`
  - 是最终图片/视频 Prompt 编译层；依据 D-24，不把编译职责移回 parser。

### 3.2 依赖与快照

- `backend/app/models/production.py`
  - `DependencyRef`：通用上游类型/ID/版本到下游目标的依赖。
  - `AssetRef`：Shot 对具体 Asset 版本的引用。
  - `JobInputRef`：每次 Job 实际装配的槽位、输入键、资产 ID/版本、职责和启停状态。
  - `WorkflowVersion`、`Job.input_snapshot`、`Asset.input_snapshot`：保留工作流和生成输入历史。
- `backend/app/models/asset_plan.py`
  - `AssetPlan` 提供稳定资产定义；`AssetPlanDependency` 提供定义间槽位依赖。
- `backend/app/services/series/impact_analysis_service.py`
  - 只处理系列历史剧本修改影响，不是通用生产依赖图，不能冒充统一实现。
- 现状是“有多类权威记录、缺统一查询”，优先实现只读聚合服务，不新增平行依赖表。

### 3.3 Reference Binding

- `backend/app/services/reference_resolver.py`
  - `resolve_shot_references()` 以资产 ID 解析场景、角色、道具、生命周期和已确认角色道具组合图。
  - 输出 `ReferenceManifest`、实际槽位、选择来源、回退原因和生命周期快照版本。
- `backend/app/services/reference_layout.py`
  - `compressed_numbers()` 是移除未用槽位后，Prompt 图片编号与实际图片顺序对齐的权威机制。
- `backend/app/api/v1/bindings.py`
  - 主要是角色道具关系和道具视觉校验，不是通用依赖图 API。

### 3.4 工作流与真实槽位

槽位事实来自实际 JSON 与 capabilities，不允许在业务代码或 LLM Prompt 中重新猜编号。

- `wf_image_ref_qwen21_multi`
  - 9 个图片输入，依次为：`ref_scene`、`ref_identity`、`ref_character_2`、`ref_character_3`、`ref_character_4`、`ref_prop`、`ref_prop_2`、`ref_character_5`、`ref_character_6`。
  - 实测 JSON 节点 4 为 `TextEncodeQwenImage21`，`images.image_1..9` 连接 LoadImage 节点 20–28。
- H3 Ref2VA
  - 9 个图片输入：`ref_prev_frame`、`ref_image`、`ref_identity`、`ref_character_2`、`ref_prop`、`ref_character_3`、`ref_character_4`、`ref_character_5`、`ref_character_6`。
  - 另有 3 个音频输入；没有 `ref_scene` 槽位。
- 公共入口为 `capabilities.declared_reference_slots()`；不得修改 Ref2VA JSON 来适配新设计。

### 3.5 任务、事件与恢复

- `Job` 继续作为底层执行单元。
- `Task` / `TaskStage` / `TaskEvent` / `TaskResult` 是既有上层编排，不再引入第三个任务框架。
- `JobEvent` 是事务性 outbox；任务状态与事件应同事务写入，提交前不得发布。
- 远程 ComfyUI、网络与媒体操作必须保持在短数据库事务之外。
- 已有依赖门禁、取消、恢复、幂等键及 SSE/轮询测试，后续仅补真实缺口。

### 3.6 前端与 API

- Vue 3 + TypeScript + Naive UI + Pinia + TanStack Query 已覆盖剧本、解析、资产、分镜图、分镜看板、合成导出、任务进度等流程。
- 新 API 必须在 `backend/app/main.py` 注册；`api/v1/__init__.py` 不是注册中心。
- `backend/app/api/v1/shots.py` 使用成功 Job 的 current Asset，并以 `updated_at` 防止浏览器缓存旧图。
- 上游变化只标记 stale/redo，不自动重生成、不删除旧数据。
- 列表序列化存在逐镜头/类型查询当前 Asset 的 N+1 候选优化点；未基准测量前不做无关改写。

## 4. Plan 与项目约束的冲突处理

以下项目决策优先于 Plan 中的通用示例：

1. 分镜图和分镜视频采用单有效版本，不新增候选图库或多版本重生成体系。
2. 不修改 Ref2VA 工作流配置，不改变 H3/Ref2VA、TTS、合成和导出行为。
3. H3 原生音频仍是最终音频主路径；已审核 TTS 按说话人作为参考音频注入，不盲目替换 H3 音频。
4. 分镜图与末帧路径/命名遵循当前稳定覆盖约定。
5. 上游变更标记下游 stale/redo，不自动触发生成或删除历史结果。
6. `API.md`、`DATABASE.md`、`ARCHITECTURE.md`、`WORKFLOW.md` 在各自领域为权威文档；冲突需显式记录。

## 5. 分阶段落地校准

| Phase | 审计状态 | 实施策略 |
|---|---|---|
| 0 审计/基线 | 已执行 | 记录真实基线，既有失败单列 |
| 1 Script Intelligence | 部分/缺失 | 先加原文区块索引、证据 Validator 和独立分析契约；Shadow Run 不覆盖正式数据 |
| 2 Storyboard Planning | 与主解析耦合 | 后续以兼容 Adapter 拆分，不推翻现有 Shot |
| 3 Dependency/Snapshot | 快照已有、统一查询缺失 | 聚合现有四类依赖记录，提供遍历、影响、环/断链诊断 |
| 4 Binding Engine | 已有 | 只补槽位溢出阻断等实证问题 |
| 5 Compilers | 已有 | 保持单一编译层，修复问题需同步既有模板测试 |
| 6 图片生产 | 已有 | 保留单有效版本与任务恢复，不建候选体系 |
| 7 Storyboard Board | 已有 | 后续只补依赖可视化和实证 UX 缺口 |
| 8 连续性/视频 | 已有 | 不改 H3 音频和 Ref2VA 槽位约定 |
| 9 Job/Event/Recovery | 已有 | 不新增任务框架 |
| 10 前端流程 | 已有主体 | 新能力按需接入，不整页重写 |
| 11 DB/API 兼容 | 待逐切片验证 | 优先无迁移、 additive API；必须迁移时先检查 Alembic |
| 12 E2E/验收 | 未完成 | mock 与真实环境严格区分 |

## 6. 第一批实施边界

第一批只包含可独立回滚的共享基础能力：

- 从不可变原文确定性生成稳定 `source_block_id`、顺序、字符范围和原文证据；提供引用校验。
- 在现有模型之上提供只读 Dependency Graph，统一依赖方向、影响遍历、环与断链诊断。
- 提供 additive API 和聚焦测试；不新增数据库表，不改变现有生成入口。

后续阶段只有在代码、测试和验收均真实完成后，才能在进度文档中标记完成。
