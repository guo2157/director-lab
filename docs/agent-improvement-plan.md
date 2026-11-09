# Director-Lab 导演台 Agent 2.0 改进计划（Phase 0 审计输出）

- 日期：2026-11-09
- 依据：docs/agent-audit-report.md、docs/agent-current-architecture.md、docs/agent-problem-list.md
- 原则：先审计后改造、优先复用现有业务能力、不破坏既有流程、每阶段验收后再进入下一阶段

---

## Phase 1：意图识别与上下文重构

### 目标
解决意图粒度不足、默认兜底无关、多轮上下文丢失问题。

### 实施步骤
1. 扩展意图体系：在 intent_router.py 现有 6 类基础上补充细粒度 action 键与 target 结构（script.analyze、asset.generate、asset.regenerate、storyboard.generate、video.generate 等），保持 query/analysis/generate_request/pipeline_action 向后兼容。
2. 统一结构化意图输出：intent_resolver.py 输出扩展为包含 intent、entities、context_reference、confidence、requires_confirmation、requires_clarification、missing_fields、reason 的 Pydantic 模型，并做 Schema 校验。
3. 分级置信度处理：>=0.85 执行条件检查；0.65~0.84 结合上下文重判；<0.65 澄清。
4. 整改默认兜底：主对话增加确定性三出口（general_answer / clarification / unsupported），删除通用『无法理解』回复。
5. 增强 Context Manager：AgentSession.context_state 增加 execution 子结构（current_task、completed_steps、failed_steps、retry_count、last_result）与 pending_confirmation_id 显式字段，需 Alembic 迁移。
6. 增加意图识别日志：每次 classify_intent/resolve_intent 结果落 tool_event 审计消息。
7. 建立意图测试集：backend/tests/test_agent_intent.py。

