# Director-Lab Agent Runtime 审计报告

> 审计日期: 2026-11-25  
> 审计范围: Phase 0 全模块代码审计  
> 审计原则: 以代码为依据，不依据文件名、注释或历史设计文档  

---

## 审计模块概览

| 模块 | 文件位置 | 行数 | 当前实现 | 已有复用能力 | 问题 | 改造建议 |
|---|---|---|---|---|---|---|
| **Agent API** | `app/api/v1/agent.py` | 2122 | FastAPI Router: 会话CRUD/消息管理/审批裁决与执行/业务审核/阶段指引/生成申请创建/LLM对话流(SSE) | 会话-消息-任务-审批全链路已打通 | Agent编排逻辑耦合在API层(~1500行)未分离Runtime; 聊天路由内嵌pipeline_tools集成; SSE聊天流内直接构造系统提示词与工具定义 | 保留API路由层,编排逻辑拆入Agent Runtime; 系统提示词/工具清单由Runtime统一管理 |
| **Intent Router** | `app/services/intent_router.py` | 632 | 规则快路径+LLM结构化识别+路由策略(CHAT/REJECT/GUIDE/PIPELINE/CLARIFY) | 规则+LLM混合; 置信度分级; 细粒度子类; 导航关键词动态正则 | route_action仅输出枚举不做任务编排; 置信度与执行授权混用; generate_request→GUIDE而非传入Planner | 保留快速识别降低流程控制权; generate_request结果传入Planner非直接GUIDE; 置信度继续作为Plan Validation输入 |
| **Intent Resolver** | `app/services/intent_resolver.py` | 265 | 融合Intent+显式ID+焦点上下文→ResolvedIntent; 实体验证; 澄清消息映射 | 实体解析链路完整(显式ID→焦点回退→澄清); focus验证; clarification_code确定性映射 | 无project context snapshot/依赖版本校验; clarification仅基于实体缺失; 无Goal持续性追踪 | 强化为Context Resolver一部分: 增加项目版本/依赖/审核状态解析; 与Goal Manager联动 |
| **Parent Agent** | `app/services/parent_agent.py` | 213 | AgentTask创建/状态机流转/结果聚合; 状态转换门禁; 结果聚合 | 状态机门禁已实现; AgentTask与统一Task层双向关联; 结果聚合 | 无Plan/Goal概念; 任务创建后无后续调度; 无Replan/Verification闭环 | 改造为Agent Runtime/Orchestrator: 增加Goal Manager/Planner/Executor/Verifier/Replanner |
| **Tool Registry** | `app/services/agent_tools.py` | ~800 | TOOL_REGISTRY注册29个工具; 统一Input Schema; 前置条件; 权限/风险/副作用/幂等元数据; execute_tool执行网关 | 工具注册规范完整; 审批→校验→前置→执行四段网关; 意图→工具映射 | 全部一次性注册无动态筛选; 生成类request/run双步骤耦合; ToolSpec缺output_schema/retry/verification | 动态筛选(按阶段/权限/依赖); 补充output_schema/retry/verification; 工具执行统一经Executor调度 |
| **Tool Impls** | `app/services/agent_tool_impls.py` | 1771 | 29个工具入参Schema+业务实现 | 全部真实DB查询; 角色/场景/道具按项目过滤; 记忆写入截断/白名单保护; project_id服务端注入 | 分离良好 | 保持现有分离; 补充工具级Retry/Timeout/Verification; 执行日志结构化 |
| **Governance** | `app/services/agent_governance.py` | 133 | NextAction/TerminalStatus/PolicyDecision枚举; 预算控制; 策略引擎框架 | 动作枚举覆盖全; 导演台专用动作 | 框架定义未在主流程全面应用; 预算控制待接入; 决策审计未持久化 | 全面接入Runtime执行循环; PolicyDecision作为前置门禁; 审计日志结构化落库 |
| **Task Orchestrator** | `app/services/task_orchestrator.py` | ~500 | 统一Task层: 创建/阶段/DAG调度/事件桥接/审批桥接/对账恢复 | 状态机+DAG调度(schedule_ready_stages); Job→Stage事件桥接; 孤儿任务对账; MAX_CONCURRENT=8 | DAG调度仅用于解析任务未接入Agent Plan; 无动态Replan; 无Plan Revision历史 | 升级为持久化计划与任务执行管理: Plan Step映射到TaskStage; 增加Plan Revision+Replan触发 |
| **AgentTask模型** | `app/models/agent.py` | 217 | AgentTask: intent/goal/risk/confidence/success_criteria/status/result_summary | 状态机门禁(parent_agent); 与AgentConfirmation 1:1 | 缺goal_id/plan_id/lifecycle_state/progress_snapshot | 增加goal_id/plan_id/current_step_id/lifecycle_state |
| **AgentPlanStep** | `app/models/agent.py` | ~80 | step_index/step_type/tool_name/args/status/result | 已有基础字段 | task_id指向agent_task非plan; 缺dependencies/preconditions/verification/retry_count | 重构为Plan→Step二层; 补充依赖/前置/验证/重试字段 |
| **Job** | `app/models/production.py` | 327 | job_type/status/params/result/error_code; JobEvent outbox | 完整异步生产生命周期; 全局递增seq; Worker调度集成 | Job与AgentTask无直接关联(TaskStage间接桥接); Job完成→Agent恢复依赖bridge轮询 | 保留现有; 增强Job→Agent事件通道; 补充agent_task_id可选直连 |
| **Pipeline State** | `app/services/pipeline_state.py` | 275 | 7节点流程链; 操作→节点映射; 剧本审核门禁 | 工具级门禁; derive_progress权威判定; 剧本审核阻断 | 资产/分镜审核闸口已移除(2026-11); 审核版本绑定仅用于script_review | 保留并强化门禁; 按Plan恢复资产/分镜审核; 增加审核版本绑定与失效 |
| **Approvals** | `app/services/approvals.py` | 269 | AgentConfirmation创建/裁决/执行分离; 入参指纹去重; 审计消息; 任务编排桥接 | 审批-执行分离; 入参幂等; 裁决同步写消息+事件 | 审批仅针对生成操作; 不存目标版本; 无partially_completed | 强化版本绑定; 审核失效机制; 增加partially_completed状态 |
| **Memory** | `agent_memory.py`+`session_memory.py` | 140+111 | AgentMemory(project/preference/experience); CRUD+supersede+上下文注入; SessionMemory(短期窗口+LLM摘要) | 分层记忆; 双上限; supersede保留历史; 会话摘要触发阈值 | 记忆注入仅用于系统提示词未接入Runtime; 缺语义检索; 经验无验证 | 建立分层检索; Memory→Context Resolver; Experience Memory验证 |
| **Worker** | `executor.py`+`scheduler.py`+`compose_executor.py`+`parse_executor.py` | 2611+515+529+374 | 生成/解析/合成/导出执行器; ComfyUI池化; 启动恢复; 对账 | 完整异步执行; 参数注入/上传/轮询/下载/落库全链路; 启动恢复 | Worker与Agent无直接事件通知; Job完成→Agent恢复有轮询延迟 | 保留现有; 增加Agent任务关联; 补充持久化事件通道 |
| **Event Stream** | `app/services/event_stream.py` | 122 | SSE outbox增量轮询; asyncio.to_thread隔离; 节流日志; 连接池保护 | 通用轮询框架; 连接池安全; 指数退避 | 仅轮询无持久化事件; 不保证可靠投递; 无重放 | 统一任务进度状态更新: 保留轮询; 补充AgentEvent表; 支持seq增量重放 |
| **Frontend Agent** | `frontend/src/components/director/` | N/A | Vue3组件: SessionRail/ChatComposer/MessageStream/ApprovalList/ReviewTimeline/ContextSidebar | 会话管理/消息流/审批面板/审核时间线完整 | 无Plan/Goal展示; 无Step进度/DAG; 无Replan; 历史任务恢复入口待增强 | 升级为Agent Workbench: Plan展示/Step进度/Job状态/Replan/产物/任务恢复/暂停取消/历史 |
| **数据库模型** | `app/models/*.py` | ~900 | 15+模型文件覆盖全业务流程 | 模型覆盖完整 | AgentGoal/AgentPlan表缺失; AgentTask与Plan无关联; 缺ExecutionLog/Verification/Event表 | 补充AgentGoal/AgentPlan/AgentPlanStep/AgentExecutionLog/AgentEvent/AgentVerification; Migration兼容 |
| **测试** | `backend/tests/` | 40+文件 | agent_sessions/tasks/tools/intent/memory/governance/verifier/gate/context | 核心模块覆盖 | 无Planner/Replanner单元测试; 端到端场景测试待扩展 | 按Plan§21补充单元测试+20个场景集成测试 |

