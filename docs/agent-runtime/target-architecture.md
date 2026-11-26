# Director-Lab Agent Runtime 目标架构

> 基于 Phase 0 审计结果绘制
> 对齐 Agent Runtime 架构升级 Plan §3-§25

---

## 目标分层（Phase B 实际落地形态）

Phase B 只落地了「MVP」这一层：`/chat` 把批量生产类请求交给 Runtime，
审批 / Job 完成后由 Worker 自动续跑，重启由兜底扫描补齐。图中标注了每块的实现文件。

```
                         User
                          │
        POST /api/agent/{session_id}/chat          （开关 agent_runtime_chat_enabled，默认关闭）
                          │
              agent_runtime_router.should_use_runtime()
                │ 命中批量资产生成 / 批量分镜图 / 继续上次任务
                ▼
        agent_runtime.run_agent_loop()
          ├─ goal_manager.get_or_create_goal()   指纹相同复用，不同则 supersede
          ├─ planner.build_plan_from_template()  B7 两个模板，代码生成、不用 LLM
          └─ agent_runtime.tick()                幂等单次推进（B1）
                 ├─ 抢 Goal 锁（agent_goal.locked_until 条件 UPDATE）
                 ├─ 认领恢复标记（agent_plan_step.resume_requested_at）
                 ├─ 逐步执行 → execute_step → 只走 agent_tools.execute_tool
                 │     ├─ 同步成功 → verifier.verify_step → succeeded
                 │     ├─ 返回 job_id → waiting_job（TaskStage 挂到 Goal 的统一 Task）
                 │     ├─ 需要审批 → waiting_approval（自动创建 AgentConfirmation）
                 │     └─ 失败 → replanner.classify_error → 重试 / 等待用户 / 失败
                 ├─ 每步提交一次（崩溃不丢已完成步骤）
                 └─ _derive_loop_status → agent_goal.lifecycle_state + agent.* 事件
                          │
        ┌─────────────────┴──────────────────┐
        │                                    │
   HTTP 同步推进                        Worker 内嵌线程
   （/chat 首次 tick）                agent_resume.start_resume_thread()
                                          ├─ 每 2s process_marked()：认领标记 → tick
                                          └─ 每 30s sweep()：补标记 + 回收超时 running
                                                     │
                                          Job 执行器（executor）
                                          task_orchestrator.bridge_job_event()
                                            └─ 终态时 mark_agent_steps_for_job()
                                          approvals.resolve_confirmation()
                                            └─ 裁决时写 resume_requested_at
```

### 恢复闭环为什么用「标志位 + 兜底扫描」

Phase B 没有新建事件表，也不用事件游标。恢复请求就是 `agent_plan_step.resume_requested_at`：

- `bridge_job_event` 在 Job 进入终态时、与业务写入同一事务，把 `waiting_job` 步骤打上标记；
- `approvals.resolve_confirmation` 在裁决提交前，把 `waiting_approval` 步骤打上标记；
- Worker 的 agent-resume 线程每 2 秒认领标记并 tick；
- 标记丢失（进程崩溃、事务回滚）由每 30 秒的 `sweep()` 重新推导：
  Job 已终态但步骤仍 `waiting_job`、审批已裁决但步骤仍 `waiting_approval`、
  `running` 超时、以及 `lifecycle_state=running` 但 `last_tick_at` 超过 5 分钟且有 pending 步骤。

优点：不依赖长连接、不依赖页面在线、不需要维护事件游标；重复打标记是幂等的。

### Goal ↔ AgentTask ↔ 统一 Task

```
agent_goal (1) ──── agent_task_id ───▶ agent_task   （用户级任务：意图快照 + 状态机）
     │
     └────────────── task_id ─────────▶ task         （统一 Task：承载本 Goal 下所有 Job 阶段）
                                          └── task_stage（每个产生 Job 的步骤挂一个 stage）
```

一个 Goal 只有一个统一 Task；产生 Job 的步骤把 `TaskStage` 挂到这个 Task 上，
不再「一步一个 Task」。`goal.task_id` 为空时（例如调试接口创建的 Goal）才新建统一 Task 并回写。

