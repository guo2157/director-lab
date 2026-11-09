# Director-Lab 导演台 Agent 2.0 问题清单（Phase 0 审计）

- 日期：2026-11-09
- 性质：基于真实代码的定位清单，非概念分析

---

## 1. 意图识别问题

### 1.1 意图粒度不足
- 文件路径：backend/app/services/intent_router.py
- 类名：无（模块级函数）
- 函数名：classify_intent / route_action
- 调用关系：agent.py chat 主流程调用 classify_intent，随后 resolve_intent 与 route_action
- 问题原因：INTENT_CATEGORIES 仅为 query/analysis/generate_request/pipeline_action/ambiguous/unsafe 六类粗粒度，缺乏 script.analyze、asset.regenerate、video.generate 等细分
- 当前行为：用户『重新生成第二个角色图片』只能落到 generate_request 或 pipeline_action，目标解析依赖显式 ID，无 ID 时只能澄清
- 正确行为：识别为 asset.regenerate 并提取目标实体（第二个角色图片）
- 修改方案：扩展细粒度意图树，保持向后兼容（Phase 1）

### 1.2 置信度处理单一
- 文件路径：backend/app/services/intent_router.py
- 函数名：classify_intent
- 问题原因：仅 0.7 单一阈值，低于阈值且原意图在 query/analysis/pipeline_action 时改判 ambiguous，无 0.65～0.84 结合上下文重判区间
- 修改方案：引入分级置信度处理（Phase 1）

### 1.3 默认兜底整改未完成
- 文件路径：backend/app/api/v1/agent.py
- 函数名：chat 主流程（GUIDE/CLARIFY 分支）
- 问题原因：主对话 LLM 兜底仍依赖系统提示词约束，缺三个出口的确定性分流（general_answer/clarification/unsupported）
- 修改方案：增加确定性出口分类（Phase 1）

## 2. 上下文管理问题

### 2.1 无 Execution Context 持久化
- 文件路径：backend/app/models/agent.py
- 类名：AgentSession
- 问题原因：context_state 仅存 focus/operation，无 execution（当前任务、已完成步骤、失败步骤、重试次数、任务结果）
- 当前行为：对话恢复后无法精确知道任务执行到哪一步
- 修改方案：扩展 context_state 增加 execution 子结构（Phase 1）

### 2.2 多待确认事项消歧依赖语义
- 文件路径：backend/app/api/v1/agent.py
- 函数名：_mentions_pending_target
- 问题原因：多 pending 时依赖『第N版』『vN』等文本匹配，无结构化 pending_confirmation 字段
- 修改方案：AgentSession 增加 pending_confirmation_id 显式字段（Phase 1）

### 2.3 对话摘要按条截断
- 文件路径：backend/app/services/session_memory.py
- 函数名：maybe_compress_session
- 问题原因：SUMMARY_TRIGGER_THRESHOLD=10 时触发全量压缩，摘要约 4000 字，无分层/按主题摘要
- 修改方案：Context Manager 增强时同步改进（Phase 1）

## 3. 项目状态感知问题

### 3.1 无统一 ProjectContext 服务
- 文件路径：backend/app/services/progress.py / backend/app/api/v1/agent.py
- 函数名：derive_progress / generation_progress_snapshot
- 问题原因：项目状态分散在多个查询服务，未统一封装为 ProjectContext 对象，且 agent.py 内联注入只读工具
- 当前行为：Agent 能拿到部分状态，但无统一『当前项目/当前剧本/当前集数/各阶段状态/阻塞原因』结构
- 修改方案：新建 ProjectContext 汇编服务（Phase 1）

### 3.2 前置条件检查不完整
- 文件路径：backend/app/services/agent_tools.py / backend/app/api/v1/agent.py
- 函数名：execute_tool（preconditions 字段）
- 问题原因：ToolDefinition 存在 preconditions 字段但未运行时执行；仅 approval 门禁有效
- 当前行为：用户在没有剧本时直接请求生成分镜，若目标定位成功仍会创建审批申请，而不是先提示前置条件
- 修改方案：实现 preconditions 运行时检查（Phase 1/2）

## 4. 执行进度问题

### 4.1 进度为阶段均值，粒度不足
- 文件路径：backend/app/services/task_orchestrator.py
- 函数名：update_stage_progress
- 问题原因：任务进度取阶段均值，阶段内部缺少子步骤权重的进度计算
- 修改方案：按方案 §10.4 使用任务权重计算（Phase 3）

### 4.2 SSE 事件仅到 Task 级
- 文件路径：backend/app/api/v1/events.py
- 函数名：task 事件流
- 问题原因：task_event 只包含 status/progress/error，无子 Agent 执行阶段细粒度事件（角色分析/场景分析等）
- 修改方案：增加 subtask.* 事件（Phase 3）

### 4.3 Worker 重启任务恢复依赖补偿
- 文件路径：backend/app/services/task_orchestrator.py
- 函数名：reconcile_orphan_tasks
- 问题原因：无检查点机制，仅按 Job 终态补偿闭环，无法恢复中断的子步骤进度
- 修改方案：引入检查点与幂等恢复（Phase 2/3）

## 5. 父子 Agent 协作问题

### 5.1 无子 Agent 调度
- 文件路径：backend/app/services/parent_agent.py / task_orchestrator.py
- 函数名：create_task_from_intent / add_stage
- 问题原因：仅创建 AgentTask 与 TaskStage，无子 Agent 概念，TaskStage 无 agent_name 字段
- 当前行为：剧本分析等流程没有并行多 Agent，只靠单个 pipeline 工具串行执行
- 修改方案：新建子 Agent 调度与 DAG 执行器（Phase 2）

### 5.2 结果聚合缺失
- 文件路径：backend/app/api/v1/agent.py / backend/app/services/execution_verifier.py
- 函数名：执行抽象中无聚合
- 问题原因：execution_verifier 只验证单个 Job，无多子任务结果收集与数量校验
- 当前行为：无法保证角色/场景/道具/分镜分析全部完成才报告成功
- 修改方案：新增 ResultAggregator + Validator（Phase 2）

## 6. 前端问题

### 6.1 TaskCard.vue 疑似模板异常
- 文件路径：frontend/src/components/TaskCard.vue
- 问题原因：模板中发现 `<a\nn v-if="downloadUrl"` 疑似多余字符 n
- 影响：可能影响模板解析与下载链接渲染
- 修改方案：待复核后修复（Phase 3）

### 6.2 聊天框展示能力不足
- 文件路径：frontend/src/views/DirectorConsoleView.vue
- 问题原因：助手消息仅文本 + 任务卡片，未展示子 Agent 执行过程、失败详情（失败阶段/工具/错误码/已完成子任务）
- 修改方案：增强消息渲染与任务详情面板（Phase 3）

## 7. 数据与日志问题

### 7.1 缺统一执行链路日志
- 文件路径：backend/app/services/agent_tools.py / agent.py
- 问题原因：tool_event 审计消息已存在，但无 trace_id 串联 planner/tool/subagent/LLM token 消耗
- 修改方案：引入 trace_id 与执行链路日志（Phase 4）

### 7.2 成本统计缺失
- 文件路径：backend/app/integrations/llm.py
- 问题原因：未记录每次 LLM 调用的 token 消耗与费用
- 修改方案：增加成本统计（Phase 4）

## 8. 测试体系问题

### 8.1 无系统性 Agent 测试集
- 文件路径：backend/tests（存在测试但未按 2.0 方案分类）
- 问题原因：尚无意图识别数据集、上下文测试、多 Agent 测试、SSE 恢复测试
- 修改方案：按方案 §17 建立测试集（Phase 4）
