# Director-Lab 导演台 Agent 意图识别架构审计报告（Phase 1）

- 审计日期：2026-11-04
- 审计对象：/home/ubuntu/director-lab 导演台 Agent 意图识别与任务调度链路
- 审计性质：纯只读代码审计，本轮零代码改动
- 对照基准：《Director-Lab 导演台 Agent 意图识别系统架构设计与实施方案》（用户需求文档 18 节）
- 证据来源：backend/app/api/v1/agent.py、backend/app/services/intent_router.py、generate_intent.py、session_memory.py、agent_governance.py、agent_tools.py、agent_context_tools.py、backend/app/models/agent.py、docs/agent/ 既有差距分析文档

---

## 1. 现有架构总览（实际执行链路）

```text
用户消息 POST /agent/sessions/{id}/chat
  ↓
会话校验（archived 拒绝）+ 用户消息持久化（seq 唯一索引，冲突重试 3 次）
  ↓
视频生成工作流前置拦截（_handle_video_workflow，命中即返回）
  ↓
意图路由 intent_router
  ├─ 规则快路径 apply_fast_path_rules（unsafe > 返工 > 进度查询 > 流程操作 > query）
  ├─ LLM 结构化识别 classify_intent（Pydantic Schema + json_mode，近 10 条消息消解指代 + 会话摘要）
  └─ route_action 确定性路由 → NextAction 枚举（CHAT/REJECT/GUIDE/CLARIFY/PIPELINE）
  ↓（CHAT 分支）
项目上下文注入（意图驱动只读工具 + ToolBudget 预算 + tool_event 审计落库）
  ↓
历史窗口（40 条 user/assistant）+ 会话摘要（长期记忆）
  ↓
LLM 生成回复（瞬态错误指数退避重试 3 次）
  ↓
助手消息持久化（payload 携带 intent/route_action/confidence/tool_budget 审计）
```

## 2. 代码位置清单

| 模块 | 位置 | 行数 | 职责 |
| --- | --- | --- | --- |
| Agent API 入口 | app/api/v1/agent.py | 1174 | 会话/消息/审批 CRUD + chat 主流程 |
| 意图路由 | app/services/intent_router.py | 420 | 规则快路径 + LLM 分类 + NextAction 路由 |
| 生成目标定位 | app/services/generate_intent.py | 102 | 镜头/实体 ID 定位（只认 ID 不猜名称） |
| 会话记忆 | app/services/session_memory.py | 110 | 短期窗口 + LLM 摘要压缩（长期记忆） |
| Agent 治理 | app/services/agent_governance.py | 133 | NextAction/TerminalStatus 枚举 + ToolBudget + PolicyEngine |
| 工具注册中心 | app/services/agent_tools.py | 595 | TOOL_REGISTRY（24 工具，Schema 化）+ execute_tool |
| 工具实现 | app/services/agent_tool_impls.py | 1617 | 24 个工具的 Pydantic 入参模型与实现 |
| 上下文工具 | app/services/agent_context_tools.py | 300 | 意图驱动只读工具选择与上下文格式化 |
| 数据模型 | app/models/agent.py | 135 | AgentSession/AgentMessage/AgentTask/AgentPlanStep/AgentConfirmation |

## 3. 对照目标架构的差距矩阵

| 目标架构模块 | 现状 | 评级 | 差距说明 |
| --- | --- | --- | --- |
| Rule Engine（规则引擎） | apply_fast_path_rules：unsafe/返工/进度/流程操作快路径，置信度 1.0，命中即返回 | ✅ 基本具备 | 快路径 risk_level 恒 low；无规则冲突检测记录；无弱提示标记（关键词命中仅作 LLM 线索） |
| Context Manager（上下文管理） | 短期窗口 40 条 + LLM 摘要压缩 + 项目上下文注入（project_overview/asset_stats/list_jobs） | ⚠️ 部分具备（约 40%） | 无 Focus Context（当前关注实体）：『重新生成』无法定位上一操作对象；无 Operation Context（最近操作）；无跨项目切换隔离校验；上下文无优先级仲裁机制 |
| LLM Intent Classifier | classify_intent：Pydantic Schema + json_mode + 指代消解（近 10 条）+ 置信度阈值 0.7 + 失败降级 query | ✅ 基本具备 | 意图类别为粗粒度 6 类（query/analysis/generate_request/pipeline_action/ambiguous/unsafe），无文档要求的细粒度意图树（asset.regenerate 等）；无多意图识别；无否定意图显式字段 |
| Intent Resolver（意图融合消歧） | route_action 确定性路由 + generate_intent.resolve_shot/resolve_entity（只认显式 ID） | ⚠️ 部分具备（约 30%） | 无规则+LLM 结果融合层；指代消解仅靠 LLM 近 10 条消息，无结构化焦点回退；目标歧义时无『询问用户哪个实体』的消歧流程（定位不到一律不创建）；无实体 ID 数据库验证后的统一标准化意图输出 |
| Parent Agent（父 Agent 调度） | AgentTask/AgentPlanStep 表已落库，运行时规划逻辑未实施；chat 为单轮同步请求-响应 | ❌ 缺失 | 无任务状态机运行时（暂停/恢复/取消/重试）；无多步任务拆解与依赖编排；无子 Agent 调度 |
| Tool Registry（工具注册中心） | TOOL_REGISTRY 24 工具：permission/risk_level/side_effect/idempotent/requires_approval/timeout/max_retries + Pydantic Schema + PolicyEngine + 审批门禁 | ✅ 已具备 | timeout_seconds/max_retries 已声明但运行时未执行；无 Token/成本统计 |
| Execution Verifier（执行验证） | E2E 测试层验证（137 passed）；success_criteria 字段已落库 | ❌ 运行时缺失 | 工具返回 ok 即视为成功，无产物存在性/数据库状态二次核验；无失败自动分析与可重试判定 |

