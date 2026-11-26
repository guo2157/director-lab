# Director-Lab Agent 2.0 当前代码审计报告
- 审计日期：2026-11-25
- 审计性质：只读审计 + 测试基线建立（Phase 0），本轮仅新增本文档，未修改任何业务代码
- 审计对象：`/home/ubuntu/director-lab`（release 分支，HEAD `2d0daf2` test(prompt): P3-5 视频首镜/续镜模板同步演进守护）
- 证据来源：代码实证 + 内存库行为实证 + 后端 169 项 Agent 相关测试 + 前端构建
- 说明：历史 Agent 审计文档（2026-10-27 ~ 2026-11-21）已逐项对照当前代码核实，状态见第 10 节

## 0. 工作区保护记录
| 项 | 值 |
| --- | --- |
| 分支 | release |
| HEAD | 2d0daf2599dc8b28fc068f30e9b62c9a8c76d7c2 |
| 未提交修改 | backend/app/services/prompt_qc.py、backend/app/worker/executor.py、frontend/src/components/AppLayout.vue、frontend/src/components/DirectorAgentPanel.vue、scripts/migrate_storyboard_files.py |
| 未跟踪文件 | backend/tests/test_prompt_qc_positions.py、scripts/repair_missing_thumbs.py |
| Alembic head | a7c3e9f5b1d8 (head) |
| 说明 | 未提交修改中 AppLayout.vue / DirectorAgentPanel.vue 为本次重构启动前刚完成的『顶栏恢复导演台入口 + 移除固定 AI 助手浮钮』改动，其余与 Agent 重构无关；审计全程未 reset/clean/覆盖任何用户文件 |

## 1. 当前真实调用链图
```mermaid
flowchart TD
  A[DirectorConsoleView.vue<br/>本地 ref 状态, 未走 Pinia] -->|api/agent.ts| C[POST /api/agent/sessions/{id}/chat]
  A2[DirectorAgentPanel.vue<br/>useAgentStore Pinia] -->|同一 api/agent.ts| C
  C --> D1[1. 持久化 user 消息<br/>seq 冲突重试 3 次]
  D1 --> D2{1.30 待确认裁决<br/>_handle_pending_confirmation}
  D2 -->|命中| R1[早返回: approval 裁决回复]
  D2 -->|未命中| D3{1.35 视频工作流<br/>_handle_video_workflow}
  D3 -->|命中| R2[早返回: video_workflow]
  D3 -->|未命中| D4{1.3.5 联网搜索正则<br/>SearchRun 状态机}
  D4 -->|命中| SE[search_service.run_web_search<br/>web_fetch SSRF 防护] --> R3[早返回: web_search]
  D4 -->|未命中| E1[classify_intent<br/>规则快路径+LLM json_mode]
  E1 --> E2[resolve_intent<br/>显式ID>Focus>澄清]
  E2 --> E3[context_manager.update_execution]
  E3 --> E4[parent_agent.create_task_from_intent<br/>仅 generate_request/pipeline_action 建 AgentTask]
  E4 --> E5[task_orchestrator.create_task<br/>同步建统一 Task]
  E5 --> F{route_action}
  F -->|REJECT/GUIDE/CLARIFY/PIPELINE| R4[早返回: 固定文案/审批申请/流程工具/澄清]
  F -->|CHAT/回落| G1[项目上下文注入<br/>base_tools 3 个 + 条件 list_assets<br/>+ build_read_tool_calls]
  G1 --> G2[ToolBudget 预算控制<br/>每次调用落 tool_event 审计消息]
  G2 --> G3[历史窗口 40 条 user/assistant]
  G3 --> G4[session_memory 摘要注入<br/>+ agent_memory 记忆注入]
  G4 --> G5[LLMClient.chat_messages<br/>瞬态错误指数退避重试 3 次]
  G5 --> G6[持久化 assistant 消息<br/>complete AgentTask/Task]
  G6 --> H[同步 HTTP 响应双消息]
  I[审批执行 POST /approvals/{id}/execute] --> J[execute_tool approved=True<br/>run_generate/run_style_update]
  J --> K[Job 创建 → Worker/Scheduler<br/>→ ComfyUI 实例池]
  K --> L[job_event outbox → SSE /api/events]
  E5 -.-> M[task_event outbox → SSE /api/tasks/events]
```
关键事实：/chat 为**单轮同步请求-响应**，LLM 超时 120s 内前端无中间反馈；无 Planner、无 Agent Loop、无后台 Agent Runner。

