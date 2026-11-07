# 剧本解析阶段化进度与结构化结果卡片交付报告（对话式任务执行改造 Phase 3）

- 日期：2026-11-04
- 前置：Phase 2 统一 Task Orchestrator（`task-orchestrator-delivery-20261104.md`）
- 原则：不推倒重写；LLM 不得编造状态；进度事件只进 task_event outbox，不刷屏聊天记录

## 一、解析任务接入统一 Task 层（app/services/pipeline_tools.py）

- tool_analyze_script 创建解析 Job 后同步创建统一 Task（task_type=parse，title=剧本解析）+ parse 阶段（TaskStage 关联 job_id）
- 任务立即 start（running）；Job 事件经 bridge_job_event 自动桥接为 task 事件
- 编排层接入 try/except 异常隔离：编排失败不影响既有解析链路

## 二、阶段化进度事件（app/worker/parse_executor.py）

- 新增 _task_progress(db, job_id, progress) 辅助函数（异常隔离），在解析主流程 4 个里程碑推进阶段进度：
  - 集级记录置 running 后：10%
  - LLM 调用成功后：50%
  - 归一化完成、结果组装前：70%
  - 集级记录 succeeded 后：90%
- 每次进度更新写 task.stage_progress 事件（含 stage_id/stage_key/progress/task_progress），前端任务卡片据此动态更新
- 说明：当前解析为单次 LLM 调用产出全部集，进度按里程碑推进；EpisodeParseRecord 集×资产类型粒度记录保持原有 upsert 语义

## 三、任务自动闭环与结果卡片（app/services/task_orchestrator.py）

- bridge_job_event 每次桥接后调用 _auto_close_task_if_done：所有阶段到达终态时自动闭环任务
  - 全 succeeded → complete_task + 写 TaskResult 结果卡片（result_type=shots，含 parse_version_id/shots 等，job.finished payload 的嵌套 result 字段自动解包）
  - 任一 failed → fail_task（error_code/error_msg 取自失败阶段结果）
- 新增 update_stage_progress_by_job(db, job_id, progress)：按 job_id 更新关联阶段进度（执行器里程碑用）

## 四、验证结果

- 单元测试 `tests/test_parse_task_integration.py`：4 passed（tool_analyze_script 建 Task+阶段/桥接自动闭环+结果卡片/失败自动闭环/按 job_id 更新进度）
- Task Orchestrator 回归 `tests/test_task_orchestrator.py`：10 passed（合计 14 passed）
- 后端全量回归：482 passed / 7 failed；7 个失败均为 /tmp/p0.png root 所有权环境问题（与 Phase 1/2 基线一致，非本次改动引入）
- 后端已重启：PID 1597852，健康检查 HTTP 200

## 五、遗留与下一步

- Phase 4：角色/场景/道具生成聊天闭环（生成 Job 建立时同步建 TaskStage 关联，桥接机制已就绪）
- Phase 5：人工审核与依赖门禁（复用 request_generate/run_generate/execute_approval 既有审批流）
- 前端任务卡片组件（消费 payload.task_card 与 /api/tasks/events SSE）
