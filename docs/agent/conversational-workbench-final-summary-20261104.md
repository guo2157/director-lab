# Director-Lab「对话式 AI 生产工作台」十阶段改造收官总结

- 日期：2026-11-04
- 目标：将导演台改造为「对话式 AI 生产工作台」，实现十阶段统一对话式任务执行
- 约束：不推倒重写、复用现有服务/ComfyUI/Job 队列、LLM 不得编造状态、进度事件不刷屏（任务卡片单卡片动态更新）、前端不得直调 ComfyUI

## 一、阶段总览

| 阶段 | 内容 | 状态 | 交付物 |
| --- | --- | --- | --- |
| Phase 1 | 能力矩阵审计 | ✅ | docs/agent/phase1-audit-capability-matrix-20261104.md |
| Phase 2 | 统一 Task 层 | ✅ | task_orchestrator.py + task/task_stage/task_event/task_result 表 + alembic 迁移 |
| Phase 3 | 剧本解析阶段化进度 + 结构化结果卡片 | ✅ | parse_executor 阶段化进度事件，自动闭环 |
| Phase 4 | 生成链路接入统一 Task 层 | ✅ | tool_run_generate 统一入口（keyframe/clip/audio/character/scene/prop） |
| Phase 5 | 人工审核与依赖门禁 | ✅ | docs/agent/approval-gate-phase5-delivery-20261104.md |
| Phase 6 | 合成/导出接入统一 Task 层 | ✅ | docs/agent/compose-export-phase6-delivery-20261104.md |
| Phase 7 | 恢复/重试/性能安全（收官） | ✅ | docs/agent/phase7-recovery-reconcile-delivery-20261104.md |

## 二、核心架构成果

### 2.1 统一 Task 层（Phase 2）

- 新建 `app/services/task_orchestrator.py`：Task/TaskStage/TaskResult/TaskEvent 模型与编排原语（create_task/start_task/add_stage/finish_stage/fail_task/complete_task/write_event）
- Task 映射 Job，job_event outbox 桥接为 task 事件，复用现有 Job 队列与幂等（request_key）机制
- alembic 迁移：a7d9e3f5c2b4、d4e6f8a1c3b9

### 2.2 全链路任务化（Phase 3-6）

- 解析：分集进度事件 + 结构化结果卡片（自动闭环）
- 生成：tool_run_generate 统一入口覆盖全部 target 类型
- 审核：人工审批门禁 + 依赖门禁（execute_approval）
- 合成/导出：tool_compose_video/tool_export_video 建 Task+阶段（TaskStage 关联 job_id），compose_executor（30%/60%）与 export_executor（60%）_task_progress 进度事件，结果卡片含 composition_id/export_id/download_url

### 2.3 恢复与安全（Phase 7）

- 孤儿 Task 恢复对账：reconcile_orphan_tasks 按 Job 终态闭环，幂等，Worker 启动挂载（异常隔离）
- LLM 任务状态纪律：系统提示词约束只能引用真实状态、无数据如实说明、失败只基于真实错误码
- 与既有三类恢复机制协同：_recover_interrupted_jobs（running→queued）、_reconcile_comfy_timeout_jobs（远端已完成重置 queued+resume_prompt 续跑）、_recover_stalled_llm_jobs（LLM 卡死回收）

## 三、质量验证

- 新增测试：tests/test_task_recovery_reconcile.py（6 passed）、tests/test_compose_export_task_integration.py（4 passed）
- 全量回归：503 passed / 7 failed（7 个失败均为 /tmp/p0.png root 所有权环境问题，与基线一致，非改动引入）
- 后端重启验证：PID 1634655，GET /api/health → 200

## 四、关键文件索引

- 编排：app/services/task_orchestrator.py
- 调度：app/worker/scheduler.py
- 执行器：app/worker/executor.py、compose_executor.py、export_executor.py、parse_executor.py
- 对话入口：app/api/v1/agent.py（chat:843、execute_approval:296、_DIRECTOR_SYSTEM_PROMPT:32）
- 工具层：app/services/pipeline_tools.py
- 模型：app/models/task.py（Task/TaskStage/TaskResult）、app/models/production.py（Job:74、job_event:182）
- 事件流：app/api/v1/events.py（SSE，seq 递增断线重连）

## 五、遗留事项

1. 前端任务卡片组件：消费 payload.task_card 与 /api/tasks/events SSE，覆盖 parse/generate/compose/export 全部任务类型（各阶段遗留的前端工作，建议作为下一迭代首要事项）
2. 二期：SSE 搜索事件流、搜索取消通道、OpenTelemetry
3. 「剧本生成失败」疑似旧会话状态残留，待新会话验证

## 六、结论

十阶段改造后端部分全部完成：统一 Task 层贯通解析/生成/审核/合成/导出全链路，恢复对账与 LLM 状态纪律补齐可靠性短板，全量回归无新增失败，服务健康运行。系统已具备「对话式 AI 生产工作台」的后端完整能力，前端任务卡片为唯一主要遗留项。