## 2. 模块已实现能力与实际调用位置
| 模块 | 已实现能力（代码实证） | 实际调用位置 |
| --- | --- | --- |
| api/v1/agent.py (2126 行) | 会话/消息 CRUD、审批 resolve/execute、审核 resolve、/chat 11 步主流程、视频工作流、搜索分支、导航事件 | 前端两入口唯一 Agent API |
| intent_router.py (632 行) | 6 类粗意图 + subcategory、规则快路径、LLM json_mode、置信度 0.7 阈值 + 三档（execute/recheck/clarify）、NextAction 枚举路由、澄清文案确定性拼接 | agent.py chat() 1.4 节 |
| intent_resolver.py (265 行) | ResolvedIntent 融合意图与结构化上下文；实体解析 显式ID>焦点>澄清；数据库验证实体归属 | agent.py chat() E2 |
| context_manager.py (355 行) | SessionContext（focus/operation/execution/pending_confirmation_id）JSON 落 context_state；validate_focus 数据库实时验证；clear_on_project_switch 清理结构化上下文 | agent.py 项目切换（171 行）与 chat() |
| parent_agent.py (210 行) | AgentTask 创建（仅 TASK_INTENTS）、状态机 5 态流转、aggregate_task_results 结果聚合（TaskResult result_type=aggregate 幂等落库） | agent.py chat()、approvals.py |
| agent_governance.py (133 行) | NextAction/TerminalStatus/PolicyDecision 枚举、ToolBudget（12 次/30s/24000 字符）、evaluate_tool_policy | agent.py、agent_tools.execute_tool |
| agent_tools.py (729 行) | TOOL_REGISTRY 28 工具、Pydantic input_model 校验、preconditions 运行时检查（4 个 precheck 函数）、execute_tool 审批门禁与计时、get_allowed_tools 意图-工具映射 | agent.py、approvals.py |
| agent_tool_impls.py (1771 行) | 全部 28 个 tool_* 实现 + Input Schema | TOOL_REGISTRY fn 引用 |
| agent_context_tools.py (300 行) | build_read_tool_calls 按消息内容动态选只读工具、format_tool_context | agent.py G1 |
| task_orchestrator.py (663 行) | 统一 Task/Stage 编排、DAG depends_on 门禁、bridge_job_event、进度聚合、reconcile_orphan_tasks、取消传播 | agent.py、approvals.py、worker |
| execution_verifier.py (219 行) | verify_job_submission、verify_execution_result、verify_aggregate_completeness、verify_stage_dependencies | approvals.py execute 路径（审计确认未被 /chat 主对话调用） |
| session_memory.py (111 行) | 摘要压缩（阈值 10 条未摘要消息触发）、summary_upto_seq 检查点、LLM 失败保留旧摘要 | agent.py 2.5 |
| agent_memory.py (135 行) | 三类记忆（project/preference/experience）、supersede 替代链、注入上限 20 条/3000 字符 | agent.py 2.6、list_memories/add_memory/supersede_memory 工具 |
| approvals.py (259 行) | 审批创建/批准/执行、执行时直接置 AgentTask completed（257 行）、重复消费防护 | agent.py approvals 路由 |
| review_confirmation.py (518 行) | 作品审核（剧本/资产/关键帧/片段四轮次）、幂等键、pending 提示构建 | agent.py 1.30、pipeline_action |
| event_stream.py (122 行) | SSE 轮询框架：Session 在 yield 前关闭、anyio.to_thread 跑阻塞查询、心跳、MAX_BATCH | events.py 两组 SSE |
| pipeline_state.py (275 行) | 流程阶段状态机（制作链路闸口） | pipeline 工具与 agent._handle_pipeline_action |
| pipeline_tools.py (732 行) | 创建项目/保存剧本/解析/生成/合成/导出等流程工具 | agent.py、pipeline API |
| integrations/llm.py (654 行) | chat/chat_messages/chat_stream/chat_json、LLMResponseMeta、usage 提取、截断 JSON 检测 | agent.py（仅 chat_messages）、script_agent、job_cancel、script_evaluation |
| search_service.py + web_fetch.py | SearchRun 状态机、结果验证、SSRF 防护（localhost/私网/链路本地/云元数据拦截、http(s) 白名单、follow_redirects） | agent.py 1.3.5 |

