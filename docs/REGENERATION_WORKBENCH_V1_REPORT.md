# 多图参考重新生成工作台 V1 实施报告

实施日期：2026-10-27
范围：分镜参考图、分镜视频的多图参考重新生成机制

---

## 一、真实工作流审查结论

### 1.1 工作流实际存放位置与装载方式

工作流不在代码中硬编码，而是存放于 `backend/data/workflows/` 下的 `wf_*.json` 与配套 `wf_*.meta.json`，由 `scripts/seed_workflows.py` 解析后写入数据库 `workflow` 表，保存 `definition`、`capabilities`、`parsed_schema`、`version`。版本规则为内容变化才 version+1，内容一致不虚增版本。

### 1.2 目标工作流的真实能力

| 工作流 | 类型 | 参考槽位 | 数量上限 | 关键约束 |
|---|---|---|---|---|
| wf_image_t2i_qwen | image / keyframe | 无 | 0 | 不支持任何参考图；steps/cfg/sampler 为结构参数，不可运行时改写 |
| wf_image_ref_qwen_edit | image / keyframe | ref_identity、ref_costume、ref_scene | 3 | TextEncodeQwenImageEditPlus(image1..3)；未注入槽位须在提交前剔除 image{n} 连线 |
| wf_video_h3_ref2va | video / clip | ref_prev_frame、ref_image、ref_identity、ref_costume、ref_scene、ref_prop | 9 | 分别映射 ref_image_0..5；first_frame 与 end_frame 均为 false；prev_frame 为 true；提示词用 `<Picture N>` 引用 |

对照工作流：`wf_video_h3_flf2v` 支持首帧 + 可选尾帧，是真正的首帧约束能力；`wf_video_h3_t2v` 无参考槽位。

### 1.3 参考图进入模型的真实路径

`ref_inject.build_media_params` 按提示词中的 `@名称` 解析实体，再按工作流 `capabilities.reference_slots` 声明的槽位依次填充：角色 → ref_identity、ref_costume（按出现顺序，最多 2 个）；场景 → ref_scene；道具 → ref_prop。

`RESERVED_SLOTS` 包含 ref_image、ref_prev_frame、first_frame、end_frame，@引用不得占用。引用超出槽位返回 422，未匹配引用返回 invalid_reference，资产缺失返回 reference_asset_missing（409）。参考图确实通过工作流媒体参数进入模型，不存在仅写入提示词文本的情况。

### 1.4 数据库原有保存内容

- `asset` 表：version、parent_id、file_path、thumb_path、source_job_id、validity、input_snapshot、quality_status。
- `job` 表：workflow_id、workflow_version、params、input_snapshot、request_key、submission_state、status。
- 执行器在提交前写入 input_snapshot：injected_params、injected_media、params、workflow_version、comfyui_instance。

### 1.5 审查发现且已确认的关键事实

1. 原有执行器落库时资产 `version` 固定为 1，未按版本递增。
2. 自动生成流程**不会**建立 `AssetRef` 镜头-资产关联记录，镜头侧产物实际通过 `Job.shot_id` 反查。这一点直接影响所有按 `AssetRef` 查询的实现方式。
3. 原有 `POST /shots/{id}/regenerate` 在提交阶段即把当前资产标记为 superseded，不符合候选先审核的产品规则。
4. 参考图只以最终文件路径记录（injected_media），未结构化保存 asset_id、version、用途、必需性与槽位映射。

---

## 二、实施内容

### 2.1 数据层

新增迁移 `d1e2f3a4b5c6_job_input_ref_and_idempotency.py`：

- 新表 `job_input_ref`：job_id、slot_param、input_key、asset_id、asset_version、asset_type、role、required、file_path、semantic、enabled、seq、created_at。索引 `(job_id)`、`(asset_id)`。
- `job.request_key` 条件唯一索引 `ux_job_request_key`（仅非空值生效），实现提交级幂等，历史 NULL 数据不受影响。

模型：`app/models/production.py` 新增 `JobInputRef`；`app/models/ids.py` 新增 `job_input_ref_id()`；`app/models/__init__.py` 导出。

