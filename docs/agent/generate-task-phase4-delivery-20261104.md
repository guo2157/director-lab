# 角色/场景/道具生成聊天闭环交付报告（对话式任务执行改造 Phase 4）

- 日期：2026-11-04
- 前置：Phase 3 解析阶段化（`parse-task-phase3-delivery-20261104.md`）
- 原则：不推倒重写；复用既有审批流（request_generate → run_generate → execute_approval）；LLM 不得编造状态

## 一、生成任务接入统一 Task 层（app/services/agent_tool_impls.py）

- tool_run_generate 创建生成 Job 后同步创建统一 Task（task_type=generate）+ generate 阶段（TaskStage 关联 job_id）
- 阶段命名按 target 映射：keyframe=分镜图生成 / clip=分镜视频生成 / audio=音频生成 / character=角色形象生成 / scene=场景基准图生成 / prop=道具锚定图生成
- 会话归属：审批记录关联的 AgentTask（confirmation.task_id → AgentTask.session_id）携带会话 ID
- 幂等保护：同 request_key 复用 Job 时不重复建 Task（复用分支在 Task 创建之前返回）
- 编排层接入 try/except 异常隔离：编排失败不影响既有生成链路
- 既有门禁全部保留：审批状态门禁、状态机门禁（_assert_stage_gate）、静图先行闸口（D-13）、资产就绪门禁（D-23）、keyframe 确认闸口

## 二、生成进度事件（app/worker/executor.py）

- 新增 _task_progress(db, job_id, progress) 辅助函数（异常隔离），在生成主流程 2 个里程碑推进阶段进度：
  - ComfyUI 提交后进入等待：30%
  - 资产落库前：90%
- 每次进度更新写 task.stage_progress 事件，前端任务卡片据此动态更新
- Job 事件桥接（Phase 2 已挂载）：job.started/job.retry/job.quality_retry/job.finished 自动桥接为 task 事件

## 三、结果卡片（复用 Phase 3 自动闭环机制）

- 生成完成自动闭环：job.finished(succeeded) → 阶段 succeeded → 任务 completed + TaskResult 结果卡片（含 assets 资产列表/prompt_id/seed）
- 生成失败自动闭环：job.finished(failed) → 阶段 failed → 任务 failed（error_code/error_msg 取自失败阶段结果）

## 四、验证结果

- 单元测试 `tests/test_generate_task_integration.py`：4 passed
  - tool_run_generate 创建统一 Task + generate 阶段（关联 job_id，阶段命名正确）
  - 幂等复用 Job 时不重复建 Task
  - 桥接自动闭环 + 结果卡片（assets 资产列表）
  - 桥接失败自动闭环（error_code 透传）
- 后端全量回归：486 passed / 7 failed；7 个失败均为 /tmp/p0.png root 所有权环境问题（与 Phase 1/2/3 基线一致，非本次改动引入）
- 后端已重启：PID 1604837，健康检查 HTTP 200

## 五、过程修复记录

- agent_tool_impls.py 为 root 所有权导致写入被拒：sudo chown 修正后继续（该文件所有权问题与 /tmp/p0.png 同源，均为历史 root 操作残留）
- AgentConfirmation 模型无 session_id 字段：会话归属改从关联 AgentTask 取（confirmation.task_id → AgentTask.session_id）
- 测试夹具补齐 D-23 资产门禁前置（场景基准图资产 + 项目审核状态 approved + baseline 加入 session）

## 六、遗留与下一步

- Phase 5：人工审核与依赖门禁（Task Orchestrator 增加依赖门禁判断，复用既有审批流）
- Phase 6：分镜图/视频/合成/导出链路接入（compose/export 执行器桥接已挂载，需在 tool_compose_video/tool_export_video 建 Task）
- 前端任务卡片组件（消费 payload.task_card 与 /api/tasks/events SSE）
