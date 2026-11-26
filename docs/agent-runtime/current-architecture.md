# Director-Lab 当前架构

> 审计时间: 2026-11-25
> 基于真实代码绘制，不含推测

---

## 整体分层

```
┌─────────────────────────────────────────────────────────┐
│                   Frontend (Vue3)                        │
│  DirectorConsoleView -> ChatComposer/MessageStream/       │
│  ApprovalList/ContextSidebar/SessionRail                 │
├─────────────────────────────────────────────────────────┤
│                   Agent API (agent.py, 2122行)            │
│  POST /sessions -> CRUD                                    │
│  POST /{id}/messages -> SSE 对话流                        │
│  GET  /{id}/approvals -> 审批面板                          │
│  POST /{id}/approvals/{cid}/resolve -> 裁决               │
│  POST /{id}/approvals/{cid}/execute -> 执行               │
│  GET/POST /{id}/reviews -> 业务审核                        │
├─────────────────────────────────────────────────────────┤
│              Agent Services (app/services/)               │
│                                                          │
│  intent_router.py    --- 规则+LLM意图识别                │
│  intent_resolver.py  --- 显式ID/焦点/澄清                │
│  parent_agent.py     --- AgentTask创建/状态机            │
│  agent_tools.py      --- Tool Registry(29工具)            │
│  agent_tool_impls.py --- 工具实现(1771行)                │
│  agent_governance.py --- 动作枚举/预算控制                │
│  approvals.py        --- 审批创建/裁决/执行分离           │
│  pipeline_state.py   --- 7节点流程链+门禁                │
│  pipeline_tools.py   --- 流程工具                         │
│  task_orchestrator.py --- Task/DAG调度/事件桥接           │
│  execution_verifier.py --- Job真实性验证                  │
│  assets_gate.py      --- 资产就绪门禁(已移除审核)        │
│  stage_readiness.py  --- 各阶段产物就绪判定              │
│  parse_gate.py       --- 解析结果准入                     │
│  context_manager.py  --- Focus/Operation/Execution上下文  │
│  agent_memory.py     --- 分层记忆                         │
│  session_memory.py   --- 会话摘要压缩                     │
│  event_stream.py     --- SSE outbox轮询                   │
│  progress.py         --- 12阶段推导                       │
├─────────────────────────────────────────────────────────┤
│                  Data Models (app/models/)               │
│  agent: AgentSession/Message/Task/PlanStep/Confirmation/  │
│         Memory                                            │
│  production: Asset/Workflow/Job/JobEvent/Composition/     │
│              Export/ParseVersion/DependencyRef            │
│  task: Task/TaskStage/TaskEvent/TaskResult               │
├─────────────────────────────────────────────────────────┤
│                  Worker (app/worker/)                     │
│  scheduler.py: 调度主循环,ComfyUI池化,启动恢复,对账      │
│  executor.py: 生成任务执行                                 │
│  compose_executor.py / parse_executor.py / export_executor │
├─────────────────────────────────────────────────────────┤
│              Integrations (app/integrations/)             │
│  comfyui.py/pool / llm.py / model_capability.py           │
└─────────────────────────────────────────────────────────┘
```

---

## 请求处理链路（当前）

```
用户消息 (POST /sessions/{id}/messages)
  │
  |-(1) 会话校验: active/archived
  │
  |-(2) 消息入库: seq 自增, role=user
  │
  |-(3) 意图识别
  │   |-- intent_router.route(user_content)
  │   │   |-- fast_path_rules: unsafe/regen/progress/pipeline_action/query
  │   │   |-- classify_intent (LLM结构化)
  │   |-- intent_resolver.resolve_intent(intent)
  │       |-- explicit_id: 镜头/实体ID提取+DB验证
  │       |-- focus_context: 回退到context_manager
  │       |-- clarification: 后退澄清
  │
  |-(4) 任务创建（仅 generate_request / pipeline_action）
  │   |-- parent_agent.create_task_from_intent -> AgentTask
  │
  |-(5) 审批创建（生成类意图）
  │   |-- approvals.create_confirmation -> AgentConfirmation
  │
  |-(6) SSE 对话流构建
  │   |-- 系统提示词 + 记忆上下文 + 阶段指引
  │   |-- 工具列表: get_allowed_tools(intent)
  │   |-- 聊天历史 + 会话摘要
  │   |-- LLM 流式调用 -> yield SSE
  │
  |-(7) 工具调用（LLM 请求时）
  │   |-- agent_tools.execute_tool -> precheck+Schema+impl
  │
  |-(8) 生成执行（用户审批后）
      |-- resolve_confirmation -> approve/reject
      |-- execute_approval -> pipeline_tools/run_generate
      |-- task_orchestrator.create_task -> Task+TaskStage
      |-- execution_verifier -> 验证Job
      |-- bridge_job_event -> TaskStage闭环
```

---

## 数据流关系

```
AgentSession (会话)
  |-- AgentMessage (消息, seq递增)
      │
      |-- AgentTask (意图任务)
          |-- Intent: query/analysis/generate_request/pipeline_action
          |-- Status: pending->running/waiting_approval->completed/failed/cancelled
          |
          |-- AgentConfirmation (审批)
          |   |-- tool_name/args/fingerprint
          |   |-- Status: pending->approved/rejected
          |
          |-- AgentPlanStep (计划步骤)
          |   |-- step_type: tool_call/clarification/reply
          |   |-- Status: pending->running->succeeded/failed/skipped
          |
          |-- Task (统一编排层, agent_task_id关联)
              |-- TaskStage (执行阶段, job_id关联)
                  |-- depends_on: [stage_key...]
                  |-- Status: pending->running->succeeded/failed
                  |-- Job (生产执行, worker消费)
                      |-- Status: queued->running->succeeded/failed
                      |-- JobEvent (outbox, seq递增)

AgentMemory (分层记忆)
  |-- memory_type: project/preference/experience
  |-- Status: active/superseded
```

---

## 关键设计约束（已实现）

1. 意图识别与执行分离: 模型只识别，确定性代码执行
2. 审批-执行分离: create->resolve->execute三段式
3. 状态机门禁: AgentTask状态转换有明确规则
4. Job-事件-outbox: 全局递增seq,SSE增量轮询
5. Worker自适应并发: ComfyUI池化,启动恢复,对账
6. 记忆分层: 项目/偏好/经验,supersede可追溯
7. 上下文分类: Focus/Operation/Execution,项目切换清空

---

## 架构关键缺口（需改造）

| 缺口 | 当前状态 | 目标 |
|------|---------|------|
| Goal Manager | 无——每次消息独立解析Intent | 结构化Goal持久化追踪 |
| Context Resolver | context_manager仅存Focus/Operation | 统一分层上下文 |
| Planner | AgentPlanStep存在但未被系统使用 | 动态构建+依赖/并行/条件 |
| Plan Validation | 无 | 循环依赖/权限/资源校验 |
| Executor | execute_tool单次调用 | 统一调度+超时/Retry/锁 |
| Verifier | 仅验证Job存在 | 状态+产物+业务+质量+人工五维 |
| Replanner | 无 | 错误分类->Retry/Replan/Wait/Fail |
| Human Gate | 仅生成审批,资产/分镜审核已移除 | 恢复审核门禁+版本绑定 |
| Agent Loop | 依赖SSE请求,刷新即断 | 持久化可恢复循环 |
| Memory Retrieval | 仅列表注入提示词 | 语义检索+动态选择 |