### 2.2 核心服务 `app/services/regeneration.py`

- `normalize_reference_slots()`：归一化工作流槽位声明，兼容字符串数组与对象数组两种真实格式，并标注 `semantic`（reference / frame_constraint）。
- `build_base_config()`：从指定基础版本反查原始生成配置，配置缺失时返回 `snapshot_complete=false` 与具体原因，禁止用默认值填充。
- `validate_refs()`：校验槽位是否被工作流支持、必需槽位是否保留、资产与版本是否存在、文件是否可读、资产类型是否与槽位用途匹配。
- `assemble_snapshot()`：按三种模式合并继承项与用户改动，输出冻结快照并计算内容哈希（排除生成时间，保证相同输入哈希稳定）。
- `write_input_refs()`：把快照中的参考图结构化为 `job_input_ref` 记录。
- `snapshot_to_media()`：把启用槽位转成工作流媒体参数。

### 2.3 接口 `app/api/v1/regeneration.py`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/shots/{id}/regenerate/config | 读取指定基础版本的原始生成配置与只读约束 |
| POST | /api/shots/{id}/regenerate/workbench | 按三种模式提交重新生成任务（带幂等键） |
| GET | /api/jobs/{id}/snapshot | 查看冻结快照与结构化参考图 |
| GET | /api/assets/{id}/candidates | 列出候选版本 |
| POST | /api/assets/{id}/adopt | 采用候选版本 |
| GET | /api/shots/{id}/impact | 查询受影响的后续镜头 |

说明：提交入口使用独立路径 `/regenerate/workbench`，因为项目原本已存在 `POST /shots/{id}/regenerate` 重做接口，同路径会导致新逻辑被遮蔽。

### 2.4 执行器改造 `app/worker/executor.py`

- 新增 `_next_asset_version()`：同镜头同类型版本号递增，不再固定为 1。
- 重新生成任务的产物落为 `validity=candidate`，不覆盖当前生效版本。
- 正常下载落库与异常恢复落库两条路径均同步改造，避免绕过候选机制。

### 2.5 前端

- `frontend/src/api/regeneration.ts`：新增接口封装。
- `frontend/src/components/RegenerateConfigPanel.vue`：新增参考图与生成配置面板，包含模式切换、基础版本信息、工作流信息、参考图卡片（缩略图/槽位/用途/版本/必需状态/启用开关）、帧约束独立分区、正负提示词编辑、本次不修改项清单、候选版本列表与采用操作。
- `frontend/src/views/project/StoryboardView.vue`：在镜头详情抽屉接入面板，并按镜头已有产物自动判断展示分镜图或分镜视频配置。

---

## 三、测试与验证结果

### 3.1 新增测试

`tests/test_regeneration_workbench.py`（12 项，全部通过）：

1. 真实工作流槽位与帧约束语义以元信息为准
2. t2i 工作流无参考槽位，不凭空补造
3. Qwen Edit 支持三个槽位
4. 仅改提示词时参考图集合与参数逐项不变
5. 替换角色参考图仅该槽位变化
6. 移除可选槽位后不再注入
7. 必需槽位移除被阻止
8. 参考图文件丢失返回明确错误
9. 资产类型错配返回明确错误
10. 历史配置不完整阻止默认模式
11. 快照哈希稳定
12. 配置接口返回基础版本、工作流版本与只读约束

`tests/test_regeneration_candidates.py`（6 项，全部通过）：

1. 相同幂等键重复提交只产生一个任务
2. 候选列表只返回候选版本
3. 采用候选后候选转生效、原生效版本转历史
4. 非候选版本不可被采用
5. 提交被拒时当前生效版本不受影响
6. 实体类候选采用后同一资产只保留一个生效版本，其他实体不受牵连

### 3.2 回归结果

- 后端全量：`203 passed`
- 前端构建：`✓ built in 6.62s`，产物正常输出

---

## 四、实施过程中修正的真实缺陷

