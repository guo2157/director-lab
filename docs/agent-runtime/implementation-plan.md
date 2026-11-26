# Agent Runtime 实施进度

## 当前进度

| 项 | 内容 |
|---|---|
| 当前阶段 | Phase B · 最小可用版本（**已完成**：B0 → B11，门禁 B10 全绿） |
| 下一个任务 | Phase C · C0 统一工具目录 |
| 最近提交 | B11（本提交）、`0a25415` B10、`a75bafa` B9、`84cb666` B8 |
| 最近全量 pytest | 1625 passed / 0 failed（Phase A 结束基线 1560 passed / 1 xfailed；原始基线 1445 passed / 1 failed）；tests/test_agent_*.py = 331 passed / 0 xfailed |
| 更新时间 | 2026-11-27 03:05（Asia/Shanghai） |
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

- [x] **B0** 迁移与事件白名单（`a1b2c3d4e5fb`）
- [x] **B1** 抽出 `tick`：幂等的单次推进（`4f75a3a`）
- [x] **B2** Agent 事件发布（给前端看的事实流）（`66c3cd6`）
- [x] **B3** Job 终态 → 恢复标记（`e7ad13d`）
- [x] **B4** 审批闭环：自动申请、裁决后恢复、自动执行（`8ffa690`）
- [x] **B5** Worker 中的 agent-resume 线程与兜底扫描（本提交）
- [x] **B6** `/chat` 接入 Runtime（开关后面）（本提交）
- [x] **B7** 最小计划模板：资产生成与分镜图生成（本提交）
- [x] **B8** 确定性回复与状态查询接口（本提交）
- [x] **B9** 暂停、继续、取消（本提交）
- [x] **B10** 端到端 MVP 验收测试（`0a25415`）
- [x] **B11** 收尾：文档、开关策略、清理（本提交）

| 阶段完成记录 | 内容 |
|---|---|
| 合并的 PR | 无（按用户要求仅本地提交，不推送、不开 PR） |
| 门禁 B10 | `tests/test_agent_phase_b_e2e.py` 7 个场景全绿；Phase A 的 `test_p0_9_resume_has_caller` 已从 xfail 转正并通过 |
| 全量 pytest（修复前 → 修复后） | 基线 1445 passed / 1 failed → Phase A 末 1560 passed / 1 xfailed → Phase B 末 **1625 passed / 0 failed** |
| 真实库核对 | 只读三项：① `alembic_version` 仍为 `a1b2c3d4e5f9`（Phase B 迁移 head `a1b2c3d4e5fb` 从未对真实库执行）；② 用临时库升级到 `a1b2c3d4e5f9` 后与真实库逐对象比对 `sqlite_master`（type/name/sql）：287 个对象、0 差异；③ 业务表行数无异常突变（project 6 / job 211 / asset 161 / character 6 / scene 8 / shot 42，agent_goal 2 / agent_plan 8 / agent_plan_step 46 为 Phase A 已有记录）。`PRAGMA table_info` 确认真实库 `agent_plan_step` / `agent_goal` 均无 Phase B 新列 |
| 遗留问题 | 见下节「Phase B 遗留问题汇总」 |

### Phase B 开关策略

- `DLAB_AGENT_RUNTIME_CHAT_ENABLED`（`settings.agent_runtime_chat_enabled`）**默认 `false`**，本阶段保持关闭，
  线上 `backend/data/runtime_settings.json` 未写入该项。
- 关闭时 `/chat` 行为与接入前一致：`tests/test_agent_phase_b_chat.py::test_flag_off_behavior_unchanged`
  用黄金快照 `tests/fixtures/chat_baseline_flag_off.json` 逐字节比对问答 / 导航 / 单镜头生成 / 批量资产生成四条输入，
  并把 Runtime 分支函数替换为「一旦被调用即失败」；`test_agent_phase_b_e2e.py::test_flag_off_untouched` 再确认不产生任何 `agent_goal`。
- 计划中的「本地用示例项目《游乐场》手动验收后把 `.env.example` 改为 true」**未执行**：仓库没有 `.env.example`
  （配置走 `backend/.env`，且 `.gitignore` 忽略），且用户明确要求不要打开该开关。因此本阶段只在文档里给出建议值，
  默认值保持 `false`，留待 Phase C 完成、用户手动验收后再打开（见 `docs/DECISIONS.md` AG-41）。
- `DLAB_AGENT_RESUME_ENABLED`（`settings.agent_resume_enabled`）默认 `true`；`agent_runtime_chat_enabled` 关闭时
  `/chat` 不创建 Goal，agent-resume 线程遇不到任何带标记的步骤（`test_chat_flag_off_means_no_marked_steps` 证明）。
- `DLAB_AGENT_LOOP_DEBUG_ENABLED` 默认 `false`，调试接口 `/api/agent/loop/run` 关闭时返回 404。

### Phase B 遗留问题汇总（Phase C/D 输入）

1. `approvals.create_confirmation` / `resolve_confirmation` / `mark_task_finished` 内部 `db.commit()`，
   调用后无法用回滚撤销；Phase B 未重构（通用约束 5），tick 与审批写入不是单事务。
2. `resolve_confirmation` 写 workspace 事件时 `session_id` 可能取不到值（审批记录本身不带 session），
   前端依赖该字段的订阅会拿不到；需在 Phase D 统一审批事件的会话归属。
3. 流程工具（`PIPELINE_TOOL_REGISTRY`）与返工工具（`REGEN_TOOL_REGISTRY`）尚未接入 Runtime；
   Plan Validator 目前直接拒绝不在 `TOOL_REGISTRY` 中的工具，Phase C 再按需放开。
4. `agent-resume` 守护线程要等线上服务下次重启才生效（本轮不重启，属「必须停下」的情形）；
   重启前，恢复只依赖 `/chat` 的同步 tick 与手动调用。
5. 取消 Goal 时只对本系统内 `queued` / `running` 的 Job 调用 `cancel_jobs`；
   已提交到远端 ComfyUI 但没有本地 `prompt_id` 的任务无法撤回，会在 Worker 侧自然跑完（结果不再回写 Goal）。
6. `/chat` 的暂停 / 继续 / 取消只认精确短语（AG-37），自然语言表达要等 Phase C 的 GoalSpec。
7. 批量生成步骤仍是一步一个审批记录；Phase C 会把批量步骤按实体拆分 / 合并（Phase A A5 已注明）。

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
