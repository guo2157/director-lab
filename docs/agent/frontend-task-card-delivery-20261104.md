# 前端任务卡片组件交付报告

- 日期：2026-11-04
- 范围：前端任务卡片组件（消费 assistant payload.task_card 与 /api/tasks/events SSE，覆盖 parse/generate/compose/export 全部任务类型）
- 背景：Phase 1-7 后端统一 Task 层已收官，本项为各阶段遗留的前端工作，至此十阶段改造前后端全部闭环

## 1. 交付内容

### 1.1 API 模块（src/api/tasks.ts）

- `TaskCardData` / `TaskStageInfo` / `TaskEvent` 类型定义，与后端 `task_orchestrator.task_payload` 及 `/api/tasks/events` 事件结构一一对应
- `backfillTaskEvents(updatedSinceSeq)`：断线补齐（`GET /api/tasks/events/backfill`，seq 严格大于判定）

### 1.2 SSE 消费（src/composables/useTaskEvents.ts）

- 订阅 `/api/tasks/events?last_seq=N`（EventSource，与后端 seq 严格大于语义一致）
- 断线重连：EventSource 自动重连，onopen 时通过 backfill 按 lastSeq 补齐丢失事件
- `taskSnapshots`：task_id → 卡片快照（reactive），事件合并（后发覆盖旧状态，seq 去重）
- `registerCard(card)`：用 assistant payload.task_card 注册初始快照（已有更新数据不回退）
- onUnmounted 自动断开

### 1.3 任务卡片组件（src/components/TaskCard.vue）

- 单卡片动态更新（不刷屏）：初始卡片 + SSE 快照合并视图，进度条/阶段状态原地更新
- 展示：标题、任务类型（解析/生成/合成/导出）、状态标签（排队中/进行中/已完成/失败/已取消）、总进度条、阶段列表（逐阶段 ✓/✗/百分比）、错误码与错误信息、结果摘要（前 6 个字段）、download_url 下载入口
- 终态停止进度条刷新

### 1.4 接入对话视图（src/views/DirectorConsoleView.vue）

- 导入 TaskCard 组件与 useTaskEvents composable
- `taskCardOf(m)`：提取 assistant 消息 payload.task_card
- watch messages（deep）：消息加载/新消息到达时注册卡片初始快照
- onMounted 建立 SSE 连接
- 模板：消息气泡内（search 卡片之后）渲染 `<TaskCard :card :snapshot>`，仅 assistant 且含 task_card 时展示

## 2. 验证

- 前端构建：`vue-tsc -b && vite build` 类型检查通过，2850 模块转换，构建成功（7.22s）
  - 注：默认 dist 目录因历史 root 所有权文件 unlink 失败（EACCES，与 /tmp/p0.png 同类环境问题），已用独立输出目录 dist_verify 验证构建完整通过后清理
- 联调冒烟：
  - `GET /api/tasks/events/backfill?updated_since_seq=0` → 200
  - `GET /api/tasks/events?last_seq=0` SSE 连接正常建立，ping 心跳正常（当前无活跃任务，无事件推送属预期）

## 3. 变更文件清单

| 文件 | 变更 |
| --- | --- |
| frontend/src/api/tasks.ts | 新建：任务类型定义 + backfill API |
| frontend/src/composables/useTaskEvents.ts | 新建：SSE 消费 + 断线补齐 + 快照合并 |
| frontend/src/components/TaskCard.vue | 新建：任务卡片组件（单卡片动态更新） |
| frontend/src/views/DirectorConsoleView.vue | 接入：导入/注册快照/SSE 连接/模板挂载（+32 行） |

## 4. 设计约束落实

- 进度事件不刷屏：单卡片内动态更新（快照合并，非追加消息）
- 前端不直调 ComfyUI：仅消费后端统一 Task 层的 task_card 与 SSE
- 断线重连：seq 递增 + backfill 补齐，与 /api/events 语义一致

## 5. 端到端验证（2026-11-04 补充）

### 5.1 dist root 所有权文件清理

- dist 内 40 个 root 所有权文件已用 sudo -n 清理，默认 `npm run build` 恢复正常（7.28s 构建成功）

### 5.2 剧本解析链路端到端验证（新会话）

- 新建会话 ags_e7f15735bdf3 关联项目 prj_956e2ddc066d（小红帽）
- 对话触发 analyze_script 工具：Job job_ce3db350a410 入队并完成，task_card（tsk_8cdd48ee4cb8）status=completed、progress=100
- 结论：解析链路端到端正常，「剧本生成失败」未复现（此前失败为旧会话未关联项目导致 clarify 取消，属预期行为而非故障）

### 5.3 backfill 端点 NameError 修复

- 端到端验证中发现 `GET /api/tasks/events/backfill` 返回 500：events.py:162 `db.get(Task, ...)` 中 Task 未导入（函数内仅导入了 TaskEvent）
- 修复：backfill 函数内导入改为 `from app.models.task import Task, TaskEvent`
- 验证：后端重启（PID 1644443，/api/health 200）后 backfill 返回 200，7 条事件数据完整；events/task 相关测试 38 passed 无回归

## 6. 遗留事项

- 二期：SSE 搜索事件流、搜索取消通道、OpenTelemetry

## 7. 结论

前端任务卡片组件交付完成：对话流内任务卡片覆盖 parse/generate/compose/export 全部任务类型，SSE 实时更新 + 断线补齐，构建与端到端验证通过（含 backfill 端点缺陷修复）。至此十阶段「对话式 AI 生产工作台」改造前后端全部闭环。