### 涉及文件
- backend/app/services/intent_router.py
- backend/app/services/intent_resolver.py
- backend/app/services/context_manager.py
- backend/app/api/v1/agent.py
- backend/app/models/agent.py
- backend/alembic/versions/*（新增迁移）
- backend/app/schemas/agent.py
- backend/tests/test_agent_intent.py

### 验收标准
- 能区分普通对话、查询、执行；能正确处理确认/拒绝/纠正；不再出现无关兜底；能识别当前项目与当前任务。
- 新增测试全部通过，现有测试不回归；每项修改输出文件清单与测试结果。

## Phase 2：工具系统与任务调度重构

### 目标
建立可靠的任务执行体系：统一 Tool Registry、前置条件检查、任务持久化、父子任务关联、DAG 调度、结果聚合、幂等、重试与恢复。

### 实施步骤
1. ToolDefinition 增加 preconditions 列表，execute_tool 运行时执行前置条件检查（剧本状态、资产生成/审核状态、分镜状态），不满足时返回 PRECONDITION_FAILED 并给出阻塞原因。
2. 实现任务状态机运行时与持久化：AgentTask/AgentPlanStep 接入现有 task_orchestrator，TaskStage 增加 agent_name 字段并落库。
3. 实现 DAG Scheduler：支持无依赖子任务并行（角色/场景/道具分析）、有依赖子任务等待（分镜分析依赖前三者），最大并发数、单任务超时、失败隔离、取消传播。
4. 实现父 Agent 结果聚合器：按 plan_id/task_id 收集所有子任务结果，缺失时标记失败，统计 total/items/errors，并写入数据库。
5. 实现多结果 Validator：扩展 execution_verifier 增加聚合完整性校验（数量校验、关联关系校验）。
6. 任务幂等增强：生成任务使用 idempotency_key + 任务状态检查 + 项目资源锁，防止重复执行；明确重新生成时创建新版本。

### 涉及文件
- backend/app/services/agent_tools.py
- backend/app/services/task_orchestrator.py（新增 DAG Scheduler）
- backend/app/services/parent_agent.py
- backend/app/services/execution_verifier.py
- backend/app/services/agent_tool_impls.py（工具实现增加前置条件）
- backend/app/models/task.py（TaskStage 增加 agent_name）
- backend/alembic/versions/*（新增迁移）
- backend/tests/test_agent_tasks.py

### 验收标准
- 工具调用正确、前置条件判断准确；子任务结果完整；任务失败不假装成功；部分失败可恢复；同一生成任务不重复执行；DAG 依赖正确执行。

## Phase 3：实时进度与前端改造

### 目标
用户可在聊天框看到完整执行过程：任务卡片、子 Agent 状态、失败详情、断线恢复。

### 实施步骤
1. 事件系统扩展：task_event 增加 subtask.created/started/progress/completed/failed 事件类型，事件数据包含子任务名称、阶段、进度、error。
2. SSE 断线恢复完善：确认 Last-Event-ID 或 seq 严格大于补齐、历史事件补发、重复事件去重；任务执行独立于浏览器连接（现已具备，补充测试）。
3. 进度计算：子任务权重计算任务进度，禁止 LLM 任意生成百分比；任务 completed 前必须通过结果校验。
4. 前端任务卡片增强：TaskCard.vue 修复疑似模板异常（`<a\nn v-if="downloadUrl"`），增加失败详情区（失败阶段/工具/错误码/错误信息/已完成子任务/是否可重试）、重试按钮。
5. 聊天界面增强：消息渲染支持任务卡片内联进度、子 Agent 列表、完成后结果摘要（角色/场景/道具/分镜数量、完整性检查、下一步建议）。
6. 页面刷新恢复：进入会话时查询 /api/tasks?session_id= 并渲染活动任务卡片。

### 涉及文件
- backend/app/api/v1/events.py
- backend/app/services/task_orchestrator.py（事件类型扩展）
- frontend/src/components/TaskCard.vue
- frontend/src/views/DirectorConsoleView.vue
- frontend/src/composables/useTaskEvents.ts
- frontend/src/api/agent.ts
- backend/tests/test_agent_sse.py

### 验收标准
- 长任务持续反馈；页面刷新后能恢复任务；子 Agent 过程可见；完成后展示真实业务结果；不再只返回『流程已执行』；SSE 断线重连补齐正常。

## Phase 4：Agent 质量提升

### 目标
提升长期运行可靠性与可观测性。

### 实施步骤
1. 建立自动化评估：意图识别数据集、多 Agent 场景测试、SSE 恢复测试、上下文测试，纳入 pytest。
2. 链路追踪：为每次 Agent 执行生成 trace_id，串联意图、planner、工具、子 Agent、LLM 调用与 token 消耗，写入日志/审计消息。
3. 成本统计：记录每次 LLM 调用模型、输入输出 token、耗时。
4. 模型降级策略：LLM 失败时按策略重试或切换模型，不阻断对话。
5. 记忆管理优化：统一记忆来源与时间戳，确保用户纠正优先、项目隔离、可追溯。

### 涉及文件
- backend/app/integrations/llm.py
- backend/app/services/agent_governance.py
- backend/tests/test_agent_eval.py
- backend/tests/test_agent_recovery.py
- backend/app/services/session_memory.py
- backend/app/services/agent_memory.py

### 验收标准
- 关键测试通过：意图识别准确率 >=95%、子任务结果完整率 100%、假成功率 0%、任务状态可恢复率 100%、事件持久化率 100%；既有业务流程不受影响。

## 风险与注意事项

- 不破坏现有项目：所有数据库迁移可回滚，新旧接口尽量兼容，不修改无关代码，不删除现有功能。
- 不重写业务服务：Agent 只做调度与控制流，业务执行仍走既有 pipeline_tools / regen_tools / script_parse / compose 等服务。
- 分阶段施工：每阶段独立验收，未通过不进入下一阶段，避免大规模一次性重写风险。
- 前端改动必须回归测试：导演台聊天是核心交互界面，需在真实会话中验证审批、审核、生成、任务卡片全流程。