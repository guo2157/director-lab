# Director-Lab 动态工作流系统 — 现状审计报告（P0）

> 审计对象：`/home/ubuntu/director-lab`（HEAD `bb9799d`）
> 审计时间：2026-11-18
> 审计方法：读 `docs/WORKFLOW.md` / `docs/API.md` / `docs/DATABASE.md` / `docs/AGENTS.md` → 读真实代码 → 查真实 DB（`backend/data/director_lab.db`）→ 查真实工作流 JSON → 调运行中实例（`http://127.0.0.1:8000/api/workflows`）→ 跑测试基线。
> **本报告只描述已验证的事实。计划里的设计意图不等于已实现，反之亦然。**

---

## 0. 一句话结论

**计划里认为「待建设」的大部分能力，本项目已经实现，只是实现形态与计划设想不同；真正缺失的是「上传即产出可用 Schema」「不可变版本表」「参数 Schema 可用性（label/type/range）」「统一工作流选择器 UI」「运行环境依赖检查」这 5 处。**
因此本项目的正确做法是**扩展**（extend），不是**重建**。若按计划原文从零搭 `WorkflowManager/WorkflowParser/WorkflowCompiler/WorkflowOutputParser`，会与现有 4 个已在生产链路里的模块重复建设，并直接违反自身「禁止重复建设」条款。

量化：计划 6 个后端模块中，**4 个已有等价实现且带生产修复补丁**（Parser / Compiler / Binding / OutputParser），**2 个完全缺失**（Manager 的版本/状态部分、RuntimeChecker）；前端 6 个组件**全部缺失**，但已有 1 个半成品入口（设置页工作流 tab）。

---

## 1. 真实架构与主调用链（带行号）

```
POST /api/shots/{id}/generate        api/v1/generate.py:1007
  └─ _match_workflow                 api/v1/generate.py:127      ← 匹配（默认→内置→任意 + 参考槽位修正）
  └─ 提示词编译 + 业务素材装配        services/prompt_compiler.py:1140 (keyframe) / :1787 (clip)
       └─ reference_resolver.py      ReferenceManifest（asset_id 精确查找，禁止按下标猜身份）
  └─ _create_job                     api/v1/generate.py:396      ← 落库 job + input_snapshot
       └─ audit_reference_slots      services/prompt_compiler.py:923  ← 槽位职责硬校验（防换脸/串镜）

worker（director-lab-worker.service）
  └─ execute_generate_job            worker/executor.py:680
       ├─ 媒体上传 + 系统槽位注入     executor.py:820-900（ref_prev_frame / first_frame / ref_audio_N / width / height / length）
       ├─ prune_unbound_media_inputs services/workflow_inject.py:112   ← 剔除未注入槽位（防占位图污染）
       ├─ compact_reference_slots    workflow_inject.py:159            ← ref_image_N 连续化
       ├─ remap_picture_refs         workflow_inject.py:192            ← <Picture N> 重编号 + 删失效行
       ├─ compact_qwen_image_slots   workflow_inject.py:236            ← images.image_N 连续化
       ├─ remap_image_tags           workflow_inject.py:262 附近        ← <imageN> 重编号
       ├─ inject_params              workflow_inject.py:86             ← 标记注入（深拷贝，不改原定义）
       ├─ 提交 ComfyUI               integrations/comfyui.py:53  (+ pool 多实例)
       └─ find_output_nodes + extract_outputs + download_file
                                     workflow_inject.py:301 / comfyui.py:147 / executor.py:1549
```

**结论：计划的 `WorkflowCompiler`（校验→绑定→替换→生成 Prompt）与 `WorkflowOutputParser`（解析产物→归一化）已经存在，且积累了大量真实故障修复（占位图剔除、槽位断档压缩、Picture 重编号、未声明参数告警）。计划若要求「新建」这两个模块，将直接丢掉这些修复。**

---

## 2. 能力矩阵：已实现 / 部分实现 / 完全缺失

### 2.1 已实现（可直接复用，勿重建）

| 能力 | 真实位置 | 证据 |
|---|---|---|
| workflow 表 + 默认/内置标记 | `models/production.py:62-76` | DB `workflow` 有 13 列，10 条记录，`is_builtin=1` |
| 标记协议解析（`$param` / `$param.field` / `$param!` / `$~param` / `$~param.field` / `$output.name`） | `services/workflow_inject.py:40` `parse_workflow` | 与 `docs/WORKFLOW.md §2` 完全一致；含画布残留键检测 |
| 参数注入（深拷贝、必填校验、未提供保留工作流原值） | `workflow_inject.py:86` `inject_params` | 返回注入快照 |
| 未注入参考槽位剔除（含删除 LoadImage 节点） | `workflow_inject.py:112` | 防 `placeholder_*.png` 污染生成结果 |
| autogrow 槽位断档压缩 + 提示词引用重编号 | `workflow_inject.py:159/192/236/262` | H3 `<Picture N>` 与 Qwen `<imageN>` 双通道；失效引用整行移除 |
| 输出节点解析（`$output.*` → node_id，重复标记报错） | `workflow_inject.py:301` | — |
| 产物提取（images/videos/audio/gifs） | `integrations/comfyui.py:147` | 归一为 `{kind, filename, subfolder, type}` |
| 产物落库（版本化、缩略图、末帧抽取、失败不阻断） | `worker/executor.py:1549-1700` | `asset_type_map`、`_persist_shot_last_frame:406` |
| 工作流匹配（target→workflow_type；默认→内置→任意） | `api/v1/generate.py:44/127/239` | 含「分镜图默认改用带参考槽位工作流」修正 |
| 参考图业务绑定（@角色/@场景/@道具 → 槽位，未匹配硬报错） | `services/ref_inject.py:66/136` | 不静默忽略；槽位不足 422 |
| 参考图解析引擎（asset_id 精确查找 + 回退记录） | `services/reference_resolver.py` `ReferenceManifest` | 已声明「禁止按图片索引猜身份」 |
| 槽位职责硬校验（身份槽/时序槽串位阻断） | `services/prompt_compiler.py:923` `audit_reference_slots` | 409 `reference_slot_mismatch` |
| 任务快照（params / 注入媒体 / 槽位布局 / ComfyUI 实例） | `generate.py:435-445`、`executor.py:1318`、`executor.py:900-906` | `input_snapshot.reference_layout` / `injected_media` / `comfyui_instance` |
| 重试复用原快照 + 原 workflow_version | `api/v1/jobs.py:166` | 复制 `input_snapshot`，不改工作流 |
| 重新生成工作台（三档模式 / 槽位可编辑 / 快照冻结 / 幂等键） | `api/v1/regeneration.py:200/231`、`services/regen_workspace.py` | `inherit_prompt_only` / `modify_refs` / `full_reconfigure` |
| `job_input_ref` 结构化参考图索引表 | `models/production.py:144` | **只有** `regeneration.py:642` 写入 |
| ComfyUI 多实例池 + 实例级故障切换 | `integrations/comfyui_pool.py`、`executor.py:646` | 与 AGENTS.md 硬约束 #3 一致 |
| ComfyUI 连通性/实例状态端点 | `api/v1/settings.py:176/260/265/320` | 仅连通性，**不含依赖检查** |
| 工作流静态校验脚本（API 格式/连线/标记/宽高成对/输出存在） | `backend/scripts/validate_workflows.py` | 与 `workflow_inject.parse_workflow` 同套规则 |
| 工作流能力 sidecar（9 槽位声明、分辨率约束、帧数规则） | `backend/data/workflows/*.meta.json` | 见 §5 |

### 2.2 部分实现（可用但不足以支撑动态表单）