## 3. AgentTask / AgentPlanStep / Task / TaskStage / Job 关系实证
```
AgentMessage(user) ──1:1── AgentTask(agt_)          ← 仅 generate_request/pipeline_action 创建
                              │ 1:N（模型层定义）
                              ▼
                        AgentPlanStep(aps_)          ← 【实证：运行时从不写入，仅建表】
AgentTask ──1:0..1── Task(task_, Task.agent_task_id) ← chat() 同步创建，幂等按 agent_task_id 复用
Task ──1:N── TaskStage ──0..1── Job(job_)           ← 业务执行层，Worker/Scheduler 驱动
Task ──1:N── TaskEvent(outbox, event_seq 全局递增)
Job  ──1:N── JobEvent(outbox, event_seq 全局递增)
```
**状态写入双轨实证（P0-2 风险确认）：**
- AgentTask 状态由 parent_agent 状态机管理（pending/running/completed/failed/cancelled）。
- 但 `approvals.py:257` 在审批执行成功后直接 `task.status = "completed"`（AgentTask），绕过 parent_agent 状态机；且 /chat 中 GUIDE 分支在**审批申请提交成功**时即 `parent_agent.complete_task(task, "已创建生成审批申请，等待人工审批")`——AgentTask 的 completed 语义实际是『申请已提交』，与业务产物完成无关，但字段名与聚合逻辑没有区分。
- AgentPlanStep **全库检索仅 models 与 project_purge 引用**，无任何运行时写入——第二阶段规划的『计划步骤持久化』从未落地。

## 4. Tool Registry 全量清单（28 个，按类型分类）
只读（23 个，全部 risk=low、requires_approval=False）：
get_shot_prompt、script_normalize¹、script_evaluate¹、project_overview、list_jobs、asset_stats、list_assets、list_asset_plans、get_style_config、get_asset_generation_progress、get_keyframe_generation_progress、get_clip_generation_progress、get_job、list_project_entities、list_project_shots、get_parse_status、list_compositions、list_exports、list_memories、list_asset_versions、get_shot_impact

写（5 个）：
| 工具 | permission | risk | requires_approval | preconditions |
| --- | --- | --- | --- | --- |
| batch_request_generate | write | medium | False（只建审批不执行） | project+script_parsed |
| request_style_update | write | medium | False（只建审批） | 有 |
| run_style_update | write | high | **True** | 无 |
| add_memory | write | low | False | 无 |
| supersede_memory | write | low | False | 无 |

生成（2 个）：
| 工具 | permission | risk | requires_approval | preconditions |
| --- | --- | --- | --- | --- |
| request_generate | generate | medium | False（只建审批） | project+script_parsed |
| run_generate | generate | high | **True** | 无 |

¹ script_normalize/script_evaluate 的 timeout_seconds=300（LLM 类只读工具）。

## 5. /chat 特殊分支与早返回路径（全部实证）
| 序号 | 分支 | 触发条件 | 返回数据 | 落库 |
| --- | --- | --- | --- | --- |
| 0 | 归档拒绝 | session.status=archived | 409 session_archived | 无 |
| 1.30 | 待确认裁决 | 有 pending 且非导航请求 | assistant(role 含 approval 审计) | user + approval 审计 + assistant |
| 1.35 | 视频工作流 | _handle_video_workflow 命中 | route_action=video_workflow | user + assistant |
| 1.3.5 | 联网搜索 | 正则命中（前 200 字符） | route_action=web_search + SearchRun 记录 | user + assistant |
| 1.4 | REJECT | unsafe 意图 | UNSAFE_REJECT_MESSAGE | user + assistant（payload 全量意图审计） |
| 1.4 | GUIDE | generate_request 且定位到目标 | 审批申请文案 + approval payload；定位失败回落主对话 | user + assistant；成功时 AgentTask/Task 双双 complete |
| 1.4 | PIPELINE | pipeline_action | 流程工具结果；navigate_workspace 拆 nav_request | user + assistant + workspace 事件 outbox |
| 1.4 | CLARIFY | ambiguous/需澄清 | 确定性澄清问题 | user + assistant；AgentTask/Task cancel |
| 主路径 | CHAT | 其余 | LLM 回复 + 项目上下文注入（仅关联项目时） | user + N×tool_event + assistant；complete AgentTask/Task |

**GUIDE 分支回落缺陷（P1）**：`action = None` 回落主对话后，此前已创建的 AgentTask/unified Task 在主路径结束时仍会被 complete_task——语义上等于『澄清后闲聊也会完成任务』，需 Phase 1 核实修正。

## 6. SSE / backfill 接口、事件模型与前端订阅者
| 接口 | 事件源 | 前端订阅者 |
| --- | --- | --- |
| GET /api/events + /events/backfill | JobEvent outbox（event_seq 全局递增） | workspace 事件流（AppLayout startWorkspaceEvents / useTaskEvents 间接） |
| GET /api/tasks/events + /tasks/events/backfill | TaskEvent outbox | useTaskEvents（DirectorConsoleView 注册卡片；DirectorAgentPanel 未直接使用） |
| workspace 事件 | workspace_event outbox（导航等） | AppLayout 全局订阅 |

