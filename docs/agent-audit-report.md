# Director-Lab 导演台 Agent 2.0 架构审计报告（Phase 0）

- 审计日期：2026-11-09
- 审计对象：/home/ubuntu/director-lab
- 审计性质：只读代码审计，零代码改动
- 审计范围：导演台前端页面、Agent API、意图识别、Prompt、Context Manager、Agent Orchestrator、父子 Agent、Tool Registry、任务执行系统、SSE、数据库模型、剧本分析流程

## 1. 总体结论

Director-Lab 已完成导演台 Agent 1.0 的基础建设：会话消息持久化、审批门禁、业务对象审核、统一任务编排、SSE 事件流、项目状态注入、意图路由、确认答复解析均已有确定性代码实现。但仍存在与 2.0 方案目标差距较大的部分：意图体系粒度不足、运行时任务规划缺失、父子 Agent 调度不完整、进度与结果聚合对用户不可见、前端仍缺少任务卡片与实时进度深度整合等。

## 2. 实际执行链路（已核实）

```text
POST /api/agent/sessions/{id}/chat
  → 持久化用户消息（seq 冲突重试 3 次）
  → _handle_pending_confirmation（确认答复优先解析，approve/reject/revise/defer/unclear）
  → _handle_video_workflow（剧本/项目创建向导式工作流，状态存 AgentSession.workflow_state）
  → 联网搜索分支（规则正则识别，SearchRun 状态机，真实搜索）
  → intent_router.classify_intent（规则快路径 + LLM 结构化识别 + route_action）
  → intent_resolver.resolve_intent（实体解析：显式 ID → Focus Context → 澄清）
  → 生成请求转审批（_try_create_generate_approval / request_generate）
  → parent_agent 任务创建 + task_orchestrator 统一 Task/TaskStage
  → 项目只读上下文注入（project_overview/asset_stats/list_jobs/list_assets，ToolBudget 硬预算）
  → LLM 生成回复（瞬态错误重试 3 次）
  → 助手消息持久化（payload 携带 intent/route_action/confidence/tool_budget/task_id/task_card）
```

## 3. 关键代码位置清单

| 模块 | 位置 | 职责 |
|---|---|---|
| Agent API 主入口 | backend/app/api/v1/agent.py (1884 行) | 会话/消息 CRUD、审批、审核、聊天主流程、视频工作流、确认答复解析 |
| 意图路由 | backend/app/services/intent_router.py (437 行) | 规则快路径、LLM 分类、NextAction 路由 |
| 意图解析 | backend/app/services/intent_resolver.py (199 行) | 显式 ID/Focus/澄清融合、标准化 ResolvedIntent |
| 上下文管理 | backend/app/services/context_manager.py | SessionContext、Focus Context、Operation Context、项目切换清理 |
| 父 Agent | backend/app/services/parent_agent.py (100 行) | AgentTask 创建与状态机 |
| 任务编排 | backend/app/services/task_orchestrator.py (568 行) | Task/TaskStage 状态机、事件 outbox、Job 桥接、审批桥接、恢复对账 |
| 工具注册表 | backend/app/services/agent_tools.py (651 行) | TOOL_REGISTRY、Pydantic Schema、权限/风险/幂等/审批门禁、execute_tool |
| 工具实现 | backend/app/services/agent_tool_impls.py (1749 行) | 24+ 工具的实际业务实现 |
| 上下文工具 | backend/app/services/agent_context_tools.py (300 行) | 意图驱动只读工具选择与上下文格式化 |
| 治理 | backend/app/services/agent_governance.py (133 行) | NextAction/TerminalStatus、ToolBudget、PolicyEngine |
| 会话记忆 | backend/app/services/session_memory.py (110 行) | 短期窗口 + LLM 压缩长期摘要 |
| 长期记忆 | backend/app/services/agent_memory.py (135 行) | project/preference/experience 记忆、superseded |
| 执行验证 | backend/app/services/execution_verifier.py | 异步 Job 真实验证、同步状态透传 |
| 确认管理 | backend/app/services/review_confirmation.py (518 行) | artifact_review 状态机、幂等键、TTL 过期、审计 |
| 审批门禁 | backend/app/services/approvals.py | tool_approval、入参指纹、审批与执行分离 |
| 任务模型 | backend/app/models/task.py (122 行) | Task、TaskStage、TaskEvent、TaskResult |
| Agent 模型 | backend/app/models/agent.py (214 行) | Session、Message、Task、PlanStep、Confirmation、Memory |
| 进度推导 | backend/app/services/progress.py (163 行) | derive_progress 12 阶段、产物判定 |
| 进度 API | backend/app/api/v1/progress.py (371 行) | 资产/关键帧/片段进度、快照 |
| 事件 API | backend/app/api/v1/events.py (164 行) | SSE、backfill、统一任务事件 |
| 前端 API | frontend/src/api/agent.ts | chat、sessions、approvals、reviews |
| 前端事件 | frontend/src/composables/useTaskEvents.ts | SSE 订阅、断线补齐 |
| 前端卡片 | frontend/src/components/TaskCard.vue | 状态/进度/阶段/结果渲染 |
| 导演台视图 | frontend/src/views/DirectorConsoleView.vue (1717 行) | 三栏布局、聊天、审批、审核卡片 |