| 能力 | 现状 | 缺什么 |
|---|---|---|
| 工作流上传 | `api/v1/workflows.py:53` 接受 multipart ≤10MB，解析 JSON | `_parse_schema`（`:158`）**按画布格式解析**（读 `definition["nodes"]` 与 `widgets_values`），而真实工作流是 API 格式（`{"1": {...}}`）→ 上传后 `parsed_schema` 恒为空。未调用 `parse_workflow`，不查标记、不查输出、不校验 API 格式 |
| 上传元数据 | 只有 `name / workflow_type / description / trigger / capabilities` | 无 `alias`、`original_filename`、`file_hash`、`status`；前端（`api/workflows.ts:11`）**只传 name+type**，`trigger`/`capabilities` 恒为 null → 上传即失去匹配资格 |
| 版本管理 | `version` 整数列，`PATCH` 时 `wf.version += 1` 并**原地覆盖 `definition`**（`workflows.py:110-135`） | 与 `docs/API.md:431`「生成新的不可变工作流版本」**直接矛盾**。历史 job 的 `workflow_version` 指向已被覆盖的内容 → 计划 §11.3「重试不改变原始配置」实际不成立 |
| 静态校验 | `workflows.py:95` `validate_workflow` 检查 `definition["nodes"]` / `["links"]` | 对 API 格式恒返回「缺少 nodes 定义」→ 该端点对合法工作流**只会报错，是无效校验** |
| 输出解析 | 通用 `extract_outputs` + `$output` 映射 | 无输出 Schema（`media_type` / `multiple` / `required`）、无「成功但零产物」「文件大小 0」的独立判定（现以 `output_missing` fail 处理，算够用） |
| 参数 Schema | `scripts/seed_workflows.py:59` 生成 `{params: {name: {node_id,field,media,required}}, outputs:[]}` | 无 `label/type/semantic/default/min/max/step/options/group/advanced/order` → **无法渲染动态表单**；且与上传端点产物（list 形状）**两种格式并存** |
| 参考图槽位语义 | 槽位名语义写死在代码（`ref_inject.py:33-44` 的 `_CHARACTER_SLOTS` 等 + `RESERVED_SLOTS`） | 无计划 §6.2 的通用 `semantic` 枚举（`first_frame/last_frame/previous_shot_last_frame/character_identity/...`）；槽位不可由用户编辑 |
| 工作流选择 UI | 仅 `StoryboardView.vue:42`：`items.find(w => w.name.endsWith(clipTier.value))` | **按名称后缀推断工作流**——正是计划 §24 明令禁止的第 3 条；其他生成页面无选择器 |
| 项目级默认工作流 | `docs/API.md:262` 定义 `settings.default_image_workflow_id` 等 | **全仓 0 处代码引用**（`grep default_image_workflow_id` 无命中）→ 文档有、实现无 |
| 运行环境检查 | 实例连通性 + `/object_info` 未使用 | 无「工作流所需 class_type / 模型文件是否存在于目标实例」检查；`capabilities.requires` 目前只是人类可读文本 |
| 幂等 | `regeneration` 有 `idempotency_key`（`request_key`） | `POST /shots/{id}/generate` 无幂等键 |
| 操作审计 | 无 | 无 `workflow` 变更审计日志 |

### 2.3 完全缺失

| 缺失项 | 计划章节 |
|---|---|
| `workflow_version` 表（不可变定义 + file_hash + parser_version + validation_status + runtime_status + dependency_snapshot + changelog + 回滚） | §11.1 / §12.2 |
| `workflow` 表 `alias` / `original_filename` / `status` / `current_version_id` | §3.3 / §12.1 |
| `project_workflow_config`（项目级默认 + 参数配置持久化） | §7.3 / §8.5 / §12.3 |
| `workflow_task_snapshot`（不可变任务快照；现为 `job.input_snapshot` 可变 JSON + 仅 regen 写 `job_input_ref`） | §11.2 / §12.4 |
| `POST /api/workflows/parse`（上传前只解析不落库） | §13.2 |
| `/versions` CRUD / `publish` / `rollback` | §13.3 |
| `GET /schema`（前端渲染用，去掉 `binding.node_id`） | §13.4 / §5.3 |
| `GET /available` / `POST /match`（能力匹配 + 原因） | §13.5 / §16 |
| `POST /versions/{id}/test`（真实测试生成） | §13.2 / §15 |
| 参数白名单 + 参数编辑（改 label/type/range/group/semantic） | §5.2 / §14.3 |
| 前端 6 组件：`WorkflowSelector` / `WorkflowParameterForm` / `WorkflowCapabilityInfo` / `WorkflowReferenceSlots` / `WorkflowVersionSelector` / `WorkflowTestResult` | §7.1 / §8.1 / §14 |
| 工作流管理独立页（列表/详情/版本/测试/回滚），现仅在 `SettingsView.vue:514` 一个 tab 内做列表+上传 | §3.1 / §14.1 / §14.2 |
| 上传安全（节点数上限、嵌套深度、SHA-256、路径穿越、节点 ID 合法性） | §4.3 |
| 参数迁移（切工作流按 `semantic` 迁移） | §7.4 |
| 统一错误码族（`workflow_*` 仅 6 个，缺 `workflow_schema_invalid` / `workflow_capability_mismatch` 之外的版本/运行/编译族） | §13.7 |

---

## 3. 真实数据库结构（`backend/data/director_lab.db`，Alembic head `d2a8b5f14c37`）

### 3.1 `workflow`（13 列，10 行，全部 `is_builtin=1`）

| 列 | 类型 | 说明 |
|---|---|---|
| id | VARCHAR(32) | `wf_<12hex>`，主键 |
| name | VARCHAR(200) | 现为文件名 stem（`wf_video_h3_ref2va_std_5s`）→ **展示名不合格** |
| workflow_type | VARCHAR(16) | 实际值：`image` / `video` / `tts` / `bgm`（**不是**计划的 `image_t2i` 等） |
| description | TEXT | 取自 meta.notes |
| trigger | JSON | `{target, required_params, priority}` |
| capabilities | JSON | 见 §5.2 |
| version | INTEGER | 1 或 3；**可变，非不可变版本** |
| definition | JSON | API 格式，原地可覆盖 |
| parsed_schema | JSON | seed 为 dict 形状；上传端点为 list 形状（不一致） |
| is_default | BOOLEAN | 全 0 |
| is_builtin | BOOLEAN | 全 1 |
| created_at / updated_at | DATETIME | — |

### 3.2 `job`（21 列）

关键列：`workflow_id`（FK，可空）、`workflow_version`（INTEGER）、`params`（JSON）、`input_snapshot`（JSON）、`request_key`（幂等）、`submission_state`、`priority`。

### 3.3 `job_input_ref`（14 列）

`job_id / slot_param / input_key / asset_id / asset_version / asset_type / role / required / file_path / semantic / enabled / seq / created_at`。
**只有 `api/v1/regeneration.py:642` 写入；正常生成链路不写。**

### 3.4 不存在

`workflow_version`、`project_workflow_config`、`workflow_task_snapshot`、`workflow_audit_log`。

---

## 4. 真实 API 清单与文档偏差

