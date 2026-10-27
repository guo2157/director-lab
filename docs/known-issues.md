# 剩余问题、限制和后续建议

- 记录日期：2026-10-26

## 一、进行中事项（非故障）

1. **分镜视频生成（阶段 F）后台进行中**
   - 8 镜头任务已提交（ref2va 工作流），镜头 1 运行中，其余 7 个排队
   - H3 单镜约 20-25 分钟（A10），串行预计约 3 小时完成
   - 监控方式：`tail -f /tmp/clip_monitor2.log`（每 60 秒记录任务状态分布）
   - 完成后可执行：`POST /api/projects/prj_c4d1175b2c70/compose`（合成）→ `POST /api/projects/prj_c4d1175b2c70/export`（导出，需 composition_id）

2. **镜头 1 TTS 音频任务排队中**（job_61462f84eb7b），worker 串行调度，排在 clip 任务之后

3. **合成任务排队中**（job_2947fc9e68ed），依赖 clip 完成

## 二、本次修复引入的变更（需知悉）

1. **executor.py 新增 clip 自动参数注入**（已备份 executor.py.bak_20261026）
   - ref_image（当前镜头分镜图）/ ref_prev_frame（上镜末帧）/ H3 width/height / length（帧数换算）
   - 回归测试 59 passed；建议后续补充针对该逻辑的单元测试
2. **ref2va 设为 video 类型默认工作流**（原三个 video 工作流均无默认标记，兜底匹配不可控）
3. **comfyui_job_timeout_seconds 600→1800**（H3 真实生成时长约 20-25 分钟）
4. **alembic 迁移 1283779e3172 已应用**（asset.quality_status 列）
5. **frontend/dist 属主修正为 ubuntu**

## 三、遗留问题与建议

1. **工作流默认标记缺失**：建议为各类型工作流显式设置 is_default（image/tts 类型同样存在兜底匹配不可控问题）
2. **retry 产生新任务 ID**：多次重试会产生重复任务（本次已手动取消 2 个）；建议前端重试时提示已有排队中的重试任务
3. **H3 硬件基准未校准**：工作流 meta 标注 pending_benchmark；本次实测 A10 单镜约 20-25 分钟，建议回填 meta
4. **worker 日志信息量少**：dlab_worker.log 仅记录调度行，建议补充任务级错误详情（当前需查 API/数据库）
5. **runtime_settings.json 属主 root**：与运行进程一致无功能问题，但建议统一为服务专用用户管理
6. **历史失败任务残留**：数据库中存在早期失败记录（LLM 401/502、参数缺失等），均为修复前产生，不影响当前流程

## 四、从剧本输入重新运行全流程的方法

1. 创建项目：`POST /api/projects`（creation_type=script，aspect_ratio）
2. 输入剧本并评估：`POST /api/projects/{id}/script`（estimate）
3. 选择范围并解析：parse（LLM 生成角色/场景/道具/镜头）
4. 资产图：逐个 `POST /api/characters/{id}/generate`、`/api/scenes/{id}/generate-baseline`、`/api/props/{id}/generate`
5. 分镜图：`POST /api/projects/{id}/generate`（target=keyframe）→ 确认 `POST /api/projects/{id}/keyframe/confirm`
6. 分镜视频：`POST /api/projects/{id}/generate`（target=clip，默认 ref2va 工作流）
7. 音频：`POST /api/shots/{id}/generate`（target=audio）
8. 合成：`POST /api/projects/{id}/compose` → 导出：`POST /api/projects/{id}/export`（composition_id）

## 五、需要人工介入的事项

- 等待 8 镜视频生成完成后，人工审核视频质量（人物一致性/镜头衔接/音画同步）
- 合成与导出完成后，人工确认成片并下载（GET /api/exports/{id}/download）

---

## 2026-10-26 修复记录（代码层修复与测试）

1. 工作流默认标记补齐：image 类型默认 wf_image_ref_qwen_edit（wf_8f52e2b0512c），tts 类型默认 wf_tts_qwen3_custom_voice（wf_ccf45b3f0c1a），video 默认保持 wf_video_h3_ref2va。
2. 在途任务去重（幂等性）：
   - 单镜生成/重做：同镜头同目标已有 queued/running 任务时返回 409 job_in_flight（generate.py _check_inflight_duplicate）。
   - 批量生成（场景/项目）：跳过在途镜头，响应新增 skipped 字段报告原因（generate.py _filter_inflight）。
   - 任务重试/批量重试：同镜头同目标在途时拒绝或跳过（jobs.py）。
3. worker 任务级日志：executor.py 在任务成功/失败时输出任务 ID、类型、镜头、错误码与消息（dlab_worker.log 可诊断性）。
4. clip 自动参数注入可追溯：注入的 width/height/length 与 ref_image/ref_prev_frame 持久化到 job.input_snapshot（injected_params/injected_media）。
5. 新增单元测试 tests/test_clip_inject_and_dedup.py（5 个用例：注入参数、length 收敛、单镜去重、重试去重、批量跳过）；全量回归 94 passed。
6. 服务已重启加载新代码（uvicorn :8000 + worker.scheduler），API 健康检查 200。

### 未解决（外部依赖阻塞）
- ComfyUI 实例（cloudstudio）已失效（HTTP 403），8 个 clip 任务、TTS、合成任务失败（comfy_timeout/comfy_unreachable/output_missing）。实例恢复后可通过 POST /api/jobs/{id}/retry 或 /api/jobs/projects/{id}/retry-failed 重跑，去重逻辑已保证不会产生重复任务。
- H3 硬件基准未校准（A10 单镜 20-25 分钟，建议回填 meta）。
- runtime_settings.json 属主 root，建议统一服务用户。