**缺口**：无 AgentRunEvent——纯聊天 Agent Run 无独立事件通道；/chat 全程同步，LLM 流式输出（chat_stream）未被任何 Agent API 调用（FLOW-001 实证：chat_stream 仅 script_agent/job_cancel/script_evaluation 使用）。

## 7. Memory 过滤、摘要压缩、项目切换行为（P0-1 实证）
**跨项目记忆泄漏——实证缺陷**：
```
内存库实证（2026-11-25）：
list_memories(project_id=None) → 3 条：项目A设定(prj_A)、项目B设定(prj_B)、用户喜欢竖屏(None)
list_memories(project_id='prj_A') → 2 条：项目A设定 + 全局偏好（正确）
```
`agent_memory.list_memories` 在 project_id=None 时**不添加任何项目过滤条件**，build_memory_context(db, None) 会把全库所有项目记忆注入 Global 会话 → P0-1 验收项『Global Session 无法读取 Project A 的记忆』**当前不成立**。

**项目切换行为**：clear_on_project_switch 仅清空 focus/operation/execution/pending_confirmation_id；**session.summary 与历史消息未做任何项目作用域标记**——切换到项目 B 后，摘要中项目 A 的事实（故事名/风格/进度）继续注入 B 的执行上下文（2.5 节摘要注入无项目过滤）。

**无项目自动建项目——实证违规**：`_try_create_generate_approval` 在未关联项目时自动创建项目：消息正则提取名称失败时**默认『新项目』**、默认画幅 9:16、默认 novel 类型，创建后直接改写 session.project_id 并 commit——违反 P0-1『禁止默认创建「新项目」』『只有用户明确表达创建意图才进入 create_project』两条规则。

## 8. 两个聊天入口状态共享实证
| 维度 | DirectorConsoleView.vue | DirectorAgentPanel.vue | 共享？ |
| --- | --- | --- | --- |
| 会话/消息状态 | **本地 ref**（sessions/messages/sending 均组件内） | useAgentStore（Pinia） | **否** |
| API 层 | api/agent.ts | api/agent.ts | 是 |
| 任务事件 | useTaskEvents（注册卡片、SSE+backfill） | 未直接使用（依赖 agent store 轮询） | 部分 |
| 假进度 | **THINKING_STAGES + setInterval 轮换『正在理解你的意图…/正在查询项目状态…/正在组织回复…』** | 无 | — |
结论：两入口 API 同源但状态层独立，刷新/切换后状态不一致风险存在；ConsoleView 的假进度违反方案 8.2『不要用前端 setInterval 冒充真实进度』。

## 9. 测试基线
| 项 | 结果 |
| --- | --- |
| Agent 相关测试（10 文件：e2e_flow/generate_approval/governance/intent/memory/sessions/session_title/tasks/tools/context_and_resolver） | **169 passed, 0 failed, 44.94s**（backend/.venv，Python 3.12） |
| 前端 npm run build（vue-tsc + vite） | **通过，9.80s** |
| 历史全量基线（2026-11-17 记录） | 699 collected / 690 passed / 9 failed（test_agent_sessions 2 个 FakeLLM 桩缺参 + 7 个失效传播断言）——本次未复跑全量（含真实 LLM 网关依赖，8 个测试各需 30-60s） |
| 外部依赖阻塞 | ComfyUI 实例可用性未在本次验证；真实媒体链路按既有基线标记为环境受阻 |

## 10. 历史 Agent 计划状态对照
| 历史文档结论 | 当前代码实证状态 |
| --- | --- |
| 差距 1 工具调用零审计（2026-10-27 A 档已消除） | **已实现**：tool_event 审计消息随调用落库，LLM 失败不丢审计 |
| 差距 2 注册表 Schema 化（B 档已消除） | **已实现**：28 工具全部 Pydantic input_model + 元数据 |
| 差距 3 意图识别（B 档已消除） | **已实现**：结构化 IntentResult + NextAction 枚举 |
| 差距 4 结果验证器（部分缓解） | **部分实现**：execution_verifier 存在且被 approvals 执行路径使用；/chat 主对话无运行时验证；verify_aggregate_completeness 存在 |
| 差距 5 记忆层（C 档未启动） | **部分实现**（2026-11 迭代一）：三类记忆 + supersede + 注入上限；但存在 P0-1 隔离缺陷 |
| AgentTask/AgentPlanStep 表已就位、运行时未实施（2026-11-03） | **AgentTask 已运行**（仅执行类意图）；**AgentPlanStep 仍纯建表未运行** |
| TaskCard.vue 模板异常（历史问题清单） | **仍存在**：第 112 行 `<a` 标签内孤立字符 `n`（构建通过，浏览器容错渲染，但属代码缺陷） |
| FLOW-001 流式输出 | **未实现**：/chat 同步阻塞，chat_stream 未接入 Agent API |
| SEC-001/SEC-002 无鉴权公网可达 | 本次未复核网络面；代码面仍无鉴权依赖项 |
| Phase 1-4 改进计划（agent-improvement-plan.md，2026-11-09） | Phase 1 意图细粒度/置信度分级**已大部分落地**；Phase 2 DAG scheduler（TaskStage.depends_on/agent_name 已存在）；Phase 3 前端进度/恢复部分落地；Phase 4 评估集目录 tests/evals 已存在但未在本次核实内容 |