| 方法/路径 | 实现 | 与文档偏差 |
|---|---|---|
| `GET /api/workflows` | `workflows.py:38`，返回数组（非分页）+ `used_by_jobs` | 与 `api/workflows.ts` 注释「Paginated」不一致（前端已兼容两种形态） |
| `POST /api/workflows` | `:53` multipart | 文档要求返回 `parsed_schema.params/outputs`；**实际因 `_parse_schema` 读画布字段而恒为空** |
| `POST /api/workflows/{id}/validate` | `:95` | 对 API 格式恒报「缺少 nodes 定义」 |
| `PATCH /api/workflows/{id}` | `:110` | 文档承诺「不可变新版本」，实际原地覆盖 |
| `DELETE /api/workflows/{id}` | `:143` | 与文档一致（默认/内置/被引用 409） |
| `POST /api/shots/{id}/generate` 等 9 个生成端点 | `generate.py` | 均支持 `workflow_id` 覆盖，与文档一致 |
| `GET /api/shots/{id}/regenerate/config` | `regeneration.py:200` | 返回 `workflow/available_slots/refs/model_params/editable/modes`；`workflow` 恒不可编辑 |
| `POST /api/shots/{id}/regenerate/workbench` | `:231` | `full_reconfigure` 模式**后端已支持换工作流 + 传 params** |
| `POST /api/jobs/{id}/retry` | `jobs.py:166` | 复用 `input_snapshot` 与 `workflow_version` ✔ |

无 `/versions`、`/schema`、`/available`、`/match`、`/parse`、`/test` 任何端点。

错误码风格：**现有全部是小写 snake_case**（`errors.py`，103 行），由全局处理器转 `{"detail":{"code","message"}}`。计划 §13.7 提的 `WORKFLOW_NOT_FOUND` 大写风格会破坏前端现有映射，**不应照抄**。

---

## 5. 真实工作流文件与标记协议

### 5.1 文件（`backend/data/workflows/`，10 份 + 10 份 meta）

```
wf_image_t2i_qwen21_turbo       image   0 槽   文生图
wf_image_ref_qwen21_multi       image   9 槽   多参考图（默认出图）
wf_video_h3_ref2va_fast_5s      video   9 槽   极速 5s（tier=fast_5s）
wf_video_h3_ref2va_std_5s       video   9 槽   标准 5s（tier=std_5s）
wf_video_h3_ref2va_quality_10s  video   9 槽   质量 10s（tier=quality_10s）
wf_video_h3_flf2v               video   首/尾帧
wf_video_h3_t2v                 video   纯文生视频（降级）
wf_tts_qwen3_custom_voice       tts     预设音色
wf_tts_qwen3_voice_clone        tts     音色克隆
wf_bgm_stable_audio             bgm     背景音乐
```

**计划 §22 提到的 `image_t2i_qwen` / `image_ref_qwen_edit` / `video_h3_ref2va` 三个文件名在本仓库不存在**；`docs/WORKFLOW.md §13` 也已过时（它写的 9 份里 2 份未交付、且未记录 tier 三档）。验收必须用上表真实文件。

### 5.2 能力声明真实字段（`wf_video_h3_ref2va_std_5s.meta.json`）

```
trigger: {target: clip, required_params: [], priority: 10}
capabilities:
  workflow_type/target/audio/audio_output_mode/fps,
  frame_count_param/step/offset/range, duration_param,
  resolution_constraints{multiple_of,short_edge_min/max,max_long_edge},
  ratio_patch_key/resolution_patch_key, container, cost, local_only,
  hardware{target,status,min_vram_gb,concurrency,...}, requires, license_notice,
  reference_slots: [ {param, input_key, optional} × 9 ],
  max_reference_images: 9, model_max_reference_images: 9, tier: std_5s
```

### 5.3 真实槽位（9 个，非计划的 6 个）

| 参数名 | H3 input_key | Qwen input_key | 语义 |
|---|---|---|---|
| ref_prev_frame | `ref_images.ref_image_0` | — | 上一镜末帧（系统保留） |
| ref_image | `ref_images.ref_image_1` | — | 当前分镜图（系统保留） |
| ref_identity | `ref_image_2` | `images.image_2` | 角色身份（Qwen 侧 `optional:false`） |
| ref_character_2 | `ref_image_3` | `image_3` | 服装/全身 |
| ref_character_3..6 | `ref_image_5..8` | `image_4/5/8/9` | 多角色 |
| ref_scene | — | `image_1` | 场景（Qwen 编辑底图） |
| ref_prop / ref_prop_2 | `ref_image_4` | `image_6/7` | 道具 |
| ref_audio_0..2 | H3 `LoadAudio` | — | **参考音频（见 D6）** |

标记样例：`$prompt.prompt!`、`$width!`、`$height!`、`$length`、`$ref_identity.~image`、`$ref_audio_0.~audio`、`$output.clip`。

---

## 6. 参考图绑定真实链路

1. **业务装配**：`prompt_compiler.compile_storyboard_image:1140` / `compile_shot_video:1787` 依据镜头关联（角色定妆图 / 场景基准图 / 道具锚定图 / 本镜分镜图 / 上镜末帧）生成 `reference_slots`。
2. **精确查询**：`reference_resolver.py` 用 `asset_id` 取 `validity=current` 资产，产出 `ReferenceManifest`（含 `source` 与 `fallbacks`）。
3. **@引用补充**：`ref_inject.build_media_params:136` 把提示词里的 `@名称` 解析进槽位（仅工作流声明过的槽位；槽位不足 422）。
4. **槽位硬校验**：`prompt_compiler.audit_reference_slots:923` 阻断「身份槽放尾帧」「时序槽放角色」等串位。
5. **worker 侧系统槽位**：`executor.py:820-900` 补 `ref_prev_frame`（链式/上镜末帧）、跨集 `first_frame`、`ref_audio_N`、`width/height/length`。
6. **剔除 → 压缩 → 重编号**：见 §1 调用链，结果写 `input_snapshot.reference_layout` 并推 `job.picture_remap` 事件。

**结论：计划 §6.1–6.5（槽位语义、独立绑定、自动绑定、编号动态生成）已实现；缺的只是「用户可编辑的槽位 UI/API」与「通用 semantic 枚举」（现为固定槽位名）。**

---

## 7. 任务快照真实结构

`job.input_snapshot`（可变 JSON，逐阶段 merge）实际包含：

```
workflow_version, prompt_version{fingerprint,template_id,template_version},
params{...}, generation_mode, prompt_qc,
injected_params{width,height,length}, injected_media{slot: 路径},
reference_layout{pruned_slots, slot_remap, image_remap},
prompt_compilation{...}, comfyui_instance, render_mock?, snapshot(完整重生成配置)
```

`JobInputRef` 表仅 regen 链路写；`Asset.input_snapshot` 复制 job 快照。
**没有一个不可变快照记录**：`input_snapshot` 在后续阶段被覆盖（`executor.py:1318` 直接重写 `params`），也没有 `workflow_hash` / `compiled_prompt_hash`。

---

## 8. 缺陷清单（带证据，按优先级）

