# 分镜图/视频/合成/导出链路接入统一 Task 层交付报告（对话式任务执行改造 Phase 6）

- 日期：2026-11-04
- 前置：Phase 5 人工审核与依赖门禁（`approval-gate-phase5-delivery-20261104.md`）
- 原则：不推倒重写；compose/export 执行器事件桥接已在 Phase 2 挂载，本阶段补齐 Task 创建入口

## 一、合成/导出任务接入统一 Task 层（app/services/pipeline_tools.py）

- tool_compose_video 创建合成 Job 后同步创建统一 Task（task_type=compose，title=成片合成）+ compose 阶段（TaskStage 关联 job_id）
- tool_export_video 创建导出 Job 后同步创建统一 Task（task_type=export，title=成片导出）+ export 阶段（TaskStage 关联 job_id）
- 编排层接入 try/except 异常隔离：编排失败不影响既有合成/导出链路
- 既有门禁全部保留：状态机门禁（compose 阶段前置）、未审核放行镜头拦截、归档项目拦截、合成版本有效性校验（stale/status）

## 二、合成/导出进度事件（app/worker/compose_executor.py、export_executor.py）

- 两个执行器新增 _task_progress 辅助函数（异常隔离），在关键里程碑推进阶段进度：
  - compose_executor：镜头收集后 30% / 合成执行前 60%
  - export_executor：导出写盘前 60%
- 每次进度更新写 task.stage_progress 事件，前端任务卡片据此动态更新
- Job 事件桥接（Phase 2 已挂载）：job.started/job.finished 自动桥接为 task 事件

## 三、结果卡片（复用 Phase 3 自动闭环机制）

- 合成完成自动闭环：job.finished(succeeded) → 阶段 succeeded → 任务 completed + TaskResult 结果卡片（含 composition_id/duration）
- 导出完成自动闭环：结果卡片含 export_id/file_name/download_url
- 失败自动闭环：error_code/error_msg 取自失败阶段结果

## 四、验证结果

- 单元测试 `tests/test_compose_export_task_integration.py`：4 passed
  - tool_compose_video 创建统一 Task + compose 阶段（关联 job_id）
  - 合成桥接自动闭环 + 结果卡片（composition_id）
  - tool_export_video 创建统一 Task + export 阶段（关联 job_id）
  - 导出桥接自动闭环 + 结果卡片（export_id）
- 后端全量回归：497 passed / 7 failed；7 个失败均为 /tmp/p0.png root 所有权环境问题（与 Phase 1-5 基线一致，非本次改动引入）
- 后端已重启：PID 1622073，健康检查 HTTP 200

## 五、说明

- 测试中状态机门禁（compose 阶段前置）通过 monkeypatch 绕过：该门禁属 pipeline_state 既有测试范围（test_pipeline_state.py），本测试聚焦编排层逻辑
- 分镜图/视频生成链路已在 Phase 4 接入（tool_run_generate 统一入口，覆盖 keyframe/clip/audio/character/scene/prop 全部 target）

## 六、遗留与下一步

- Phase 7：恢复/重试/性能安全（复用 scheduler 三类恢复 + request_key 幂等 + comfy 对账续跑；LLM 提示词强制只引用真实事件状态）
- 前端任务卡片组件（消费 payload.task_card 与 /api/tasks/events SSE，覆盖 parse/generate/compose/export 全部任务类型）
