# Agent Runtime 实施进度

## 当前进度

| 项 | 内容 |
|---|---|
| 当前阶段 | Phase A · 止血与正确性（已完成，等待 Phase B） |
| 下一个任务 | Phase B · B0 迁移与事件白名单 |
| 最近提交 | A12b（本提交）、`57149be` 交接文档、`3cc9490` A11、`5e335b1` A10 |
| 最近全量 pytest | 1560 passed / 1 xfailed / 0 failed（基线 1445 passed / 1 failed，2026-11-26） |
| 更新时间 | 2026-11-26 19:10（Asia/Shanghai） |
| 工作区 | 干净（每次任务一个本地提交，不推送） |

> 本文件替换 Phase 0 时期的旧版 implementation-plan.md。
> 各阶段的详细任务说明见 [plan/](plan/README.md)；编码 Agent 完成一个任务后，在这里勾选对应条目，**不要新建平行文档**。
> 阶段之间有门禁：上一阶段的验收测试全绿，才能开始下一阶段。

## Phase 0 · 审计与设计 ✅

- [x] 2026-11-25 审计产出（audit-report.md 等），2026-11-26 基于最新代码修订为最终 Plan（[plan/final-plan.md](plan/final-plan.md)）

## Phase A · 止血与正确性

- 前置条件：无（基线 tree `1df9fe8b`）
- 验收门禁：A12b 回归汇总：每个 P0 缺陷一条命名回归用例
- 任务说明：[plan/phase-a-agent-prompts.md](plan/phase-a-agent-prompts.md)

- [x] **A00** 测试隔离：数据库地址强制指向临时库（必须最先完成）— `0eaac87`
- [x] **A00b** 测试守卫：禁止真实 LLM / ComfyUI 网络请求 — `f1bb505`
- [x] **A0** 数据模型补丁（一次迁移，为后续任务铺路）— `63b624a`
- [x] **A12a** 测试基建：Runtime 专用夹具（先于 A1 完成）— `f99b886`
- [x] **A1** 事务边界：Runtime 入口自管 Session 与提交（P0-1）— `5a54196`
- [x] **A2** 调试接口的线程与 SSE 桥接（P0-2、P0-3）— `654a785`
- [x] **A3** 进度回调透传与结果汇总顺序（P0-4）— `2223d14`
- [x] **A4** Executor 只走 execute_tool 网关（P0-5）— `4c039a4`
- [x] **A5** 结果分流：Job → waiting_job，审批 → waiting_approval（P0-6）— `a6b2348`
- [x] **A6** Verifier 以真实状态判定（P0-7）— `cace2f0`
- [x] **A7** 审批按步骤绑定（P0-8）— `e8c2f38`
- [x] **A8** 目标指纹：新目标不复用旧计划（P0-10）— `8131b61`
- [x] **A9** Replan 产生新 revision 并复用已完成步骤（P0-11）— `ca0fe1c`
- [x] **A10** 状态接口、终态与词表收敛（P0-12 及相关）— `5e335b1`
- [x] **A11** 原子执行锁（P1）— `3cc9490`
- [x] **A12b** 回归汇总与文档收尾（最后执行）

| 阶段完成记录 | 内容 |
|---|---|
| 合并的 PR | 无（按用户要求仅本地提交，不推送、不开 PR） |
| 全量 pytest（修复前 → 修复后） | 基线 1445 passed / 1 failed → 1560 passed / 1 xfailed / 0 failed |
| 遗留问题 | P0-9（resume 无调用方）、approval_required 自动建审批、批量步骤拆分、Worker 消费事件均在 Phase B/C 处理；reclaim_stale_steps 未接入调度（Phase B Worker 调用） |

### Phase A 止血 · 逐条勾选

- [x] A00 测试隔离（`0eaac87`）/ A00b 网络守卫（`f1bb505`）
- [x] A0 数据模型补丁（`63b624a`）/ A12a 测试基建（`f99b886`）
- [x] A1 事务边界（`5a54196`）/ A2 调试接口线程与 SSE（`654a785`）/ A3 进度回调（`2223d14`）
- [x] A4 只走 execute_tool 网关（`4c039a4`）/ A5 Job 与审批分流（`a6b2348`）
- [x] A6 Verifier 真实状态（`cace2f0`）/ A7 审批按步骤绑定（`e8c2f38`）
- [x] A8 目标指纹（`8131b61`）/ A9 Replan revision（`ca0fe1c`）
- [x] A10 状态接口与终态（`5e335b1`）/ A11 原子执行锁（`3cc9490`）
- [x] A12b 回归汇总与文档收尾（本提交）

### Phase B 前置条件

- `resume_after_job` / `resume_after_approval` 需要真实调用方：`task_orchestrator.bridge_job_event` 与审批裁决处接入。
- Worker 需要消费 `agent.resume_requested` 事件并推进 tick。
- `/api/agent/{session_id}/chat` 主链路接入 Goal Manager → tick（Phase A 明确未改动）。
- `approval_required` 时自动创建 `AgentConfirmation`（Phase A 只做状态映射）。

## Phase B · 最小可用版本（接入主链路与恢复闭环）

- 前置条件：Phase A 全部合并，`tests/test_agent_runtime_*.py` 全绿
- 验收门禁：B10 端到端 MVP（7 个场景）
- 任务说明：[plan/phase-b-agent-prompts.md](plan/phase-b-agent-prompts.md)