| ID | 缺陷 | 证据 | 影响 |
|---|---|---|---|
| **D1** | 上传端点用画布格式解析 API 格式工作流 | `workflows.py:158-176` 读 `definition["nodes"]`/`widgets_values`；真实定义为 `{"1":{...}}` | 上传后 `parsed_schema` 恒空 → 动态表单无从渲染 |
| **D2** | 上传不校验标记/输出/API 格式 | 未调用 `parse_workflow`；`validate` 查 `nodes/links` | 上传即「成功」，运行时才在 worker 抛 `workflow_marker_missing` |
| **D3** | 版本不可变契约失效 | `workflows.py:132-135` `wf.definition=...; wf.version+=1` | 违反 `docs/API.md:431`；历史 job 无法复现；重试会静默换内容 |
| **D4** | 前端上传只传 name+type | `api/workflows.ts:11-17`；`SettingsView.vue:248-268` | 上传的工作流 `trigger/capabilities` 为 null → 不参与匹配、无槽位能力 |
| **D5** | 按工作流名称推断档位 | `StoryboardView.vue:42` `name.endsWith(tier)` | 计划 §24 第 3 条明令禁止；改名即断链 |
| **D6** | **TTS 参考音频注入 H3（现状，已确认为目标行为）** | `executor.py:872` + 三个 H3 档位工作流节点 40/41/42 `LoadAudio` → 节点 6 `ref_audios.ref_audio_0..2`；提交 `faa8062` | v1 计划要求「TTS 不进 H3」，v2 计划 §8 明确要求「TTS 音频必须作为 H3 参考输入」。**按 v2 执行：保留现有逻辑**。遗留缺口见 §16.3：音频槽位缺 character→slot 绑定快照、>3 说话人被静默丢弃 |
| **D7** | `parsed_schema` 双格式 | seed：`{params:{name:{...}}}`；上传：`{params:[{key,...}]}` | 任何消费方都要分情况处理 |
| **D8** | 项目级默认工作流文档化但未实现 | `docs/API.md:262` vs `grep default_image_workflow_id` = 0 命中 | 计划 §7.3 实际需要从零做（可用 `project.settings` 扩展，无需新表） |
| **D9** | 静态校验端点对合法工作流恒失败 | `workflows.py:95-107` | 管理页「校验」按钮永远红 |
| **D10** | 无依赖/环境检查 | `capabilities.requires` 仅为文本；`/object_info` 未用 | 「解析成功≠可运行」，计划 §15 完全缺失 |
| **D11** | 参考槽位语义写死在代码 | `ref_inject.py:33-44`、`RESERVED_SLOTS` | 新增语义（如 `last_frame`）要改代码 |
| **D12** | `job_input_ref` 仅在 regen 写 | `regeneration.py:642` 唯一写入点 | 任务详情无法按槽位查询参考图（只能翻 `input_snapshot` JSON） |
| **D13** | 生成接口无幂等键 | `generate.py` 全部端点无 `idempotency_key` | 重复点击产生重复任务（仅靠 `job_in_flight` 部分兜底） |
| **D14** | 上传无安全上限 | 仅 10MB + `json.loads` | 节点数/嵌套深度/SHA-256/节点 ID 合法性均未校验 |

---

## 9. 计划本身需要修正的 5 处（照抄会破坏现有系统）

| 计划原文 | 冲突事实 | 建议 |
|---|---|---|
| `workflow_type` = `image_t2i` / `image_i2i` / `video_reference` … | 现有枚举为 `image/video/tts/bgm/sfx`，且 AGENTS.md 硬约束 #6「三套枚举禁止混用」 | **保留现有 `workflow_type`**，把计划的细分类型放进 `capabilities.generation_modes`（`image_to_image` / `reference_to_video` …） |
| 错误码 `WORKFLOW_NOT_FOUND` 大写 | 现有 103 行错误码全为小写 snake_case + `detail.code` 结构 | 沿用 `workflow_not_found` 等小写码，仅新增缺失的小写码 |
| 6 个参考槽位 | H3 实际 9 槽 + `ref_audio_0..2` | 以 `capabilities.reference_slots` 为准，代码不写死数量（现有实现已如此） |
| 文件名 `image_t2i_qwen` / `image_ref_qwen_edit` / `video_h3_ref2va` | 实际为 `wf_*_qwen21_*` / `wf_video_h3_ref2va_{fast,std,quality}` | 用 §5.1 真实文件验收 |
| 「TTS 音频不进入 H3」 | `faa8062` 已实现 ref_audio 注入 | **停下裁决**（D6），二选一并写进 DECISIONS.md |

---

## 10. 文件级改动清单（最小改动路径）

### 10.1 新增后端

```
backend/app/services/workflow/
  schema_builder.py      # 复用 parse_workflow，产 label/type/semantic/range/group（D1/D7）
  validator.py           # 包装 scripts/validate_workflows.check + 安全上限（D2/D14）
  runtime_checker.py     # /object_info 依赖核对（D10）
  matcher.py             # 能力匹配 + 原因 + 项目默认（D8）
  output_parser.py       # 抽出 executor.py:1549 的产物逻辑为纯函数（可单测）
backend/app/models/workflow_version.py       # 不可变版本（D3）
backend/alembic/versions/<rev>_workflow_dynamic.py   # 加列 + 新表
```

### 10.2 修改后端

| 文件 | 改动 |
|---|---|
| `api/v1/workflows.py` | `_parse_schema` → 调 `schema_builder`；新增 `alias/original_filename` 入库；`validate` 改用真校验器；`PATCH` 改为「复制为新版本」；新增 `/parse` `/versions` `/schema` `/available` `/match` `/test` |
| `api/v1/generate.py` | 匹配改用 `matcher`（读项目默认）；加 `idempotency_key` |
| `api/v1/regeneration.py` | `config.editable.workflow` 依据模式开放；返回 `capabilities` 与 `params_schema` |
| `worker/executor.py` | 编译段抽为 `workflow/compiler.py`（行为不变，加 `compiled_prompt_hash`）；快照写 `workflow_hash` |
| `services/ref_inject.py` | 槽位语义表外置到 `capabilities`（D11） |
| `scripts/seed_workflows.py` | 写 `alias=meta.notes 首句`、`original_filename`、`file_hash` |

### 10.3 新增前端

```
frontend/src/api/workflow.ts                  # 扩展（保留 workflows.ts 兼容）
frontend/src/components/workflow/WorkflowSelector.vue
frontend/src/components/workflow/WorkflowParameterForm.vue
frontend/src/components/workflow/WorkflowCapabilityInfo.vue
frontend/src/components/workflow/WorkflowReferenceSlots.vue
frontend/src/views/workflow/WorkflowManagement.vue / WorkflowDetail.vue
frontend/src/types/workflowSchema.ts
```

### 10.4 修改前端

| 文件 | 改动 |
|---|---|
| `views/SettingsView.vue:514` | 工作流 tab 改为「入口卡片 + 跳转管理页」，或直接迁出 |
| `views/project/StoryboardView.vue:29-50` | 档位用 `capabilities.tier` 匹配，禁止 `endsWith(name)`（D5） |
| `components/storyboard-images/RegenWorkbench.vue` | 接入 `WorkflowSelector` + `WorkflowParameterForm`（当前 216 行，无任何工作流相关代码） |
| `components/storyboard/ShotGenerateTab.vue` | 同上 |

### 10.5 禁止改动

`services/workflow_inject.py` 的 6 个核心函数（parse/inject/prune/compact/remap/find_outputs）**只允许新增，不允许改写语义**——它们承载了 4 次真实故障修复（占位图污染、槽位断档错位、Picture 错序、未声明参数静默跳过）。

---

## 11. 数据库迁移清单（一次 Alembic 迁移，含 downgrade）

```sql
-- workflow：新增列（全部可空/有默认，旧数据无需回填即可跑）
ALTER TABLE workflow ADD COLUMN alias VARCHAR(200);
ALTER TABLE workflow ADD COLUMN original_filename VARCHAR(300);
ALTER TABLE workflow ADD COLUMN file_hash VARCHAR(64);
ALTER TABLE workflow ADD COLUMN status VARCHAR(24) NOT NULL DEFAULT 'published';
ALTER TABLE workflow ADD COLUMN current_version_id VARCHAR(32);
ALTER TABLE workflow ADD COLUMN validation_status VARCHAR(24) DEFAULT 'unchecked';
ALTER TABLE workflow ADD COLUMN runtime_status VARCHAR(24) DEFAULT 'unchecked';

-- 新表：不可变版本
CREATE TABLE workflow_version (
  id VARCHAR(32) PRIMARY KEY, workflow_id VARCHAR(32) NOT NULL REFERENCES workflow(id),
  version INTEGER NOT NULL, definition JSON NOT NULL, file_hash VARCHAR(64),
  parsed_schema JSON, schema_version VARCHAR(16) DEFAULT '1.0',
  parser_version VARCHAR(16), validation_status VARCHAR(24), runtime_status VARCHAR(24),
  dependency_snapshot JSON, changelog TEXT, created_at DATETIME NOT NULL,
  UNIQUE(workflow_id, version));

-- 新表：任务快照（不可变）
CREATE TABLE workflow_task_snapshot (
  id VARCHAR(32) PRIMARY KEY, task_id VARCHAR(32) NOT NULL REFERENCES job(id),
  workflow_id VARCHAR(32), workflow_version_id VARCHAR(32),
  workflow_hash VARCHAR(64), compiler_version VARCHAR(16),
  schema_snapshot JSON, params_snapshot JSON, reference_snapshot JSON,
  output_snapshot JSON, compiled_prompt_hash VARCHAR(64), created_at DATETIME NOT NULL);
```

