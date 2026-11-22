# Director-Lab 重构测试计划与验收记录

> 对应执行计划 §15（测试与验收矩阵）与 §19（文档交付）。
> 最后更新：2026-11-22。
> 口径：自动化测试与真实环境验收分开记录；未执行的项目明确标注，不以 mock 结果替代真实 ComfyUI/GPU 验收。

## 1. 复跑入口

| 项目 | 命令 | 最近结果 |
| --- | --- | --- |
| 后端全量 | `cd backend; python -m pytest -q` | 1334 passed（435.65s，2026-11-22） |
| 后端静态检查 | `cd backend; python -m ruff check .` | 通过 |
| E2E 三样例 | `cd backend; python -m pytest -q tests/test_pipeline_e2e.py` | 3 passed |
| 前端类型与构建 | `cd frontend; npm run build` | 通过 |
| 前端颜色规范 | `cd frontend; node scripts/check-colors.mjs` | 通过 |
| 前端对比度 | `cd frontend; node scripts/check-contrast.mjs` | 通过 |

## 2. 自动化测试矩阵映射（§15.1）

状态口径：
- **已覆盖**：存在直接针对该场景的用例；
- **部分覆盖**：相关路径有校验/用例，但缺该场景的专项断言；
- **未覆盖**：暂无自动化用例，需人工验收或后续补测。

### 2.1 Script Intelligence

| 矩阵项 | 状态 | 对应测试 / 说明 |
| --- | --- | --- |
| 短剧本 | 已覆盖 | `test_script_parse.py`、`test_pipeline_e2e.py::test_short_script_pipeline_end_to_end` |
| 长剧本 | 已覆盖 | `test_script_agent_long_script.py`、`test_script_outline_segmented.py` |
| 多场景 | 已覆盖 | `test_pipeline_e2e.py::test_complex_multi_episode_pipeline`（3 场景） |
| 多人物 | 已覆盖 | 同上（双角色）、`test_keyframe_multi_character_slots.py` |
| 角色别名 | 已覆盖 | `test_script_parse.py`（别名检测与归一） |
| 重复资产 | 已覆盖 | `test_asset_plan.py`（同名资产去重） |
| 无道具 | 部分覆盖 | `test_input_precheck_integration.py` 走 `props: []`；解析侧无专项用例 |
| 关键道具交接 | 已覆盖 | `test_prop_count_qc.py`（交接镜与道具计数） |
| 续集 / 既有资产引用 | 部分覆盖 | `test_series_management.py`、`test_series_impact_analysis.py`；未见「续集复用既有角色/道具」专项用例 |
| 原文证据 | 已覆盖 | `test_source_index.py`、`test_script_analysis.py` |
| LLM JSON 异常 | 已覆盖 | `test_llm_response.py`、`test_llm_retry.py`、`test_llm_truncation_fix.py` |
| 分段失败 | 已覆盖 | `test_script_agent_chunking.py`、`test_script_outline_segmented.py` |
| 局部重跑 | 已覆盖 | `test_reparse_supersedes.py`、`test_parse_replay_no_cross_episode_delete.py` |

### 2.2 Storyboard Planning

| 矩阵项 | 状态 | 对应测试 / 说明 |
| --- | --- | --- |
| 多场景 | 已覆盖 | `test_storyboard_plan.py`（`scene_not_found` / 场景覆盖校验） |
| 一个 Beat 多个镜头 | 部分覆盖 | 服务有 `coverage.beats` 覆盖统计，缺专项用例 |
| 多 Beat 一个镜头 | 部分覆盖 | `beat_ids` 数组校验有实现，缺专项用例 |
| 时长超限 | 部分覆盖 | `MAX_DURATION_SECONDS` 校验有实现，缺边界用例 |
| 对白时长冲突 | 未覆盖 | 暂无自动化用例，需人工/后续补测 |
| 资产 ID 缺失 | 已覆盖 | `test_invalid_plan_reports_structural_and_reference_errors`（`asset_not_found`） |
| 原文覆盖检查 | 部分覆盖 | `coverage` 有 warning 输出，缺按矩阵逐项的断言 |
| 已确认镜头锁定 | 已覆盖 | `test_confirmed_shot_reuse_is_reported_and_not_overwritten`、`test_apply_skips_locked_shot_fields` |
| 写入前字段差异预览 | 已覆盖 | `test_validate_reports_field_changes_before_apply`、`test_validate_omits_unchanged_fields_from_diff` |
| 只读校验零写入 | 已覆盖 | `test_valid_plan_maps_to_shot_preview_without_writing`、`test_apply_rejects_invalid_plan_without_writing` |

