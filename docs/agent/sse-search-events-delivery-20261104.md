# SSE 搜索事件流交付报告

- 日期：2026-11-04
- 范围：联网搜索执行过程事件化，经统一任务事件流（/api/tasks/events SSE）实时推送，前端搜索卡片实时更新
- 背景：二期遗留事项之一；复用 Phase 2 统一 Task 层与前端任务卡片 SSE 消费（useTaskEvents），不新增事件通道

## 1. 后端：搜索生命周期接入统一 Task 层（search_service.py）

run_web_search 内创建 task_type=web_search 的统一 Task（session_id/project_id 透传）：

- 状态机各阶段写进度事件（task.progress）：searching 20% / validating 45% / fetching 60% / summarizing 85%
- 终态闭环：success/partial → complete_task（含结果卡片，result_data 为搜索执行记录）；failed/empty → fail_task（error_code/error_msg 透传）
- 编排层全程 try/except 异常隔离，失败不影响既有搜索链路
- _finish 返回的 search payload 补充 task_id 字段，供前端搜索卡片关联 SSE 快照

## 2. 前端：搜索卡片实时更新（DirectorConsoleView.vue）

- searchSnapshotOf(m)：通过 payload.search.task_id 关联 taskSnapshots（useTaskEvents 维护的 SSE 快照）
- 搜索进行中（快照 status=running）：卡片内实时展示阶段文案（搜索中/验证结果/读取正文/汇总结果）+ 进度条 + 百分比（单卡片动态更新，不刷屏）
- 终态后隐藏实时区，回落到最终执行记录展示（关键词/引擎/有效结果/耗时）

## 3. 验证

- 后端搜索相关测试：8 passed 无回归
- 前端构建：vue-tsc + vite build 通过（7.22s）
- 端到端验证（真实联网搜索）：
  - 搜索执行成功（status=partial，8/8 有效结果），payload.search.task_id=tsk_d23e01bc4bdd
  - 事件流完整：task.created → task.started → task.progress(searching/validating/fetching/summarizing) → task.finished，共 7 条事件，seq 8-14 连续递增
- 后端重启：PID 1648253，/api/health 200

## 4. 变更文件清单

| 文件 | 变更 |
| --- | --- |
| backend/app/services/search_service.py | 搜索接入统一 Task 层（+57 行：Task 创建/阶段进度事件/终态闭环/payload 补 task_id） |
| frontend/src/views/DirectorConsoleView.vue | 搜索卡片实时更新（+57 行：searchSnapshotOf/实时进度条模板/样式） |

## 5. 设计约束落实

- 复用统一 Task 层与既有 SSE 通道（/api/tasks/events），不新增事件基础设施
- 进度事件不刷屏：单卡片内动态更新
- 编排层异常隔离：搜索链路不受影响

## 6. 遗留事项

- 搜索取消通道（用户中断进行中的搜索）
- OpenTelemetry 链路追踪