`project_workflow_config` **不建新表**：`project.settings` 已存在且 `docs/API.md:262` 已定义键名，按其扩展即可（计划 §12.3 允许「优先扩展原表」）。

回填策略：迁移脚本内按 `workflow` 现有行生成 `workflow_version` v1 记录，`current_version_id` 指向它；`file_hash` 用 `definition` 的规范化 JSON 计算。

---

## 12. 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| R1 破坏生产链路 | 3 档 H3 工作流正在跑真实任务，`executor` 编译段是唯一入口 | 抽取为纯函数时保持行为等价，先加「新旧编译结果对比」测试（同 params 双跑 diff） |
| R2 历史任务复现断裂 | 现有 `version` 可变，历史任务已无法复现 | 新表上线后**只对之后的变更生效**；历史 `input_snapshot` 不动，报告里明写「历史版本不可回溯」 |
| R3 D6 决策冲突 | ref_audio 注入 vs TTS 解耦 | 提交前必须由用户裁决，不擅自删功能也不擅自保留 |
| R4 前端选择器破坏现有交互 | 4 个生成入口（角色/场景/道具/分镜/视频 + 重生成）都要接 | 先做后端 + 组件，页面逐个接（每页一次提交） |
| R5 `parsed_schema` 双格式 | 若直接改 seed 格式，`SettingsView` 与 `validate` 会受影响 | 新版统一为 dict 形状，读取方加一次性兼容分支，然后删旧分支 |
| R6 无鉴权 | 公网 :80 无鉴权，新增上传/发布/测试端点会扩大攻击面 | 上传安全上限（D14）与鉴权必须同批；用户已决定「暂不加鉴权」，则至少加大小/节点数/深度限制 |

---

## 13. 测试基线（本次实测）

```
python -m pytest tests --collect-only -q   → 699 tests collected
pytest tests/test_clip_inject_and_dedup.py tests/test_workflow_negative_binding.py \
       tests/test_prompt_ref_consistency.py -q  → 22 passed in 4.00s
alembic heads → d2a8b5f14c37 (head)；alembic current → d2a8b5f14c37
GET /api/workflows → 200，10 条，capabilities/parsed_schema 可读
```

**新增测试必须覆盖（对应计划 §20，但按真实形态）**：`schema_builder`（5 种 params + 3 种输出）、`validator`（画布格式/缺输出/宽高单侧/连线占位）、`runtime_checker`（stub `/object_info`）、`matcher`（能力不足/参考图超限/项目默认优先级）、`compiler` 等价性（新旧双跑）、`output_parser`（零产物/多产物）、版本不可变（改 definition 后旧 job 的 `workflow_version_id` 不变）。

---

## 14. 建议交付顺序（按真实缺口重排，替代计划 §21）

| 阶段 | 内容 | 验收（可执行） |
|---|---|---|
| **P1** | `schema_builder` + `validator` 接入上传端点；修 D1/D2/D7/D9/D14；`workflow` 加 alias/original_filename/file_hash | `POST /api/workflows` 上传 `wf_image_t2i_qwen21_turbo.json` → `parsed_schema.params` 含 `prompt/negative/width/height/seed`，`outputs=['cover']`；上传画布格式返回 `workflow_format_invalid`；`validate` 对合法工作流返回 `valid:true` |
| **P2** | `workflow_version` 表 + 迁移 + PATCH 改「复制为新版本」+ `/versions` `/schema` `/parse` | 连续 PATCH 两次 → `versions=[1,2,3]`，v1 `definition` 不变；旧 job 的 `workflow_version` 仍指向 v1 内容 |
| **P3** | `runtime_checker`（`/object_info` 依赖核对）+ `/test` 真实测试生成 | 对缺少节点的实例返回 `workflow_dependency_missing` 并列出缺失 `class_type` |
| **P4** | `matcher` + 项目默认工作流（`project.settings`）+ `StoryboardView` 去 `endsWith(name)` | 无默认时按能力匹配；设项目默认后优先命中；显式指定不可用工作流返回明确错误（不静默切换） |
| **P5** | 前端 `WorkflowSelector` + `WorkflowParameterForm` + 管理页/详情页 | 上传新工作流后不改前端代码，生成页即可选择并渲染出对应参数表单 |
| **P6** | `RegenWorkbench`/`ShotGenerateTab` 接入 + `job_input_ref` 全链路写入 + 文档更新 | 重新生成页可切工作流、改参数、增删参考图；任务详情可按槽位查参考图 |

D6（ref_audio）与 R6（鉴权）**不进任何阶段，先裁决**。

---

## 15. 需要用户裁决的 3 件事 → 已裁决 2 件（2026-11-18 v2 计划）

| 议题 | 结论 | 依据 |
|---|---|---|
| D6 TTS 音频是否进 H3 | **进 H3（保留现状）** | v2 计划 §8「不得将 TTS 和 H3 设计成互不关联的独立生产链路」；§24 第 7 条 |
| workflow_type 枚举 | **保留 `image/video/tts/bgm/sfx`**，细分类型（`video_ref2v`、`image_i2i` 等）放进 `capabilities.generation_modes` | AGENTS.md 硬约束 #6；v2 计划 §4.1 也写「类型是分类信息，不等于能力」 |
| 上传端点鉴权 | 仍待确认（现状公网 :80 无鉴权） | 用户 2026-11 决定「暂不加鉴权」，本报告仅重申风险 |

---

## 附录 A：审计中用到的真实命令

```bash
cd /home/ubuntu/director-lab
git log --oneline -3
python3 -c "import sqlite3;db=sqlite3.connect('backend/data/director_lab.db');print(db.execute('PRAGMA table_info(workflow)').fetchall())"
curl -s http://127.0.0.1:8000/api/workflows | python3 -m json.tool | head -40
python3 -c "import json;print(json.load(open('backend/data/workflows/wf_video_h3_ref2va_std_5s.meta.json'))['capabilities']['reference_slots'])"
cd backend && .venv/bin/python -m pytest tests --collect-only -q | tail -1
```

---

# 附录 B：TTS → H3 音频参考输入真实调用链（v2 计划 §2.2 必查项，逐条回答）

> 本节是 v2 计划「必须重点核实的音频调用链」的答复，全部结论来自代码与工作流 JSON，不含推测。

## B.1 全链路（带文件:行号）