### 2.3 Binding Engine

| 矩阵项 | 状态 | 对应测试 / 说明 |
| --- | --- | --- |
| 槽位声明与工作流对齐 | 已覆盖 | `test_workflow_slot_alignment.py` |
| 多角色槽位分配 | 已覆盖 | `test_keyframe_multi_character_slots.py`、`test_three_characters_slots.py`、`test_multichar_prompt_and_slots.py` |
| 槽位超限 / 重复 | 已覆盖 | `test_prompt_compiler.py`（`reference_capacity_exceeded`）、`test_ref_and_parallel.py`、`test_workflow_slot_alignment.py` |
| 参考资产失效 | 已覆盖 | `test_ref_slot_validity_and_seq.py` |
| 负面约束绑定 | 已覆盖 | `test_workflow_negative_binding.py` |
| 工作流匹配与项目默认 | 已覆盖 | `test_workflow_matcher.py`、`test_workflow_resolve_api.py` |

### 2.4 图片生成

| 矩阵项 | 状态 | 对应测试 / 说明 |
| --- | --- | --- |
| mock 渲染全流程 | 已覆盖 | `test_mock_render.py`、`test_pipeline_e2e.py` |
| 批量生成与幂等 | 已覆盖 | `test_batch_generate_idempotent.py`、`test_batch_inflight_dedup.py` |
| 部分批次失败 | 已覆盖 | `test_partial_failure_status.py` |
| 候选保存与重生成 | 已覆盖 | `test_regeneration_candidates.py`、`test_asset_replace_regen.py` |
| 任务取消 | 已覆盖 | `test_job_cancel.py` |
| 重启恢复与对账 | 已覆盖 | `test_task_recovery_reconcile.py` |

### 2.5 分镜板与任务

| 矩阵项 | 状态 | 对应测试 / 说明 |
| --- | --- | --- |
| 五态状态机 | 已覆盖 | `test_pipeline_state.py`、`test_stage_readiness.py` |
| 失败 / 取消 / 已替代 | 已覆盖 | `test_progress_failed_superseded.py` |
| 输入预检分级放行 | 已覆盖 | `test_precheck_graded_release.py`、`test_input_precheck_api.py` |
| 任务快照与历史版本 | 已覆盖 | `test_job_snapshot_versions.py` |
| 事件流 | 已覆盖 | `test_event_stream_pool.py`、`test_workspace_events.py` |

### 2.6 视频与连续性

| 矩阵项 | 状态 | 对应测试 / 说明 |
| --- | --- | --- |
| 同集链式尾帧 | 已覆盖 | `test_shot_chain.py`、`test_clip_inject_and_dedup.py`、`test_pipeline_e2e.py`（样例 C） |
| 跨集首帧衔接 | 已覆盖 | `test_continuity.py`（末帧提取、首帧注入、版本失效） |
| 缺少连续性记录拦截 | 已覆盖 | `test_pipeline_e2e.py`（第 2 集首镜 422 `continuity_check_failed`） |
| 任务依赖门禁 | 已覆盖 | `test_task_dependency_gate.py` |
| 参考图布局 | 已覆盖 | `test_clip_reference_layout.py` |

### 2.7 基础设施

| 矩阵项 | 状态 | 对应测试 / 说明 |
| --- | --- | --- |
| 幂等 | 已覆盖 | `test_idempotency.py` |
| 依赖图与失效传播 | 已覆盖 | `test_dependency_graph.py` |
| 项目删除外键覆盖 | 已覆盖 | `test_project_purge_fk_coverage.py` |
| API 兼容 | 已覆盖 | `test_api_stage3.py` |
| 数据迁移 | 未覆盖 | 本重构未新增 Alembic 迁移，无迁移测试；如后续改表需补 |