---

## 现有架构执行链

```
用户消息 → Agent API (agent.py)
    ├── 1. Intent Router → 规则快路径 或 LLM识别 → route_action
    ├── 2. Intent Resolver → 显式ID/焦点 → ResolvedIntent
    ├── 3. Parent Agent → create_task_from_intent → AgentTask(pending)
    ├── 4. Approvals → create_confirmation → AgentConfirmation(pending)
    ├── 5. Agent API 对话流 (SSE)
    │   ├── 构建系统提示词+工具定义
    │   ├── LLM 调用
    │   └── 工具调用 → execute_tool → ToolResult
    └── 6. 审批执行 (用户确认)
        ├── resolve_confirmation → approve/reject
        └── execute_approval → pipeline_tools/agent_tools.run_generate
                ↓
        Task Orchestrator → Task+TaskStage
                ↓
        Worker → ComfyUI → Job
                ↓
        bridge_job_event → TaskStage更新 → SSE推送
```

**关键断点**:
- AgentTask创建后与Job执行无规划层关联
- 无Goal持续性追踪——每次消息独立解析意图
- 无Plan层——无法动态规划多步骤/跳过已完成/Replan
- 无统一Human Gate——审核仅限生成审批,资产/分镜审核已移除
- 无Verifier——仅验证Job存在,不做产物/业务/质量验证
- 无Replanner——失败仅Worker内置retry,不做计划级调整