```
AudioSegment（台词/旁白片段，含 text / character_id / speaker）
  models/audio.py:83
        │  POST /api/audio/segments/{id}/regenerate        api/v1/audio.py:101
        ▼
_resolve_voice                                             api/v1/audio.py:208
  片段 voice_profile_id → 角色默认 VoiceProfile → Character.voice_id（旧兼容）→ 项目 narrator_voice
        │  params = {tts_text, tts_voice(=voice_id), tts_lang, tts_emotion}
        │  工作流路由：capabilities.voice_mode == "preset" 且 voice_values 非空
        ▼
TTS 工作流（wf_tts_qwen3_custom_voice）
  节点 2 Qwen3CustomVoice：$tts_text.text! / $tts_voice.speaker /
                          $tts_lang.language / $tts_emotion.instruct / $seed
  节点 3 SaveAudioMP3：$output.voice
        │  worker 执行 → extract_outputs → download_file
        ▼
Asset(asset_type="audio")，文件 media/<ast_id>_v<n>.mp3      worker/executor.py:1620+
        │  _sync_audio_segment_after_generate                worker/executor.py:301
        ▼
AudioSegment.audio_asset_id + generation_status="completed"
  models/audio.py:121/125；review_status 独立（:127），人工审核后 = "approved"
        │  生成 H3 视频任务（generate clip / regenerate clip）
        ▼
_shot_ref_audios                                            worker/executor.py:49
  过滤 track_type∈(dialogue,narration) & generation_status=completed & review_status=approved
  按 speaker_key = character_id or track_type 去重保序，最多 3 条，累加 _ref_audio_ms
        │  media_files["ref_audio_0..2"] = 资产路径
        ▼
媒体上传（upload_image/LoadAudio 走同一 upload 接口）→ 文件名回填 params["ref_audio_N"]
        │  prune_unbound_media_inputs → compact_* → inject_params
        ▼
ComfyUI: 节点 40/41/42 LoadAudio（$ref_audio_0..2.~audio）
         → 节点 6 MiniMaxH3ReferenceToVideo.ref_audios.ref_audio_0/1/2
        │  length 由 _ref_audio_ms 反算：frames=ceil((ms/1000+0.3)*24)，n=ceil((frames-5)/17)，length=17n+5
        ▼
节点 16 SaveVideo：$output.clip（mp4，内嵌音轨；capabilities.audio=true，audio_output_mode=embedded）
        │  _probe_clip_and_update_shot                        worker/executor.py:230
        ▼
audio_plan.register_native_audio（track_type="native_audio"）  worker/executor.py:285
        │
        ▼
合成：compose.py:64-65 drop_native_audio / keep_native_audio
      项目策略 native_audio_policy ∈ discard|preserve|preserve_ambience_only|native
      （api/v1/audio.py:438-447）                                    → 避免对白重复叠加
```

## B.2 §2.2 十问逐条答复

| # | 问题 | 结论 | 证据 |
|---|---|---|---|
| 1 | 角色音色 ID 存在哪里 | `voice_profile.voice_id`（Qwen3 的 speaker_id，来自工作流 meta `voice_values`）+ `character.voice_id`（旧兼容字段）+ 项目 `settings.narrator_voice`（旁白） | `models/audio.py:36/157`、`models/narrative.py:84`、`audio.py:208-240` |
| 2 | TTS 用哪个节点接收 voice_id | 节点 2 `Qwen3CustomVoice.speaker`，标记 `$tts_voice.speaker`（与参数名 `tts_voice` 不同名，必须显式字段映射） | `wf_tts_qwen3_custom_voice.json` |
| 3 | TTS 音频保存在哪 | `Asset(asset_type="audio")`，路径 `media/<ast_id>_v<n>.<ext>`；`AudioSegment.audio_asset_id` 指向当前版本，旧版本保留 | `executor.py:301`、`models/audio.py:121` |
| 4 | TTS 音频通过什么参数进 H3 | 媒体参数 `ref_audio_0/1/2`，上传后注入为 ComfyUI 文件名 | `executor.py:872-878` |
| 5 | H3 用哪个音频输入节点 | 节点 40/41/42 `LoadAudio`（`$ref_audio_0..2.~audio`）→ 节点 6 输入键 `ref_audios.ref_audio_0/1/2` | `wf_video_h3_ref2va_{std_5s,fast_5s,quality_10s}.json` |
| 6 | 多角色/多片段绑定逻辑 | 按 `speaker_key = character_id or track_type` 去重保序，每说话人取该镜第一条已审核片段，最多 3 条 | `executor.py:49-85` |
| 7 | 音频是否影响时长 | **是**。`_ref_audio_ms` 累加后反算 `length`（17n+5，fps=24），配音优先于 `shot.duration` | `executor.py:890-900` |
| 8 | H3 输出形态 | 单一 `SaveVideo` 产出 mp4，音轨内嵌（`audio_output_mode=embedded`）；内嵌音轨另登记为 `native_audio` 片段 | 工作流节点 16 + `executor.py:285` |
| 9 | 重生成视频是否复用已有 TTS | **是（自动）**。`_shot_ref_audios` 每次按「当前已审核片段」实时解析，不重跑 TTS | `executor.py:49`（无缓存、无重算） |
| 10 | 改台词/音色后哪些下游要重跑 | 改台词→`audio/segments/{id}/regenerate` 产新版本 → 旧 `native_audio`/合成 `stale`；改音色→`VoiceProfile` 变更 → 该角色配音失效；视频需重生成才能用新音轨 | `audio.py:101`、`docs/agent/voice-matching-*.md` |

## B.3 `voice_id` / `voice_audio` / `voice_description` / `voice_conditioning` 的真实对应

| 计划概念 | 本项目真实落点 | 说明 |
|---|---|---|
| `voice_id` | `VoiceProfile.voice_id` = Qwen3 `speaker` 值（如 Vivian） | 仅预设模式下有效 |
| `voice_audio` | `ref_audio_0..2` 注入的 **本镜实际对白音频**（TTS 产物） | 承载台词，H3 用它对齐口型/音色 |
| `voice_reference_audio` | `wf_tts_qwen3_voice_clone` 的 `$tts_voice_ref.~audio!`（LoadAudio 节点 2） | 音色克隆参考，不承载本镜台词，**只在 TTS 工作流内部** |
| `voice_description` | `VoiceProfile.voice_name / description / default_instruct` | 展示与 instruct 用，不进 H3 |
| `voice_conditioning` | `capabilities.voice_mode ∈ preset / clone_reference_audio` | 决定注入 `tts_voice` 还是 `tts_voice_ref` |

**关键区分（计划 §8.3 提醒的那一点已核实）**：H3 的 `ref_audio_N` 是 `voice_audio`（本镜台词音频），TTS 克隆工作流的 `tts_voice_ref` 是 `voice_reference_audio`。二者不可混用；当前代码没有混用。

## B.4 已发现的 4 个音频链路缺口（v2 计划 §8.6/§8.7 要补的）

| 缺口 | 证据 | 影响 |
|---|---|---|
| A1 音频槽位缺 `character_id` 绑定快照 | `media_files["ref_audio_N"]` 只有路径，`input_snapshot.injected_media` 也只存路径 | 无法回答「Picture/ref_audio_2 是谁的声音」，违反 §8.6「保存合并前的独立音频和角色映射」 |
| A2 第 4 个说话人被静默丢弃 | `executor.py:78` `if speaker_key in seen or len(paths) >= 3: continue` | 4 人对话镜头第 4 人无音频，且无 warning |
| A3 音频未做格式/时长校验 | 上传前无采样率/声道/时长检查（H3 参考音频约束未声明） | 违反 §7.6、§8.4 |
| A4 TTS 复用无依赖指纹 | 复用判据只靠 `review_status=approved` | 违反 §13.5「依赖指纹未变化才允许复用」；台词改了但旧片段仍 approved 时会误复用（由片段状态机兜住，但无指纹校验） |

---

# 附录 C：v2 计划与现状的差异增量（需要新增，不是重做）

v2 相对 v1 新增了 4 块范围，本项目全部缺失，需要新建：

