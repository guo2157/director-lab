# 人工审核与依赖门禁交付报告（对话式任务执行改造 Phase 5）

- 日期：2026-11-04
- 前置：Phase 4 生成聊天闭环（`generate-task-phase4-delivery-20261104.md`）
- 原则：复用既有审批流（request_generate → run_generate → execute_approval），不新建审批通道；LLM 不得编造状态

## 一、依赖门禁（迁移 f9b3e2a6c5d8，已执行）

- task_stage 新增 depends_on 字段（JSON：前置阶段 stage_key 列表）
- add_stage 支持 depends_on 参数声明前置依赖
- start_stage 依赖门禁校验：前置阶段未全部 succeeded 时拒绝启动，写 task.stage_blocked 事件（含 blocked_by/dep_status）
  - 前置 pending/running/failed：拦截（等待人工审核/前置完成，不猜测执行）
  - 前置阶段不存在（missing）：拦截
  - 前置全部 succeeded：放行
- 典型场景：clip 阶段 depends_on=["keyframe"]（分镜图人工确认前不启动视频生成）

## 二、审批事件桥接（app/services/approvals.py）

- resolve_confirmation 裁决（approve/reject）时同步写 task.approval_resolved 事件（含 confirmation_id/approve/comment）
- 桥接按 agent_task_id 关联统一 Task（Task.agent_task_id = confirmation.task_id）
- 无关联统一 Task 时安全返回 0，不影响既有审批链路
- 桥接 try/except 异常隔离

## 三、审核状态卡片

- 审批等待中：任务卡片经 task_payload 展示 pending 状态（Phase 2 已就绪）
- 审批裁决：task.approval_resolved 事件驱动前端任务卡片动态更新（approve → 后续 run_generate 建 Task；reject → 任务卡片更新为已拒绝）
- 审批通过触发的 run_generate 已在 Phase 4 接入统一 Task 层（含全部既有门禁）

## 四、验证结果

- 单元测试 `tests/test_task_dependency_gate.py`：7 passed
  - 依赖门禁：前置未完成拦截 + stage_blocked 事件 / 前置 failed 拦截 / 依赖缺失拦截（dep_status=missing）/ 无依赖正常启动
  - 审批桥接：approve 写事件 / reject 写事件 / 无关联 Task 安全返回 0
- 后端全量回归：493 passed / 7 failed；7 个失败均为 /tmp/p0.png root 所有权环境问题（与 Phase 1-4 基线一致，非本次改动引入）
- 迁移 f9b3e2a6c5d8 执行成功
- 后端已重启：PID 1614931，健康检查 HTTP 200

## 五、遗留与下一步

- Phase 6：分镜图/视频/合成/导出链路接入（compose/export 执行器桥接已挂载，需在 tool_compose_video/tool_export_video 建 Task）
- Phase 7：恢复/重试/性能安全（复用 scheduler 三类恢复 + request_key 幂等）
- 前端任务卡片组件（消费 payload.task_card 与 /api/tasks/events SSE，含 stage_blocked/approval_resolved 事件渲染）