### 前端可见的事实流（workspace_event outbox）

`agent_events.py` 把 Goal / Plan / Step / tick 的事实写入 `workspace_event`（与业务同一事务）：

| 事件 | payload |
| --- | --- |
| `agent.goal.created` / `updated` / `finished` | goal_id, status, lifecycle_state, goal_type |
| `agent.goal.superseded` | goal_id, superseded_by |
| `agent.plan.created` | goal_id, plan_id, revision, steps[{step_id, step_key, tool_name, status}] |
| `agent.step.updated` | goal_id, plan_id, step_id, step_key, tool_name, status, job_id?, confirmation_id?, error_code? |
| `agent.tick.finished` | goal_id, lifecycle_state, counts{succeeded, failed, waiting, pending} |

payload 只放事实字段（id / 状态 / 错误码），不放 LLM 文本、提示词或进度百分比估算。
这些事件**只用于前端展示**，不作为恢复驱动。

### 状态与查询接口

- `GET /api/agent/{session_id}/goal`：当前 active Goal；没有 active 时回退最近一个非 superseded 的 Goal。
- `GET /api/agent/{session_id}/goals`：历史 Goal 分页。
- `GET /api/agent/goals/{goal_id}`：按 id 查询同一份载荷。
- `POST /api/agent/goals/{goal_id}/pause|resume|cancel`：暂停 / 恢复 / 取消。
- 生命周期：planning / running / waiting_job / waiting_approval / waiting_user / paused /
  partially_completed / completed / failed / cancelled。

---

## 完整目标架构（Phase C 之后逐步补齐）

下面的分层是最终形态，Phase B 只实现了其中「Goal Manager → Planner（模板）→ Execution Manager →
Verifier → Replanner → Response Manager」这条最小链路；Context Resolver、Constraint Manager、
通用状态驱动 Planner、审核门禁、前端工作台在 Phase C/D/E 落地。

```
                         User
                          │
                   Agent Workbench (前端, Phase E)
                  Plan/Step/Job/产物/审核展示
                          │
                     Agent API (保留路由层)
                          │
                    Agent Runtime (新增核心)
                          │
          ┌───────────────┼────────────────┐
          │               │                │
    Goal Manager    Context Resolver   Constraint Manager
          │               │                │
          └───────────────┼────────────────┘
                          │
                       Planner -> Plan Validator
                          │
                    Execution Manager
                          │
             ┌────────────┼────────────┐
        Tool Registry   Human Gate   Task Manager
             │            │            │
             └────────────┼────────────┘
                          │
                       Executor
                          │
                   Business Tool Layer (现有 agent_tool_impls)
                          │
                  Business Services (不改动)
                          │
           Worker -> Job -> ComfyUI / TTS / FFmpeg
                          │
                       Verifier
                          │
                  ┌───────┴────────┐
                  │                │
               Replan          Complete
                  │                │
                  └───────┬────────┘
                          │
                  Response Manager
                          │
                         User
```

## 核心模块设计（最终形态）

### 1. Goal Manager (app/services/goal_manager.py)

**改造来源**: Parent Agent 的 create_task_from_intent -> Goal Manager

| 功能 | 实现 |
|------|------|
| 目标结构化 | 用户请求->Goal(goal_id/project_id/constraints/success_criteria/status) |
| 目标追踪 | 维护Goal生命周期,不每次消息孤立创建 |
| 目标判定 | 检查project/script/asset/review状态,判断Goal完成度 |
| 复用 | 现有 AgentSession.project_id + derive_progress 项目实际状态 |

**数据模型**: agent_goal 表 (project_id/session_id/description/goal_type/constraints/success_criteria/status)

### 2. Context Resolver (app/services/context_resolver.py)

**改造来源**: intent_resolver.py + context_manager.py -> Context Resolver