| v2 新增范围 | 现状 | 落点 |
|---|---|---|
| **多模态槽位统一模型**（image/audio/video 同一槽位结构，含 semantic/constraints） | `capabilities.reference_slots` 只有图片 + `ref_audio_N` 靠命名约定 | `services/workflow/schema_builder.py` 扩展 `audio_inputs` / `video_inputs` 分组；`validator` 增媒体类型校验 |
| **CapabilityRegistry + 兼容性检查**（`modalities` / `audio.accepts_tts_reference_audio` / `video.supports_last_frame`） | 能力字段是自定义平铺结构（`audio`、`frame_count_*`、`tier`），无统一点路径 | 新增 `services/workflow/capabilities.py`：把现有 meta 归一化，兼容旧字段 |
| **GenerationOrchestrator + 任务依赖**（`workflow_task_dependencies`、`waiting_dependencies` 状态） | `shot_chain`（链式等待前镜尾帧）+ `audio_segment_id` 快照，无通用依赖表 | 新增表 + `services/orchestration.py`；复用现有 `chain` 机制 |
| **项目级工作流配置**（§11.3 / §19） | 文档有键名、代码 0 引用（D8） | `project.settings.default_workflows` 扩展，不建新表 |
| **前端 6 个组件 + 管理页**（§9.1 / §18） | 全部缺失；仅 `SettingsView` 一个 tab | `components/workflow/*` + `views/workflow/*` |

## C.1 Phase 拆分（对齐 v2 计划 §22，映射真实缺口）

> **状态列已于 2026-11-18 晚间按 HEAD `4caed6b` 现场复核更新**（原表停留在 `bb9799d`，滞后 6 个提交）。
> 详细复核见 §E。

| Phase | v2 要求 | 本项目实际要做的（对应缺陷号） | 状态 |
|---|---|---|---|
| P0 审计 | 审计报告 + 音频链路 | 本文件 + 附录 B | **已完成** |
| P1 基础管理 | 上传/解析/元信息/校验/版本 | D1/D2/D4/D7/D9/D14 → 上传解析+校验+alias/file_hash | **已完成** `38f2391` |
| P2 Schema 与能力 | 参数/三模态槽位/输出/能力注册表 | 不可变版本表 + CapabilityRegistry + 三模态分组；D3/D7/D11 已修 | **已完成** `272cb54` |
| P3 编译与执行 | Resolver/Compiler/Executor/OutputParser/MediaManager/Snapshot | 编译与输出解析已有（`workflow_inject` + `executor`）；`runtime_checker` + `/test` 真实提交已补；**matcher / compiled_prompt_hash 仍缺** | **大部分完成** `fa95a4f` |
| P4 TTS↔H3 协同 | 音频产物标准化/槽位/复用/多角色/音轨策略 | A1 角色映射 + A2 超限告警已补；**A3 音频格式校验 / A4 依赖指纹仍缺** | **部分完成** `af6977d` |
| P5 前端动态化 | 6 组件 + 管理页 + 5 个页面接入 | `WorkflowSelector` / `WorkflowParameterForm` / `WorkflowTestPanel` 已建；分镜图页已接；**管理页 / 项目默认 / 角色·场景·道具·分镜视频页仍未接** | **部分完成** `3532ae4`→`4caed6b` |
| P6 集成与迁移 | 回归 + 文档 + 删旧代码 | 待 P4/P5 收尾后执行 | 待做 |

## C.2 v2 计划里必须修正的 3 处（同上文 §9，v2 仍未修正）

1. §4.1 的 `workflow_type` 表（`image_t2i` / `video_ref2v` / `audio_tts`）与仓库枚举冲突 → 用 `capabilities.generation_modes` 表达。
2. §16 的 API 路径（`/api/workflows/upload`、`/api/generation/*`）会**造成重复端点**：仓库已有 `POST /api/workflows`、`POST /api/shots/{id}/generate` 等 9 个生成端点，应扩展现有路径而非新增平行路径。
3. §14 的表名（`workflows` / `workflow_versions` / `generation_tasks`）与现有单数表名（`workflow` / `job`）不一致 → 沿用现有命名，只加 `workflow_version` / `workflow_task_dependency` 两张新表。

---

# 附录 D：实机运行环境检查结果（P3 上线首跑，2026-11-18）

`POST /api/workflows/{id}/check-runtime` 对当前实例（`https://giant-pick-karen-newest.trycloudflare.com`，1066 个 class_type）逐份检查的结果：

