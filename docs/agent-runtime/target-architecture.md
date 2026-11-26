# Director-Lab Agent Runtime 目标架构

> 基于 Phase 0 审计结果绘制
> 对齐 Agent Runtime 架构升级 Plan §3-§25

---

## 目标分层

```
                         User
                          │
                   Agent Workbench (前端)
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

---

## 核心模块设计

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

### Agent Loop 持久化

- 每次循环后写 agent_execution_log
- 每个Step状态变更写 agent_event
- Job完成后通过 bridge_job_event 触发Loop恢复
- 审核完成后通过 bridge_approval_resolved 触发Loop恢复
- 页面刷新/服务重启后重读 agent_plan + agent_plan_step 继续

---

## 数据模型扩展

| 表 | 用途 | 关联 |
|---|---|---|
| agent_goal | 用户目标持久化 | session_id, project_id |
| agent_plan | 执行计划 + revision | goal_id |
| agent_plan_step | 计划步骤(重构自现有) | plan_id |
| agent_execution_log | 执行记录(新增) | goal_id/plan_id/step_id |
| agent_event | 持久化事件(新增) | goal_id/plan_id/step_id/job_id |
| agent_verification | 验证记录(新增) | step_id |

现有表改动:
- agent_task + goal_id + plan_id + current_step_id + lifecycle_state
- agent_plan_step 重构为 plan->step 二层

---

## 与现有系统接口

| Runtime模块 | 调用现有 | 方式 |
|------------|---------|------|
| Goal Manager | Project/Script模型, derive_progress | 只读查询 |
| Context Resolver | 所有Models, context_manager | 只读查询 |
| Planner | pipeline_state(参考) | 读流程节点定义 |
| Execution Manager | task_orchestrator, agent_tools, approvals | 创建Task+TaskStage, 调用Tool |
| Verifier | execution_verifier, assets_gate, stage_readiness, parse_gate | 组合调用 |
| Replanner | task_orchestrator(bridge_*), approvals | 状态更新 |
| Human Gate | approvals, pipeline_state, review_confirmation | 审批+门禁 |
| Response Manager | Agent API SSE流 | 注入Plan/Step/Goal状态 |
