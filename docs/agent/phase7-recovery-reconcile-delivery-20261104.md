# Phase 7 交付报告：恢复 / 重试 / 性能安全（收官阶段）

- 日期：2026-11-04
- 范围：孤儿 Task 恢复对账、Worker 启动挂载、LLM 任务状态纪律、测试与回归、后端重启验证
- 前置：Phase 1-6 已交付（能力矩阵审计、统一 Task 层、解析阶段化、生成链路接入、审批门禁、合成/导出接入）

## 1. 交付内容

### 1.1 孤儿 Task 恢复对账（task_orchestrator.py）

新增 `reconcile_orphan_tasks(db)`（约 +55 行）：

- 规则：
  - pending/running 的 Task，其阶段关联的 Job 已全部到终态（succeeded/failed/cancelled）但 Task 未闭环（桥接丢失）：按 Job 终态闭环（succeeded → completed + 结果卡片；failed → fail_task 透传 error_code）
  - 任一 Job 仍 queued/running（含恢复重置回 queued 的续跑任务）：保持 Task 现状，等待事件桥接
  - 无阶段或阶段无 Job 关联的 Task：不处理（由业务侧显式闭环）
- 幂等：终态 Task 跳过，重复执行返回 0
- 返回对账闭环数
- 实现复用现有原语：_JOB_STATUS_TO_STAGE / finish_stage / fail_task / complete_task / write_event / create_task / start_task / add_stage

### 1.2 Worker 启动挂载（scheduler.py）

`run_worker` 启动流程在 `_reconcile_comfy_timeout_jobs` 之后调用 `reconcile_orphan_tasks`，try/except 异常隔离，对账失败不影响主循环。

### 1.3 LLM 任务状态纪律（agent.py）

`_DIRECTOR_SYSTEM_PROMPT` 追加【任务状态纪律】3 条规则：

1. 只能引用 task_card / 事件流中的真实状态，不得编造任务进度或结果
2. 无数据时如实说明当前无进度信息，不得虚构
3. 失败解释只基于真实错误码，不得臆测原因

## 2. 测试

新增 `tests/test_task_recovery_reconcile.py`（6 用例，全部通过）：

1. 孤儿 succeeded Job → Task 闭环 completed + 结果卡片
2. 孤儿 failed Job → Task 闭环 failed（error_code 透传）
3. Job 仍 queued/running → Task 保持现状
4. 无阶段 / 阶段无 Job 关联 → 不处理
5. 终态 Task 幂等（重复对账返回 0）
6. 系统提示词包含任务状态纪律

执行记录：`pytest tests/test_task_recovery_reconcile.py -q` → 6 passed

开发中修复的两处问题（均已验证）：

- `reconcile_orphan_tasks` 中 `Task.status.in_("pending", "running")` 参数错误 → 改为列表
- 测试用例误将 fixture tuple 传入 create_task/start_task/add_stage → 改传解包后的 session

## 3. 全量回归

`pytest tests/ -q` → **503 passed / 7 failed**（360.25s）

7 个失败与 Phase 6 基线完全一致，均为 `/tmp/p0.png` root 所有权环境问题（Permission denied），非本次改动引入：

- tests/test_asset_purity.py::test_scene_batch_generate_injects_media
- tests/test_ref_and_parallel.py::test_multi_character_slot_priority
- tests/test_ref_and_parallel.py::test_character_never_occupies_ref_image
- tests/test_ref_and_parallel.py::test_character_slot_overflow_with_ref_image_declared
- tests/test_ref_and_parallel.py::test_multi_character_slot_overflow_422
- tests/test_ref_and_parallel.py::test_batch_generate_carries_media
- tests/test_ref_and_parallel.py::test_regenerate_carries_media

对比基线 497 passed → 503 passed（+6 为本次新增用例），无新增失败。

## 4. 后端重启与验证

- 旧进程 1622073 已停止，新进程 PID 1634655（uvicorn 127.0.0.1:8000）
- 健康检查：`GET /api/health` → 200
- 启动日志无异常（alembic 迁移上下文正常）

## 5. 变更文件清单

| 文件 | 变更 |
| --- | --- |
| app/services/task_orchestrator.py | +reconcile_orphan_tasks（含 in_ 参数修复） |
| app/worker/scheduler.py | run_worker 启动挂载对账（+8 行） |
| app/api/v1/agent.py | 系统提示词 +4 行任务状态纪律 |
| tests/test_task_recovery_reconcile.py | 新建（6 用例） |

## 6. 遗留事项

- 前端任务卡片组件（消费 payload.task_card 与 /api/tasks/events SSE，覆盖 parse/generate/compose/export 全部任务类型）——各阶段遗留的前端工作
- 二期：SSE 搜索事件流、搜索取消通道、OpenTelemetry
- 「剧本生成失败」疑似旧会话状态残留，待新会话验证