- [ ] **B0** 迁移与事件白名单
- [ ] **B1** 抽出 `tick`：幂等的单次推进
- [ ] **B2** Agent 事件发布（给前端看的事实流）
- [ ] **B3** Job 终态 → 恢复标记
- [ ] **B4** 审批闭环：自动申请、裁决后恢复、自动执行
- [ ] **B5** Worker 中的 agent-resume 线程与兜底扫描
- [ ] **B6** `/chat` 接入 Runtime（开关后面）
- [ ] **B7** 最小计划模板：资产生成与分镜图生成
- [ ] **B8** 确定性回复与状态查询接口
- [ ] **B9** 暂停、继续、取消
- [ ] **B10** 端到端 MVP 验收测试
- [ ] **B11** 收尾：文档、开关策略、清理

| 阶段完成记录 | 内容 |
|---|---|
| 合并的 PR | |
| 全量 pytest（修复前 → 修复后） | |
| 遗留问题 | |

## Phase C · 状态驱动 Planner 与工具层收敛

- 前置条件：Phase B 合并，`test_agent_phase_b_e2e.py` 全绿
- 验收门禁：C9 端到端（11 个场景，核心：只补缺失部分）
- 任务说明：[plan/phase-c-agent-prompts.md](plan/phase-c-agent-prompts.md)

- [ ] **C0** 统一工具目录（三套注册表的门面）
- [ ] **C1** 生产 DAG：阶段定义与"是否已满足"判定
- [ ] **C2** GoalSpec：把用户的话解析成结构化目标
- [ ] **C3** 状态驱动 Planner：滚动展开的计划
- [ ] **C4** 基于状态的 Replan 与版本变化检测
- [ ] **C5** 通用的"审批支撑步骤"
- [ ] **C6** 主对话的动态工具暴露
- [ ] **C7** 目标级预算
- [ ] **C8** 阶段级验证与目标完成判定
- [ ] **C9** 端到端场景测试（Phase C 验收门禁）
- [ ] **C10** 收尾：文档、清理、开关策略

| 阶段完成记录 | 内容 |
|---|---|
| 合并的 PR | |
| 全量 pytest（修复前 → 修复后） | |
| 遗留问题 | |

## Phase D · 人工审核门禁与版本绑定

- 前置条件：Phase C 合并，`test_agent_phase_c_e2e.py` 全绿
- 验收门禁：D9 端到端（10 个场景，含绕过 API 被拦截）
- 任务说明：[plan/phase-d-agent-prompts.md](plan/phase-d-agent-prompts.md)

- [ ] **D0** 迁移：资产行上的审核字段
- [ ] **D1** 审核开关（review_policy）
- [ ] **D2** 资产审核服务与接口
- [ ] **D3** 资产门禁接入审核
- [ ] **D4** 分镜图门禁：迁到服务层并接入审核
- [ ] **D5** Worker 兜底检查
- [ ] **D6** Runtime 接入审核步骤
- [ ] **D7** 在对话中审核
- [ ] **D8** 最小前端支持
- [ ] **D9** 端到端场景测试（Phase D 验收门禁）
- [ ] **D10** 收尾：文档与决策记录

| 阶段完成记录 | 内容 |
|---|---|
| 合并的 PR | |
| 全量 pytest（修复前 → 修复后） | |
| 遗留问题 | |

## Phase E · Agent 工作台前端

- 前置条件：Phase D 合并
- 验收门禁：E10 验收清单（12 项，附截图或录屏）
- 任务说明：[plan/phase-e-agent-prompts.md](plan/phase-e-agent-prompts.md)

- [ ] **E0** 前端基础：类型、接口、Store、事件订阅、单测框架
- [ ] **E1** 后端小补充：前端需要但现在缺少的接口
- [ ] **E2** GoalCard：当前目标卡片
- [ ] **E3** PlanSteps：计划步骤列表
- [ ] **E4** 等待入口：审批、生成任务、需要你处理、审核
- [ ] **E5** 接入 /director 导演台
- [ ] **E6** 会话侧栏与历史任务
- [ ] **E7** 侧边助手面板与项目切换
- [ ] **E8** 进度显示规则
- [ ] **E9** 响应式、可访问性与设计门禁
- [ ] **E10** 验收：模拟数据与端到端检查（Phase E 验收门禁）

| 阶段完成记录 | 内容 |
|---|---|
| 合并的 PR | |
| 全量 pytest（修复前 → 修复后） | |
| 遗留问题 | |

## Phase F · 记忆、指标、评测与收尾

- 前置条件：Phase E 合并（F6 后端 CI 可提前做）
- 验收门禁：F10 最终验收（32 条逐条对照 + 真实环境 + exe 打包）
- 任务说明：[plan/phase-f-agent-prompts.md](plan/phase-f-agent-prompts.md)

- [ ] **F0** 执行追踪补全
- [ ] **F1** LLM 调用计量
- [ ] **F2** 指标统计接口
- [ ] **F3** 指标页面（最小）
- [ ] **F4** 记忆检索接入与生命周期
- [ ] **F5** 经验记忆与目标解析评测集
- [ ] **F6** 后端 CI
- [ ] **F7** 仓库清理与文档收敛
- [ ] **F8** 运维与排障
- [ ] **F9** 原始 Plan 场景全集与回归清单
- [ ] **F10** 最终验收与发布

| 阶段完成记录 | 内容 |
|---|---|
| 合并的 PR | |
| 全量 pytest（修复前 → 修复后） | |
| 遗留问题 | |

## 已确认决策（2026-11-26）

| 决策 | 结论 |
|---|---|
| 资产 / 分镜审核门禁 | 项目级开关：新项目默认开启，存量项目默认关闭 |
| Planner 是否用 LLM 生成计划 | 不用：计划由代码根据真实进度生成，LLM 只解析目标 |
| `/api/agent/loop/run` | 保留为调试接口，前端不暴露 |
| 多用户权限 | 延后 |