## 3. E2E 样例记录（§15.2）

| 样例 | 覆盖路径 | 结果 | 用例 |
| --- | --- | --- | --- |
| A 短剧本快速路径 | 剧本 → 大纲评估 → 解析落库 → 公共资产 → 审核 → 规划只读校验 → 分镜图 → 视频 → 只读影响预检 | 通过 | `test_short_script_pipeline_end_to_end` |
| B 中途失败 → 显式重试 | mock 渲染首次失败 → 任务 `failed` 且零资产 → 显式 retry → 新任务成功且资产唯一 | 通过 | `test_generation_failure_then_retry_succeeds` |
| C 多集复杂路径 | 2 集 / 3 场景 / 双角色 / 双道具 / 6 镜头；场景批量分镜图、同集链式尾帧、跨集拦截 | 通过 | `test_complex_multi_episode_pipeline` |

E2E 约束：LLM 与渲染均为替身，媒体写入临时目录，不触达外网；所有写入由显式 API 触发，规划校验保持 dry-run。

## 4. 真实环境人工验收清单

以下项目无法由 mock 测试替代，需在具备 ComfyUI + GPU 的环境执行并逐项记录。

### 4.1 环境与工作流

- [x] ComfyUI 可访问，`/system_stats` 正常返回（2026-11-22 实测，新隧道 `/api/health` 返回 comfyui healthy）。
- [ ] 图片 / 视频 / TTS / BGM 工作流均能在 ComfyUI 侧手动跑通一次。
- [ ] 工作流槽位声明与实际输入键一致（`test_workflow_slot_alignment.py::test_real_video_workflows_are_aligned` 仅校验声明，不校验实例）。
- [ ] 负面提示词绑定在真实工作流中生效。

### 4.2 生成与失败处理

- [x] 图片链路实测通过（2026-11-22，项目 `prj_a10bc7d70728`）：角色定妆图 110.4s、场景基准图 85.4s、道具锚定图 85.3s、分镜图 150.6s，4/4 任务 `succeeded`，产物落盘 1.76MB / 2.65MB / 2.44MB / 2.37MB，`validity=current`。
- [ ] 单镜视频、批量视频全部成功且资产可预览（当前被模型/节点缺失阻塞，见 §5.5）。
- [ ] ComfyUI 不可用：任务失败并落 `error_code` / `error_msg` / `retryable`，资产库无脏数据。
- [ ] 提交失败：HTTP 非 2xx 时任务进入失败态，可显式重试。
- [ ] 超时：长任务超时后不悬挂，重启恢复能对账到正确状态。
- [ ] 结果收集失败：产物未落盘时任务失败而非假成功。
- [ ] 服务重启：`queued` / `running` 任务恢复或标记失败，无重复产物。

### 4.3 视频连续性

- [ ] 同集多镜串行生成：第 N 镜视频参考第 N-1 镜实际尾帧。
- [ ] 尾帧提取失败：任务成功但记录事件，后续可重试补齐。
- [ ] 跨集衔接：第 1 集合成 → 末帧提取 → 第 2 集首镜首帧注入。
- [ ] 跨集缺失拦截：未合成第 1 集时，第 2 集首镜生成被 422 拦截。

### 4.4 前端工作台