1. **接口遮蔽**：新增提交接口与既有重做接口同路径，导致请求实际走旧逻辑，返回无快照、空提示词。改为独立路径 `/regenerate/workbench`。
2. **重复入口覆盖既有约束**：曾新增 `POST /jobs/{id}/retry` 与既有重试入口冲突，覆盖了『同目标已有在途任务时拒绝重试』的 409 约束，导致既有测试失败。已移除重复入口。
3. **镜头产物查询口径错误**：按 `AssetRef` 查当前版本，但自动生成流程不建立该记录，导致接口查不到产物。改为按来源任务 `Job.shot_id` 反查，与既有镜头详情口径一致。
4. **采用候选未转历史**：旧版本查询同样依赖 `AssetRef`，导致原生效版本未被替换。已改为按来源任务反查并兼容手动绑定两条路径。
5. **快照哈希不稳定**：原实现把生成时间计入哈希，相同输入得到不同哈希。已排除非内容字段。
6. **实体主图指针未同步**：采用候选后角色定妆图、场景基准图、道具锚定图仍指向旧版本。已补充指针同步，且仅当指针正好指向被替换版本时才切换。

---

## 五、本轮隐患修复（严重项与中等项）

### 5.1 【严重】采用实体类候选导致双 current

原先只在有镜头归属时定位旧版本，角色定妆图、场景基准图、道具锚定图这类无镜头归属的资产会被整体跳过，导致新旧两个版本同时为 current。已在 `adopt_candidate` 增加无镜头归属分支，按「项目 + 类型 + 名称」定位旧 current 并转 superseded，同时把主图指针同步移出条件分支。文件 revision sha256 `527986a2…`（+21/-5）。

### 5.2 【中等】取当前版本口径与既有逻辑不一致

`_current_asset` 原先缺少来源任务成功过滤，与既有镜头详情判定不一致，存在失败任务时可能取到未真正产出的版本。已补充 `Job.status == succeeded` 条件。文件 revision sha256 `82ac4786…`（+3/-0）。

### 5.3 【严重】前端幂等键形同虚设

原先用 `Date.now()` 现算幂等键，每次点击都不同，网络慢时重复点击仍会产生多个任务。已改为 `pendingKey` 在提交期间保持不变、成功后才重置，失败后仍可用同一键安全重试。`RegenerateConfigPanel.vue` revision sha256 `0b88397e…`（3 处编辑）。

### 5.4 【中等】实体资产版本号语义与恢复路径计算顺序

`_next_asset_version` 增加 `asset_name` 参数，无镜头归属时按名称隔离版本，避免同项目不同实体版本号连号；异常恢复落库路径改为先确定产物类型与实体归属再计算版本，修正原实现中类型变量使用早于赋值的顺序问题。`executor.py` revision sha256 `000a575b…`。

### 5.5 【中等】并发提交返回 500

提交时先查后插，并发下第二个请求会撞唯一索引直接抛服务器错误。已捕获 `IntegrityError`，回滚后按幂等键复用已落库任务，确认不是同一幂等键时返回 409 `job_conflict` 可读提示。文件 revision sha256 `ee8ae2ae…`（+20/-1）。

### 5.6 新增覆盖

新增 `test_adopt_entity_candidate_leaves_single_current`，验证实体类候选采用后同一资产只保留一个生效版本，且同项目其他实体不被牵连。

## 六、兼容性与限制

1. 工作流定义零改动，未修改任何现有工作流的生成逻辑与节点参数。
2. 候选版本机制仅作用于重新生成路径（标记 `regeneration=true` 的任务），首次生成仍为 current，历史数据不受影响。
3. H3 ref2va 的 `first_frame` 与 `end_frame` 均为 false，本工作流只有参考图语义，不存在硬首帧约束；首帧约束能力由 `wf_video_h3_flf2v` 提供。面板中已按『参考图』与『帧约束』分区展示，不混淆两者。
4. `ref_prev_frame` 在 H3 ref2va 中映射 `ref_images.ref_image_0`，是上一镜尾帧的真实注入槽位；提示词中的 `<Picture 1>` 对应的是本镜分镜图 `ref_image`，两者不可混为一谈。
5. 上游变更后仅标记下游受影响镜头，不自动批量重生成，需用户逐个确认。
6. 迁移前已备份数据库至 `backend/data/backups/director_lab_20261027_195918.db`。