| 层级 | 来源 | 策略 |
|------|------|------|
| User Context | AgentSession + permissions | 直接查询 |
| Session Context | AgentMessage + session_memory | 短期窗口+摘要 |
| Project Context | Project/Script/Asset/Shot + derive_progress | 实时DB查询 |
| Task Context | AgentTask + Task + TaskStage | 仅当前Goal关联 |
| Job Context | Job + JobEvent | 仅当前Step关联 |
| Memory | AgentMemory | 按project_id/type检索 |

**Context Snapshot**: 执行Plan前快照(Project Version/Script Version/Asset Version/Stage)

### 3. Planner (app/services/planner.py)

**改造来源**: 新建,替代 pipeline_state 固定流程

| 功能 | 实现 |
|------|------|
| Plan生成 | Goal+Context->Plan(plan_id/revision/steps) |
| 依赖分析 | depends_on[step_id...] + 循环检测 |
| 条件任务 | precondition: {stage/asset_review/script_parsed} |
| 并行拆分 | 独立step自动并行 |
| Plan Validation | 工具存在性/权限/依赖/资源/危险操作 14项检查 |
| Revision | Plan变更->revision+1,保留历史 |

**数据模型**: agent_plan + agent_plan_step 表

### 4. Execution Manager (app/services/execution_manager.py)

**改造来源**: 新建,整合 task_orchestrator + agent_tools 执行

| 功能 | 复用 |
|------|------|
| Step执行 | task_orchestrator.create_task + TaskStage |
| Tool Selection | agent_tools.allowed_tools (动态筛选) |
| Input校验 | agent_tools Input Schema + Precondition |
| 权限检查 | agent_governance PolicyDecision |
| Async Job | task_orchestrator bridge_job_event |
| 超时/Retry | 新增 Timeout + RetryPolicy |
| 执行锁 | 新增 execution_lock (plan_step级别) |

### 5. Verifier (app/services/verifier.py)

**改造来源**: execution_verifier.py -> Verifier (扩大范围)

| 维度 | 方法 |
|------|------|
| State | Task/Job/AgentTask状态检查 |
| Artifact | 文件存在/有效性/版本检查 |
| Business | 业务规则/依赖/审核/流程检查 |
| Quality | 模型辅助(可选) |
| Human | 人工审核状态/版本检查 |

**结果**: VerificationResult(passed/failed/partial/needs_review, checks[], issues[])

### 6. Replanner (app/services/replanner.py)

**改造来源**: 新建

| 触发 | 策略 |
|------|------|
| Tool失败 | 错误分类->Retry/Replan/Wait/Fail |
| Job失败 | 查询真实Job状态->超时->Retry/Replan |
| 审核拒绝 | 更新约束->Replan |
| 依赖变化 | 重新检查->Replan或跳过失效step |
| 预算超限 | 安全降级->WAITING_USER |

**限制**: 最大Retry 3次,最大Replan 3次,Token/Time/Cost上限

### 7. Human Gate Manager (app/services/human_gate.py)

**改造来源**: approvals.py + pipeline_state.py -> Human Gate Manager

| 闸口 | 目标 |
|------|------|
| 生成审批 | 保留现有 approve/reject/execute 三段式 |
| 资产审核 | 恢复: 角色/场景/道具->人工审核->通过才进入分镜图 |
| 分镜图审核 | 恢复: 分镜图->人工审核->通过才进入分镜视频 |
| 版本绑定 | 审核绑定 Asset Version,修改后原审核失效 |
| 后端门禁 | 业务服务入口检查审核状态+版本 |

---

## Agent Loop 设计

