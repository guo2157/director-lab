# 统一 Task Orchestrator 交付报告（对话式任务执行改造 Phase 2）

- 日期：2026-11-04
- 前置：Phase 1 审计报告 `phase1-audit-capability-matrix-20261104.md`
- 原则：不推倒重写；Job 保持底层执行单元不变；LLM 不得编造状态；事件流是唯一事实来源

## 一、数据模型（迁移 e8b2d5f7a4c1，已执行）

| 表 | 说明 |
|----|------|
| task | 统一任务：session_id/agent_task_id/project_id/task_type/title/status（pending→running→completed/failed/cancelled）/progress（0-100 聚合）/error_code/error_msg |
| task_stage | 任务阶段：stage_index 递增/stage_key/stage_name/status（含 failed→running 重试回退）/progress/job_id（关联底层 Job）/result |
| task_event | 事件 outbox：event_seq 全局自增主键，断线重连以 seq 严格大于判定（复用 job_event 语义） |
| task_result | 结构化结果卡片：result_type（summary/assets/shots/custom）+ data JSON |

- 模型：`app/models/task.py`；ID 生成器：tsk_/tst_/tsr_（`app/models/ids.py`）
- 索引：session+status、project+type、stage task+index、event task_id 等

## 二、Task Orchestrator 服务（app/services/task_orchestrator.py）

- 生命周期：create_task（幂等：同一 agent_task_id 复用已有 Task）/start_task/complete_task（写 TaskResult 卡片）/fail_task/cancel_task，全部状态机门禁（终态不可变更）
- 阶段管理：add_stage/start_stage/update_stage_progress/finish_stage；任务进度 = 各阶段进度均值；任一阶段 running → 任务 running
- 事件写入：每次状态变更同步写 task_event（task.created/started/finished/failed/cancelled/stage_added/stage_started/stage_progress/stage_succeeded/stage_failed）
- Job 事件桥接 bridge_job_event：按 task_stage.job_id 定位归属任务；job.started→stage_started、job.finished（payload.status）→stage_succeeded/failed、job.retry/quality_retry→stage_progress 透传；Job 不属于任何任务时返回 0 不报错
- task_payload：任务卡片数据（task_id/status/progress/stages[]/result），供 chat payload 与前端渲染

## 三、执行器桥接挂载（4 个执行器 _write_event 内，异常隔离）

- `app/worker/executor.py`、`parse_executor.py`、`compose_executor.py`、`export_executor.py`：写 job_event 后调用 bridge_job_event，try/except 包裹，桥接失败不影响既有执行链路

## 四、chat 端点集成（app/api/v1/agent.py，6 处）

1. AgentTask 创建后同步创建统一 Task（幂等）
2. GUIDE 审批成功 → complete_task；审批失败 → fail_task（approval_submit_failed）
3. PIPELINE 分支 → complete_task
4. 澄清/拒绝分支 → cancel_task
5. LLM 失败 → fail_task（llm_error）；LLM 成功主对话闭环 → complete_task
6. 两处 assistant payload 新增 task_card（task_payload 全量卡片数据）

## 五、事件流 API（app/api/v1/events.py）

- `GET /api/tasks/events`：task_event SSE 增量推送（1s 轮询、300s 连接上限、last_seq 重连），与 /api/events 同语义
- `GET /api/tasks/events/backfill`：断线补齐（seq 严格大于，升序，上限 1000）

## 六、验证结果

- 单元测试 `tests/test_task_orchestrator.py`：10 passed（生命周期/幂等/阶段进度聚合/状态机门禁/Job 桥接 succeeded/failed/无归属/重试透传/卡片结构）
- 后端全量回归：478 passed / 7 failed；7 个失败均为 /tmp/p0.png root 所有权环境问题（与本次改动无关，与 Phase 1 基线一致）
- 迁移 e8b2d5f7a4c1 执行成功；模型导入验证通过
- 后端已重启：PID 1591969，健康检查 HTTP 200
- 新端点验证：/api/tasks/events/backfill 返回 {"total":0,"items":[]}（空表正常）；/api/tasks/events SSE HTTP 200

## 七、遗留与下一步

- 前端任务卡片组件（DirectorConsoleView 消息类型分支 + TaskCard 动态更新 + 聊天内 SSE 订阅）：Phase 2-6 各阶段同步推进，当前后端 payload.task_card 与 /api/tasks/events 已就绪
- Phase 3：剧本解析阶段化进度（parse_executor 按 EpisodeParseRecord 粒度补发 task 级进度事件）
- Phase 4：生成类任务聊天闭环（生成 Job 建立时同步建 TaskStage 关联，桥接已就绪）