## 11. P0/P1/P2 问题清单（本次实证）
### P0
1. **跨项目记忆泄漏**：list_memories(project_id=None) 无项目过滤，Global 会话注入全库项目记忆（实证第 7 节）。修复：project_id=None 时仅注入 project_id IS NULL 的记忆。
2. **项目切换摘要/历史污染**：clear_on_project_switch 不清摘要，历史消息无项目作用域标记，A 项目事实注入 B 执行上下文。修复：摘要按 scope 分段或不注入旧项目摘要；消息增加 project scope/epoch。
3. **无项目自动建项目**：_try_create_generate_approval 默认名『新项目』+ 默认画幅自动创建并绑定（实证第 7 节）。修复：移除自动创建路径，缺名称先澄清。
4. **客户端可伪造内部审计消息**：append_message 的 role 校验接受 user/assistant/system/tool_event/approval 全部角色（schemas/agent.py:36-46）。修复：外部接口仅允许 user + 受控 attachment_id。
5. **审批申请提交成功即标 AgentTask completed**：/chat GUIDE 分支 complete_task『已创建生成审批申请』；approvals.py:257 执行后直接置 completed，绕过状态机。修复：区分 waiting_approval 与 completed，状态写入收敛到 parent_agent/task_orchestrator 单入口。

### P1
6. AgentPlanStep 运行时从未写入（纯建表）——Phase 3 Planner 落地前必须明确复用还是迁移。
7. GUIDE 回落主对话后 AgentTask/Task 仍被 complete（第 5 节）。
8. 两聊天入口状态层独立（第 8 节），刷新/切换会话状态不一致。
9. DirectorConsoleView setInterval 假进度三阶段轮换（第 8 节）。
10. TaskCard.vue 第 112 行模板孤立字符 `n`。
11. /chat 同步阻塞无流式（FLOW-001）；chat_stream 未接入。

### P2
12. run_generate/run_style_update 无 preconditions（高风险工具缺前置检查挂载）。
13. 记忆写入无重复检测与敏感内容检查（add_memory 直写）。
14. 全站无鉴权（既有基线，公网暴露面见历史 SEC-002）。

## 12. 本次重构需新增/修改/迁移的文件清单（建议，供 Phase 1+ 使用）
修改（Phase 1 P0 修复）：
- backend/app/services/agent_memory.py（None 过滤）
- backend/app/services/session_memory.py / context_manager.py（摘要 scope）
- backend/app/api/v1/agent.py（_try_create_generate_approval 自动建项目移除、GUIDE 回落任务状态、append_message 角色收敛）
- backend/app/schemas/agent.py（AgentMessageCreate 角色白名单）
- backend/app/services/approvals.py（状态写入收敛）
- backend/app/services/parent_agent.py（waiting_approval 语义）
新增测试：tests/test_agent_memory_isolation.py（或并入 test_agent_memory.py）、test_agent_sessions.py 补充项目切换用例、test_agent_api_roles.py。
迁移评估：AgentMessage 增加 project_scope/epoch 字段需 Alembic 迁移（带 downgrade）；AgentPlanStep 复用无需新表。
兼容风险：append_message 角色收敛为破坏性 API 变更——当前前端两入口均只用 user 角色调用（实证 api/agent.ts appendMessage 调用点），兼容风险低，但需 Phase 1 回归确认无第三方调用方。

## 13. 阶段 0 验收自评
- [x] 每一项『已存在/缺失』结论均有代码/测试/实证证据（含内存库行为实证）
- [x] 调用链、模型关系、工具清单、分支、事件流、记忆行为、前端差异全覆盖
- [x] 测试基线可复现（169 passed 命令见第 9 节）
- [x] 未以 README 或旧审计替代代码核查
- Phase 0 完成，待用户验收后进入 Phase 1（P0 数据隔离、状态语义和安全修复）