```
START
  │
  ▼
Resolve Goal ---- GoalManager.get_or_create_goal()
  │
  ▼
Resolve Context -- ContextResolver.snapshot(project_id, goal_id)
  │
  ▼
Check Constraints - ConstraintManager.check(goal, context)
  │
  ▼
Check Goal Completion
  ├── done -> Deliver (ResponseManager)
  └── not done
       │
       ▼
     Build/Update Plan - Planner.generate(goal, context)
       │
       ▼
     Validate Plan -- PlanValidator.check(plan)
       │
       ▼
     Select Next Step - pending中depends_on满足者
       │
       ▼
     Check Preconditions - asset_gate/stage_readiness
       ├── not met -> Wait/Replan
       └── met
            │
            ▼
          Check Permission - agent_governance.PolicyDecision
            ├── DENIED_BY_POLICY -> Skip/Fail
            ├── WAITING_APPROVAL -> Human Gate
            └── ALLOW
                 │
                 ▼
               Execute Tool - ExecutionManager.execute(step)
                 │
                 ▼
               Handle Result
                 ├── sync_success -> Verify -> next_step
                 ├── async_job -> WAITING_JOB
                 ├── failed -> Replanner.replan(step, error)
                 └── cancelled -> mark_step_cancelled
                        │
                        ▼
                      Verify - Verifier.verify(step, result)
                        ├── failed -> Retry/Replan
                        └── passed -> Persist State -> Update Context -> Loop
```

### Agent Loop 持久化（Phase B 实际实现）

- 每次工具调用写一条 `agent_execution_log`（含 input/output/error_code/duration_ms）。
- 步骤状态变化写 `agent.step.updated` 到 `workspace_event` outbox（同一事务，**不新建 agent_event 表**）。
- Job 进入终态：`bridge_job_event` 与业务写入同一事务给 `waiting_job` 步骤打 `resume_requested_at`；
  审批裁决：`resolve_confirmation` 提交前给 `waiting_approval` 步骤打标记。
- Worker 的 agent-resume 线程每 2s 认领标记并 `tick()`；每 30s `sweep()` 补标记、回收超时 running 步骤、
  推进「running 但 5 分钟无 tick 且有 pending 步骤」的崩溃 Goal。
- 页面刷新 / 服务重启后重读 `agent_plan` + `agent_plan_step`，兜底扫描补齐丢失的标记，不重复创建 Job。

---

## 数据模型扩展

| 表 | 用途 | 关联 |
|---|---|---|
| agent_goal | 用户目标持久化 + 指纹 + 生命周期 + Goal 锁 + AgentTask/Task 绑定 | session_id, project_id |
| agent_plan | 执行计划 + revision + parent_plan_id | goal_id |
| agent_plan_step | 计划步骤（step_key / job_id / confirmation_id / 锁 / 恢复标记） | plan_id |
| agent_execution_log | 执行记录 | goal_id / plan_id / step_id |
| agent_confirmation | 审批记录（按步骤绑定，不可复用） | — |
| workspace_event | 事件 outbox（含 `agent.*` 事实流） | session_id / project_id |

**Phase B 明确没有新建**：`agent_event` 表、事件游标表、第二套工具注册表、并行执行器。
恢复靠 `agent_plan_step.resume_requested_at` 标志位 + Worker 周期兜底扫描。

现有表改动（Phase A + Phase B 迁移，全部只加可空列）:
- `agent_goal`：`fingerprint`、`locked_until`、`agent_task_id`、`task_id`、`lifecycle_state`、`last_tick_at`
- `agent_plan_step`：`confirmation_id`、`job_id`、`step_key`、`lock_owner`、`resume_requested_at`、`resume_reason`
- `agent_plan`：`parent_plan_id`
- `agent_task`：`goal_id`、`plan_id`、`current_step_id`、`lifecycle_state`（Phase 1 已有）

---

## 与现有系统接口

| Runtime模块 | 调用现有 | 方式 |
|------------|---------|------|
| Goal Manager | Project/Script模型, derive_progress | 只读查询（Phase B 模板计划复用 assets_gate / stage_readiness 判定） |
| Context Resolver | 所有Models, context_manager | 只读查询 |
| Planner | pipeline_state(参考) | 读流程节点定义 |
| Execution Manager | task_orchestrator, agent_tools, approvals | 创建Task+TaskStage, 调用Tool |
| Verifier | execution_verifier, assets_gate, stage_readiness, parse_gate | 组合调用 |
| Replanner | task_orchestrator(bridge_*), approvals | 状态更新 |
| Human Gate | approvals, pipeline_state, review_confirmation | 审批+门禁 |
| Response Manager | Agent API SSE流 | 注入Plan/Step/Goal状态 |