- [x] 分镜图阶段部分生成即可进入分镜看板：`done >= 1` 放行、`done == 0` 拦截；弹窗列出缺失镜头并说明「进入后暂不能生成视频」（2026-11-22 实测，6 镜仅 1 张时 ready=true、blockers=5）。
- [x] 左侧「制作流程」栏与页内按钮的解锁口径一致（同一 `stage-readiness` 接口）：第 05 步由「已锁定」变为「未开始」可点击，第 06 步仍按分镜视频锁定。
- [ ] 分镜板五态展示与实际任务状态一致（2026-11-22 实测：6 张镜头卡正常渲染，含时长、画面/对白类型与「待生成视频 / 未生成」状态标签，控制台 0 错误；五种状态未逐一构造，故未勾选）。
- [x] 规划导入对话框可打开并渲染校验预览（2026-11-22 实测）：「导入规划 JSON」打开对话框，校验后显示「校验未通过 / 错误 2 / 警告 3 / 场景覆盖 0/1 / 节拍覆盖 0/4」、逐条错误与警告、以及「规划外旧镜头（不会删除）」6 个 shot_id；未校验时「写入分镜」禁用；控制台 0 错误。字段差异与锁定跳过逻辑由 `tests/test_storyboard_plan.py` 覆盖（含 `field_changes.will_skip` 断言），未在浏览器逐一构造。
- [ ] 资产变更影响列表与工作流版本影响预检可读、可操作（2026-11-22 实测：`/assets/impact` 渲染「资产变更影响」页，`impact-preview` 与 `workflow-impact` 均 200，无影响时显示合并空态「没有待处理的资产变更影响」，控制台 0 错误；有影响时的列表与操作未构造，故未勾选）。
- [ ] 失败任务可重试、可取消，刷新后状态不丢。

## 5. 已知缺口

1. 规划侧「对白时长冲突」「一个 Beat 多镜 / 多 Beat 一镜」缺专项用例。
2. 续集复用既有角色/道具缺端到端用例。
3. 数据迁移无自动化测试（本重构未新增迁移）。
4. 真实 ComfyUI/GPU 全链路（§4）尚未执行，Phase 12 不能标记为完成。
5. 视频 / TTS 真实链路被远端环境阻塞（2026-11-22 核查，同日复验 `GET /api/workflows` 与 `POST /api/workflows/{id}/check-runtime`）：仅 `wf_image_ref_qwen21_multi` / `wf_image_t2i_qwen21_turbo` 为 `ready`，5 条 video、2 条 tts 全部 `missing_deps`。精确缺口：
   - 视频（`wf_video_h3_ref2va_std_5s` 等）：`vae/minimax_h3_video_vae_int8_convrot.safetensors`（服务器 VAE 目录只有 `minimax_h3_audio_vae_fp32` 与 `minimax_h3_video_vae_fp16`）、`loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`（LoRA 目录无任何 H3 文件），合计 4.44 GiB。
   - TTS（`wf_tts_qwen3_custom_voice` / `wf_tts_qwen3_voice_clone`）：缺自定义节点 `Qwen3Loader` / `Qwen3CustomVoice`（`DarioFT/ComfyUI-Qwen3-TTS`），模型按 HF 仓库在线加载、无需本地 safetensors。
   - BGM：`wf_bgm_stable_audio` 已移入 `data/workflows/_retired`，当前无在用 BGM 工作流，其 `stable-audio-open-1.0` / `t5-base` 模型不计入本轮验收范围；后续启用时再补。
   - 补齐方式（在 ComfyUI 所在机器执行，仓库自带脚本已覆盖上述资产）：`python autodl_comfyui_qwen21_h3_setup.py download --groups h3speed`（或 `all --groups tts --no-download --no-launch` 装 TTS 节点，再重启 ComfyUI）。补齐后重跑 §4.2 / §4.3。
6. 自动生成路径不写 `asset_ref` 表（只有手动绑定接口 `POST /api/shots/{id}/assets` 写入），自动参考关系记录在 `job.input_snapshot.reference_manifest`。实测分镜图任务 manifest 含 `ref_scene`（教室）+ `ref_identity`（林晓）两槽位，属预期行为；若前端「引用资产」列表依赖 `asset_ref` 会显示为空，需按 manifest 兜底（未实施）。
7. ~~`/api/health` 的 `worker` 字段为硬编码 `"ok"`~~（已修复，Slice 27）：worker 主循环写心跳文件，health 读心跳返回 `ok` / `stale` / `not_running` / `unknown` 并附带 `worker_last_beat`；worker 停止超过 120s 时 `status` 降为 `degraded`。已实测停 worker → `stale`/`degraded`，重启 → `ok`/`healthy`。