## 4. 与 2.0 目标架构差距评估

| 2.0 模块 | 现状 | 评级 | 主要差距 |
|---|---|---|---|
| 意图理解层 | 规则快路径 + LLM 6 类粗粒度意图 + Resolver | 部分具备 | 无细粒度意图树；无统一结构化 IntentResult 输出到所有分支；置信度阈值 0.7 且无上下文重判流程 |
| Context Manager | SessionContext/Focus/Operation 已持久化，项目切换清理已实现 | 部分具备 | 对话上下文仅最近 40 条 + 摘要；无 pending_confirmation 显式字段；无 Execution Context 持久化 |
| Project State Manager | 存在 progress.derive_progress 与 generation_progress_snapshot | 部分具备 | 未作为独立服务层暴露给所有调用方，部分分支仍由 agent.py 内联查询 |
| Agent Planner | 无运行时规划节点 | 缺失 | 仅 create_task_from_intent 简单落库，无任务拆解/依赖分析/前置条件检查 |
| Agent Orchestrator | parent_agent + task_orchestrator 已有 Task/Stage | 部分具备 | 无子 Agent 调度、无 DAG 并行执行、无最大并发/超时/取消传播 |
| Execution Engine | 既有业务服务 | 已具备 | Agent 不重复实现业务，符合要求 |
| Task Manager + Event Manager | Task/TaskStage/TaskEvent outbox、Job 桥接、SSE、恢复对账 | 基本具备 | 进度百分比为阶段均值；无任务恢复的检查点机制（仅补偿闭环） |
| Result Aggregator + Validator | execution_verifier 校验 Job 真实存在；task_payload 返回 result | 部分具备 | 仅校验单个 Job 结果，无多子 Agent 聚合完整性校验 |
| Chat Response | 聊天消息 + 审批/审核卡片 + TaskCard | 部分具备 | 任务卡片已存在但默认展示有限；失败任务不展示已完成子任务列表与重试建议 |

## 5. 已核实的问题与证据

### 5.1 意图粒度不足
- 证据：intent_router.py 中 INTENT_CATEGORIES 仅为 query/analysis/generate_request/pipeline_action/ambiguous/unsafe，无 script.analyze、asset.regenerate 等细分。
- 影响：用户说『重新生成第二个角色图片』时，若未命中返工规则，LLM 只能归为 generate_request 或 pipeline_action，目标解析依赖严格 resolve_entity，无 ID 时只能澄清。

### 5.2 任务规划与子 Agent 调度缺失
- 证据：parent_agent.py 仅创建 AgentTask 并维护状态，无 AgentPlanStep 运行时、无子任务创建/调度；task_orchestrator.py 只有 Stage，无多 Agent 并行。
- 影响：方案所述『角色分析/场景分析/道具分析并行，分镜分析依赖前序』无法实现；结果聚合可能只取一条路径。

### 5.3 上下文关联缺陷（已修复项确认）
- 证据：agent.py 中 _handle_pending_confirmation 已在 chat 主流程最前处理，『不可以』能正确关联待确认操作；workflow_state 使多轮向导可持续。
- 残留问题：ChatRequest 仅接 content，无 session 级显式 pending_confirmation 对象；多个待确认事项时 _mentions_pending_target 依赖语义模糊匹配，没有结构化目标选择。

### 5.4 项目状态感知部分实现
- 证据：progress.py derive_progress 已覆盖 estimate→done 12 阶段，agent.py 主流程注入 project_overview/asset_stats/list_jobs/list_assets；但资产审核状态仅通过 progress 快照获取，未统一封装为 ProjectContext。

### 5.5 进度反馈不完整
- 证据：task_orchestrator 的 TaskStage 有 progress，但为阶段均值；前端 TaskCard 显示进度条与当前阶段，但 SSE 事件仅到 Task 级，子阶段细粒度事件较少。

### 5.6 结果聚合验证缺失
- 证据：execution_verifier 只对单个 Job 验证；agent.py 剧本分析流程未聚合多分析器结果并校验数量完整性。

### 5.7 SSE 断线恢复存在但前端未完全对接
- 证据：events.py 提供 /api/tasks/events 与 backfill；useTaskEvents.ts 已实现 last_seq 断线补齐；但 DirectorConsoleView.vue 中对 SSE 的使用范围有限，未展示子 Agent 执行过程。

## 6. 复用资产清单

- 既有 Tool Registry（agent_tools.py）可直接扩展细粒度工具，无需新建。
- Task/TaskStage/TaskEvent/TaskResult 四表已落库，可直接承载 DAG 调度。
- review_confirmation 的幂等键/审计/TTL 机制可复用于所有人工确认。
- execution_verifier 可扩展为多结果聚合验证器。
- job 通道 + SSE outbox 可作为子 Agent 事件底座。

## 7. 下阶段建议

按方案 Phase 1 优先实施：意图分类细化 + 统一结构化意图输出 + Context Manager 增强（execution context、pending confirmation 显式化）+ 默认兜底三出口整改；随后 Phase 2 实现 Planner 与 DAG 调度。