## 4. 问题清单（对应需求文档 8 个问题）

| # | 需求文档问题 | 现状判定 | 证据 |
| --- | --- | --- | --- |
| 1 | 不能仅依赖关键词和正则 | ✅ 已解决 | 规则快路径仅处理明确命令类，自然语言走 LLM 结构化识别 |
| 2 | 正则无法理解上下文和指代 | ✅ 已解决 | classify_intent 携带近 10 条消息 + 会话摘要消解指代 |
| 3 | 连续对话『重新生成』需知道操作对象 | ❌ 未解决 | 无 Focus/Operation Context，generate_intent 只认显式 ID（chr_/sht_ 前缀），『重新生成』无 ID 时定位不到即放弃 |
| 4 | 切换项目不能上下文串项目 | ⚠️ 部分解决 | 实体定位有 project_id 校验（shot_not_in_project/entity_not_in_project），但会话级 project_id 切换时无焦点上下文清理与隔离校验 |
| 5 | 意图识别和任务规划不能混在一起 | ⚠️ 部分解决 | 意图识别已独立（intent_router），但任务规划运行时缺失，两者边界尚未在运行时体现 |
| 6 | 简单任务不能每次调用多个 LLM | ✅ 已解决 | 规则快路径零 LLM 调用；主对话仅分类+生成各 1 次 LLM，摘要压缩按阈值触发 |
| 7 | 复杂任务需要父 Agent 统一调度 | ❌ 未解决 | 无 Parent Agent 运行时，AgentTask/AgentPlanStep 表空置 |
| 8 | 工具调用必须经过参数、权限和依赖校验 | ✅ 基本解决 | Pydantic Schema 校验 + PolicyEngine + 审批门禁 + ToolBudget；依赖校验（任务依赖）缺失 |

## 5. 既有可复用资产（不重复造轮子）

| 资产 | 位置 | 复用点 |
| --- | --- | --- |
| NextAction/TerminalStatus 枚举 | agent_governance.py | Intent Resolver 输出直接复用，不新造动作枚举 |
| ToolBudget 预算控制 | agent_governance.py | Parent Agent 调度循环直接复用 |
| PolicyEngine 策略引擎 | agent_governance.py | 工具权限校验统一入口 |
| AgentTask/AgentPlanStep/AgentConfirmation 表 | models/agent.py | Parent Agent 任务状态机直接落库，无需迁移 |
| tool_event 审计消息 | api/v1/agent.py | 意图日志与执行审计统一通道 |
| 会话摘要机制 | session_memory.py | Memory Context 长期记忆基础 |
| job 通道（202+job+SSE） | worker/ | 长耗时生成任务执行底座，Parent Agent 编排复用 |

## 6. 架构取向约束（必须遵守的项目 P0 纪律）

1. 模型只负责识别与解释，路由与执行策略由确定性业务代码执行（AGENTS.md P0）。
2. 生成类操作不在对话内直接执行，走审批申请 + job 通道。
3. 工具只能由确定性代码执行，模型不得自述已调用工具。
4. 实体定位只认显式 ID 或唯一定位结果，不按名称猜测。
5. LLM 失败不阻断对话，安全降级。

## 7. Phase 2 改造方案（基于审计结论）

按差距优先级实施：

1. Context Manager（app/services/context_manager.py 新建）：结构化上下文（conversation/project/task/focus/operation/memory），Focus Context 保存实体 ID+类型+名称，Operation Context 记录最近操作与结果；会话切换项目时清空焦点；持久化到 AgentSession 新增 JSON 字段（需 Alembic 迁移）。
2. Intent Resolver（app/services/intent_resolver.py 新建）：融合规则结果 + LLM 结果 + 上下文；实体引用解析优先级：显式 ID > Focus Context > 澄清；输出统一标准化意图（含 target 实体、context_references、confidence、requires_clarification）；实体 ID 一律数据库验证。
3. 意图日志：意图解析结果（规则命中/LLM 结果/融合决策/消歧路径）落 tool_event 审计消息。
4. 意图分类细化：在现有 6 类基础上为 generate_request/pipeline_action 补充细粒度 action 键与 target 结构，保持向后兼容。

Phase 3/4（Parent Agent 运行时、子 Agent 调度）依赖 Phase 2 落地后另行实施。
