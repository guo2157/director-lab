# Director-Lab 导演台 Agent 当前架构（Phase 0 审计基线）

- 日期：2026-11-09
- 依据：backend/app/api/v1/agent.py、services/intent_router.py、intent_resolver.py、context_manager.py、parent_agent.py、task_orchestrator.py、agent_tools.py、agent_tool_impls.py、agent_context_tools.py、agent_governance.py、session_memory.py、agent_memory.py、execution_verifier.py、review_confirmation.py、approvals.py、progress.py、models/agent.py、models/task.py、api/v1/tasks.py、events.py、progress.py、frontend/src/api/agent.ts、composables/useTaskEvents.ts、components/TaskCard.vue、views/DirectorConsoleView.vue

---

## 1. 模块结构与调用关系

```text
frontend/src/views/DirectorConsoleView.vue
  └─ api/agent.ts（chat/sessions/approvals/reviews）
       └─ backend/app/api/v1/agent.py
            ├─ Session/Message CRUD（agent_session/agent_message）
            ├─ POST /{session_id}/chat 主流程
            │    1. 用户消息持久化（seq 冲突重试 3 次）
            │    2. _handle_pending_confirmation（确认答复解析）
            │    3. _handle_video_workflow（剧本/项目向导工作流）
            │    4. 联网搜索分支（规则正则 + SearchRun 状态机）
            │    5. intent_router.classify_intent（规则快路径 + LLM 分类）
            │    6. intent_resolver.resolve_intent（显式 ID → Focus → 澄清）
            │    7. 生成请求 → approvals.create_confirmation（审批门禁）
            │    8. parent_agent.create_task_from_intent + task_orchestrator
            │    9. agent_context_tools 只读上下文注入（ToolBudget）
            │   10. LLM 生成回复（重试 3 次）
            │   11. 助手消息持久化（payload 审计字段）
            └─ 审批/审核/执行接口（resolve_approval/execute_approval/resolve_review）

services 层：
  intent_router（6 类意图 + route_action：CHAT/REJECT/GUIDE/PIPELINE/CLARIFY）
  intent_resolver（ResolvedIntent：target/requires_clarification/resolution_path）
  context_manager（AgentSession.context_state：focus/operation，项目切换清空）
  parent_agent（AgentTask：pending/running/completed/failed/cancelled）
  task_orchestrator（Task + TaskStage：queued/running/completed/failed/cancelled，事件 outbox）
  agent_tools（TOOL_REGISTRY：权限/风险/幂等/审批门禁 + execute_tool）
  agent_tool_impls（各工具实现）
  agent_context_tools（只读工具选择 + 上下文格式化）
  agent_governance（NextAction/TerminalStatus/ToolBudget/PolicyEngine）
  session_memory（短期窗口 40 条 + 摘要压缩 summary_upto_seq）
  agent_memory（project/preference/experience，active/superseded）
  execution_verifier（Job 真实验证：job_id_missing/job_not_found）
  review_confirmation（artifact_review 确认：幂等键/TTL 过期/审计）
  approvals（tool_approval：入参指纹/审批与执行分离）
  progress（derive_progress 12 阶段 + generation_progress_snapshot）

data 层：
  agent_session / agent_message / agent_task / agent_plan_step
  agent_confirmation / agent_memory
  task / task_stage / task_event / task_result

事件层：
  JobEvent outbox（/api/events，last_seq 递增）
  TaskEvent outbox（/api/tasks/events，seq 严格大于）
  前端 useTaskEvents：onopen 触发 backfill、snapshot 合并
```

## 2. 关键类与函数

### agent.py（1884 行）
- chat 主流程（POST /{session_id}/chat）
- _handle_pending_confirmation：approve/reject/revise/defer/unclear
- _handle_video_workflow：STAGE_IDLE/ASK_STYLE/ASK_SOURCE/WAIT_USER_SCRIPT/SCRIPT_DRAFT/SCRIPT_CONFIRMED/WAIT_REVISION
- _try_create_generate_approval：自动建项目/实体解析/镜头解析/审批申请
- _resolve_regen_target：ast_/sht_ 或第 N 镜，不猜目标
- resolve_approval/execute_approval/resolve_review
- _open_script_review：持久化剧本确认卡片

### intent_router.py（437 行）
- apply_fast_path_rules：unsafe → regen → progress → pipeline → query
- classify_intent：Pydantic _IntentPayload + json_mode，阈值 0.7
- route_action：unsafe→REJECT、generate_request→GUIDE、pipeline_action→PIPELINE

### intent_resolver.py（199 行）
- resolve_intent：显式 ID > Focus Context > 澄清
- _clarify_message：确定性映射

### context_manager.py
- load_context/save_context/update_focus/update_operation/clear_on_project_switch/build_focus_hint
- FOCUS_MAX_AGE_SEQ=40

### parent_agent.py（100 行）
- create_task_from_intent / transition_task / complete_task / fail_task / cancel_task / retry_task
- TASK_INTENTS={generate_request,pipeline_action}

### task_orchestrator.py（568 行）
- create_task（agent_task_id 幂等）/start_task/add_stage/start_stage（依赖门禁）/finish_stage
- bridge_job_event/bridge_approval_resolved/reconcile_orphan_tasks
- task_payload（卡片数据）

### agent_tools.py（651 行）
- TOOL_REGISTRY：permission/risk_level/side_effect/idempotent/requires_approval/timeout/max_retries
- execute_tool：参数 Schema 校验 + 权限 + 审批门禁 + 审计

### review_confirmation.py（518 行）
- parse_confirmation_response / build_idempotency_key / create_script_review / resolve / expire_stale_pending / to_card
- PENDING_TTL=24h、BLOCKING_STATUSES

### approvals.py
- args_fingerprint / create_confirmation / find_approved / list_pending / resolve_confirmation / mark_task_finished

### progress.py（163 行）
- derive_progress（estimate→done 12 阶段）

## 3. 当前执行流程示例

### 剧本分析
用户发起「开始分析剧本」→ pipeline_action(analyze_script) → 创建 AgentTask → task_orchestrator.create_task → start_stage(parse) → worker 执行 Job → bridge_job_event → task.stage_succeeded → _auto_close_task_if_done → 回复任务卡片

### 角色图生成
用户发起「生成角色图」→ generate_request → _try_create_generate_approval → resolve_entity → approvals.create_confirmation → 回复 confirmation_id → 用户批准 → execute_approval → run_generate → execution_verifier 验证 Job → 回复执行结果

### 剧本确认多轮
用户「保存剧本」→ workflow_state → _open_script_review → 用户「不可以」→ _handle_pending_confirmation → reject → 不保存、不启动后续依赖

## 4. 数据流

- 用户消息：AgentMessage(role=user, seq 递增)
- 工具调用审计：AgentMessage(role=tool_event)
- 审批审计：AgentMessage(role=approval)
- 任务事件：TaskEvent(task_event.event_seq 全局递增)
- 任务卡片：TaskCard 组件读取 /api/tasks/{id}

## 5. 边界与前提交互

- 会话 archived 拒绝消息/审批/审核/执行
- 审批与执行分离：批准仅放行，执行须 execute
- 审核结论绑定 target_version，拒绝阻断下游
- 生成目标只认显式 ID，不猜名称