| 工作流 | runtime_status | 缺失节点 | 缺失模型 |
|---|---|---|---|
| `wf_image_t2i_qwen21_turbo` | **ready** | — | — |
| `wf_image_ref_qwen21_multi` | **ready** | — | — |
| `wf_video_h3_ref2va_{fast,std,quality}` | missing_deps | — | `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`、`minimax_h3_video_vae_int8_convrot.safetensors` |
| `wf_video_h3_flf2v` / `wf_video_h3_t2v` | missing_deps | — | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` |
| `wf_tts_qwen3_custom_voice` / `wf_tts_qwen3_voice_clone` | missing_deps | `Qwen3Loader`、`Qwen3CustomVoice`（Qwen3-TTS 节点包未装） | — |
| `wf_bgm_stable_audio` | missing_deps | — | `stable-audio-open-1.0.safetensors`、`t5-base.safetensors` |

已用原始 `/object_info` 逐名核对（`in raw_json` 判定），**不是检查器的误报**：
实例上有 `minimax_h3_video_vae_fp16.safetensors`，但工作流引用的是 `..._int8_convrot.safetensors`；turbo LoRA 与 fl2va 权重均未安装。

结论：
1. **当前实例只能跑图片链路**（两个 image 工作流 ready）。视频三档、FLF2V/T2V、TTS、BGM 均缺依赖——这与隧道从 `december-else-audience-dash` 换成 `giant-pick-karen-newest` 一致，说明这是一台**新的/未备齐权重的实例**，不是代码回归。
2. 这正是「解析成功 ≠ 能执行」的实例证据：上传/静态校验全部通过的 H3 工作流，在此实例上提交必然失败。
3. 因此 P4/P5 的真实生成验收必须先补齐实例权重（或把 `comfyui_instances` 指回原实例），否则只能跑 mock。

---

# 附录 E：2026-11-18 晚间现场复核（HEAD `4caed6b`）

> 本附录替代 C.1 状态表的旧口径。全部结论来自现场命令，不是引用旧记录。

## E.1 复核命令与实测结果

```bash
cd /home/ubuntu/director-lab
git log --oneline -6            # 4caed6b / 03a6f2b / e8460dc / 2657847 / 4797f73 / af6977d
git status --short              # 仅 script/llm/ParseView 等解析链脏文件（并行会话），工作流相关文件干净
cd backend && .venv/bin/alembic heads    # b8d2f5c1e9a3 (单头)
curl -s http://127.0.0.1:8000/api/workflows  # 10 条，全部 is_default=false，alias 全 NULL
python3 -c "sqlite3 PRAGMA table_info(workflow)"  # 20 列，含 alias/original_filename/file_hash/current_version_id/status/validation_status/runtime_status
select count(*) from workflow_version    # 10 行（seed 已回填 v1..v6）
```

工作流相关测试子集实测 **64 passed**（`test_workflow_upload_and_schema` / `test_workflow_versions_and_capabilities` / `test_workflow_runtime_check` / `test_audio_slot_bindings` / `test_clip_inject_and_dedup` / `test_workflow_negative_binding` / `test_prompt_ref_consistency` / `test_audio_policy`）。

## E.2 逐项复核：计划 §22 的 7 个 Phase 真实状态

| 能力 | 计划要求 | 真实状态 | 证据 |
|---|---|---|---|
| 上传 ComfyUI API Format | P1 | ✅ | `POST /api/workflows` 先 `validate_definition` 再 `build_schema`，落 `file_hash` |
| 自动解析节点/输入/输出 | P1 | ✅ | `schema_builder.build_schema` 复用 `parse_workflow`，H3 出 17 参数 |
| alias / original_filename / file_hash | P1 | ✅ 字段就绪，**但全表 alias=NULL** | `PRAGMA table_info(workflow)` 20 列；`seed_workflows.py:114` 未写 alias |
| 不可变版本 + 发布/回滚 | P2 | ✅ | `workflow_version` 10 行；`/versions` `/publish` `/rollback` 均在 |
| CapabilityRegistry + 兼容性检查 | P2 | ✅ | `capabilities.normalize` / `capability_matches`（返回缺失点路径） |
| 三模态 Schema 分组 | P2 | ✅ | `normalize` 出 `reference.slots`，`modalities.{image,audio,video}_input` |
| 运行环境依赖检查 | P3 | ✅ 且已实测 | `/check-runtime`：2 image ready，H3/TTS/BGM 均 missing_deps |
| 真实测试提交 | P3 | ✅ | `POST /workflows/{id}/test` 202 + `GET /workflows/test/{prompt_id}` |
| **matcher 服务（能力匹配 + 原因）** | P3/P4 | ❌ **缺** | `grep -rn "capability_matches" backend/app` 只有定义，0 调用方 |
| **项目级默认工作流** | P4/§11.3 | ❌ **缺** | `grep -rn "default_workflows\|default_image_workflow_id"` = 0 命中 |
| A1 音频槽位角色映射 | P4 | ✅ | `_group_audio_inputs` 返回 bindings，写 `input_snapshot.audio_bindings` |
| A2 第 4 说话人显式告警 | P4 | ✅ | overflow → `job.audio_slot_overflow` 事件 |
| **A3 音频格式/采样率/时长校验** | P4/§7.6 | ❌ **缺** | `grep "sample_rate\|audio_constraints"` 在 workflow/ 与 audio.py 均 0 命中；meta 无音频约束声明 |
| **A4 TTS 复用依赖指纹** | P4/§13.5 | ❌ **缺** | 复用判据仍只有 `review_status=approved`（`executor.py:_shot_ref_audios`） |
| WorkflowSelector / ParameterForm | P5 | ✅ | `components/workflow/*.vue` 3 个文件 |
| 分镜图页接入 | P5 | ✅ | `StoryboardImagesView.vue:570` |
| **独立工作流管理页** | P5/§18 | ❌ **缺** | `frontend/src/views/workflow/` 不存在；仍在 `SettingsView` 单 tab |
| **角色/场景/道具页接入** | P5/§10.1-3 | ❌ **缺** | `AssetsView.vue` 调 `generateCharacterPortrait(id)` 不传 workflow_id |
| **分镜视频页接入选择器** | P5/§10.5 | ⚠️ **半接** | `StoryboardView` 按 `capabilities.tier` 推导 workflow_id（D5 已修但非选择器）；`ShotGenerateTab` 无工作流选择 |
| **视频/图片重新生成弹窗** | P5/§10.6-7 | ⚠️ **部分** | `RegenWorkbench` 已接选择器；`ShotGenerateTab` 未接 |

## E.3 TTS → H3 音频链路复核（v2 §2.2 十问，本轮重新验证）

**结论：链路完好，无回归。** 逐条复核：

| # | 问题 | 复核结果 | 现场证据 |
|---|---|---|---|
| 1 | 音色 ID 存哪 | 未变 | `voice_profile.voice_id` / `character.voice_id` / `project.settings.narrator_voice`（`audio.py:208 _resolve_voice`） |
| 2 | TTS 哪个节点收 voice_id | 未变 | `wf_tts_qwen3_custom_voice` 节点 2 `Qwen3CustomVoice.speaker` ← `$tts_voice.speaker` |
| 3 | TTS 音频存哪 | 未变 | `Asset(asset_type="audio")`；`AudioSegment.audio_asset_id`（`models/audio.py:121`） |
| 4 | 凭什么参数进 H3 | **已增强** | `ref_audio_0/1/2`，且现在带 `audio_bindings` 角色映射 |
| 5 | H3 用哪个音频节点 | 未变 | 节点 40/41/42 `LoadAudio` → 节点 6 `ref_audios.ref_audio_0/1/2` |
| 6 | 多角色绑定 | **已增强** | `speaker_key = character_id or track_type` 去重保序；第 4 人进 overflow 并告警（A2 已修） |
| 7 | 音频影响时长吗 | 未变 | `_ref_audio_ms` → `frames=ceil((ms/1000+0.3)*24)`，`n=ceil((frames-5)/17)`，`length=17n+5`（`executor.py:948-957`） |
| 8 | H3 输出形态 | 未变 | 节点 16 `SaveVideo` 单 mp4 内嵌音轨；`audio_plan.register_native_audio` |
| 9 | 重生成复用 TTS | 未变（**但仍无指纹**） | `_shot_ref_audios` 每次实时查 approved 片段；A4 未做 |
| 10 | 改台词/音色重跑什么 | 未变 | 台词→segment regenerate 产新版本；视频须重生成 |

**四个概念仍严格区分**（无混用）：
- `voice_id` = `VoiceProfile.voice_id`（Qwen3 speaker 值）
- `voice_audio` = H3 `ref_audio_N` 注入的**本镜台词音频**
- `voice_reference_audio` = TTS 克隆工作流的 `$tts_voice_ref.~audio!`（只在 TTS 内部）
- `voice_conditioning` = `capabilities.voice_mode ∈ preset / clone_reference_audio`

## E.4 本轮确认的剩余缺口（按优先级）

| ID | 缺口 | 计划条款 | 影响 |
|---|---|---|---|
| **G1** | 无 matcher 服务；`capability_matches` 无调用方 | §11.1/§11.2 | 显式指定不兼容工作流不会被拦截；无「缺失能力」明确报错 |
| **G2** | 无项目级默认工作流 | §11.3/§19 | 每次只能靠 `is_default`（当前全 false）或插入顺序兜底 |
| **G3** | A3 音频输入无格式/采样率/声道/时长校验 | §7.6/§8.4 | 不符合 H3 约束的音频直接上传，失败在 ComfyUI 侧 |
| **G4** | A4 TTS 复用无依赖指纹 | §13.5 | 台词/音色改了但旧片段仍 approved 时会误复用 |
| **G5** | 无独立工作流管理页 | §18 | 参数绑定/槽位配置/测试运行无 UI |
| **G6** | 角色/场景/道具/分镜视频页未接选择器 | §10.1-10.5 | 新增工作流仍需改页面代码，违反 §25 最终目标 |
| **G7** | `alias` 全表 NULL | §4.2/§4.3 | 稳定别名未落地，业务引用只能靠 id |
| **G8** | `is_default` 全 false | §11.1 | 匹配链第一级恒空 |

## E.5 环境约束（影响验收方式）

`/check-runtime` 实测：**当前 ComfyUI 实例只有 2 个 image 工作流 ready**，H3 三档 / FLF2V / T2V / TTS / BGM 全部 `missing_deps`（缺 `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`、`minimax_h3_video_vae_int8_convrot.safetensors`、Qwen3-TTS 节点包等）。

因此 G1–G8 的验收口径：
- **代码/契约层**（matcher、能力校验、指纹、Schema、前端组件）→ 可用真实工作流 JSON + 单元测试验收。
- **真实生成层**（H3 出视频、TTS 出音频）→ 必须先在实例上补齐权重，或把 `comfyui_instances` 指回原实例；否则只能以 `missing_deps` 明确报错为验收结果，**不得用 mock 声称已通过**。