---

## 复用评估

| 现有能力 | 复用程度 | 说明 |
|---|---|---|
| AgentSession/AgentMessage | 完全复用 | 会话消息持久化成熟,seq递增+唯一索引防并发 |
| Intent Router+Resolver | 改造复用 | 保留识别,降低流程控制权,作为Goal Manager输入 |
| AgentTask状态机 | 改造复用 | 增加goal_id/plan_id/lifecycle_state,扩展Goal→Plan→Step三层 |
| AgentConfirmation审批 | 改造复用 | 审批-执行分离成熟,增加版本绑定+Human Gate门禁 |
| Task Orchestrator | 改造复用 | DAG调度+事件桥接成熟,作为Plan Step执行层 |
| Job/Worker | 完全复用 | 生产任务层不变,仅增Agent事件通知 |
| Tool Registry | 改造复用 | 注册规范完整,补充output_schema/retry/verification,按阶段动态筛选 |
| Pipeline State | 改造复用 | 7节点流程链保留,补充资产/分镜审核门禁 |
| AgentMemory/SessionMemory | 改造复用 | 分层记忆成熟,增Retrieval检索+Context Resolver集成 |
| Event Stream(SSE) | 改造复用 | 轮询框架保留,补充持久化AgentEvent表 |
| Frontend Director | 改造扩建 | 组件化良好,增Plan/Step/Replan/Human Gate展示 |

---

## 高优先级发现

1. **P0-1 数据模型缺口**: AgentGoal/AgentPlan表缺失,AgentTask无goal_id/plan_id关联
2. **P0-2 Pipeline审核移除**: 资产/分镜图审核闸口已取消(2026-11),Plan§11要求恢复
3. **P0-3 无Agent Loop持久化**: 执行依赖单个SSE请求,页面刷新/服务重启后无法自动恢复执行
4. **P0-4 无Verifier与Replanner**: 失败仅Worker内置retry,不做计划级调整
5. **P0-5 Agent编排逻辑耦合API层**: agent.py~1500行编排代码需拆入Runtime

---

## 建议实施顺序

| 优先级 | 阶段 | 行动 |
|------|------|------|
| Phase 1 | Agent Runtime基础 | Goal Manager/Context Resolver/Constraint Manager/Agent Loop持久化 |
| Phase 1 | 数据模型 | 补充AgentGoal/AgentPlan/AgentPlanStep/AgentExecutionLog/AgentEvent |
| Phase 2 | Planner/Executor/Verifier/Replanner | 动态计划+执行+验证+恢复闭环 |
| Phase 3 | Tool Registry | 统一ToolSpec/动态筛选/补充retry/verification元数据 |
| Phase 4 | Human Gate | 恢复资产/分镜审核门禁+版本绑定 |
| Phase 5 | Frontend Agent Workbench | Plan/Step/Replan/Job展示 |
| Phase 6 | Memory/评估 | 分层记忆检索/成本控制/评估优化 |

---

## 2026-11 Phase A 修复记录

每个 P0 缺陷一条命名回归用例，集中在 `backend/tests/test_agent_runtime_regression.py`。

| 缺陷编号 | 修复提交 | 回归用例 |
|---|---|---|
| P0-1 | `5a54196` A1 | `test_p0_1_persisted`、`test_loop_persists_across_sessions` |
| P0-2 | `654a785` A2 | `test_p0_2_no_request_session_in_thread` |
| P0-3 | `654a785` A2 | `test_p0_3_sse_events_delivered` |
| P0-4 | `2223d14` A3 | `test_p0_4_progress_events` |
| P0-5 | `4c039a4` A4 | `test_p0_5_gateway_only`、`test_p0_5_no_direct_tool_fn_access` |
| P0-6 | `a6b2348` A5 | `test_p0_6_job_waits` |
| P0-7 | `cace2f0` A6 | `test_p0_7_failed_not_verified` |
| P0-8 | `e8c2f38` A7 | `test_p0_8_approval_per_step` |
| P0-9 | Phase B | `test_p0_9_resume_has_caller`（xfail，Phase B 移除标记） |
| P0-10 | `8131b61` A8 | `test_p0_10_new_intent_new_goal` |
| P0-11 | `ca0fe1c` A9 | `test_p0_11_replan_revision` |
| P0-12 | `5e335b1` A10 | `test_p0_12_status_by_goal_id` |
| P0-13 | `0eaac87` A00 / `f1bb505` A00b | `test_p0_13_no_real_db` |

门禁：`pytest -q tests/test_agent_runtime_*.py tests/test_agent_loop_api.py` 101 passed / 1 xfailed；
全量 `pytest -q` 1560 passed / 1 xfailed / 0 failed（Phase A 前基线 1445 passed / 1 failed）。
`backend/data/director_lab.db` 在测试前后保持只读核对一致（alembic_version / schema 哈希 / 行数）。
