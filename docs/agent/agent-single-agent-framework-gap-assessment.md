# Director-Lab Agent 对照《单 Agent 核心流程》框架差距评估报告

- 日期：2026-10-27 初版 / 2026-11-03 更新
- 评估对象：导演台 Agent（backend/app/api/v1/agent.py + backend/app/services/agent_tools.py + agent_tool_impls.py + agent_governance.py + intent_router.py）
- 参照框架：单 Agent 核心流程（理解目标 → 组装上下文 → 制定计划 → 调用工具 → 验证结果 → 重新规划或结束）+ 通用 Agent 开发指南
- 证据来源：代码实证 + E2E 验证报告 + 回归测试 137 passed

## 1. 总体结论

Director-Lab Agent 当前处于框架定义的**第一阶段（基本执行闭环）完成并进入第二阶段初期**：意图识别结构化（规则快路径 + LLM + NextAction 枚举路由）、上下文组装（20 个只读工具 + ToolBudget 预算控制 + 40 条历史窗口）、受控工具调用（24 个工具 + 权限分级 + PolicyEngine 策略引擎 + 审批门禁）、结果验证已具备且经 E2E 验证与 137 passed 回归测试；任务规划与状态管理（AgentTask/AgentPlanStep 表已落库）、重新规划、长期记忆尚未建设。整体评级：**第一阶段达成，第二阶段部分启动，第三阶段未启动**。

| 框架能力 | 现状 | 评级 |
| --- | --- | --- |
| 1. 目标与意图识别 | 规则快路径 + LLM 结构化识别 + NextAction 枚举路由 + 置信度阈值 + 澄清机制 | ✅ 已具备 |
| 2. 上下文组装 | 20 个只读工具按意图动态注入 + ToolBudget 预算控制 + 40 条历史窗口 | ✅ 已具备 |
| 3. 任务规划与状态管理 | AgentTask/AgentPlanStep 表已落库，运行时规划逻辑未实施 | 部分具备 |
| 4. 工具调用与权限控制 | 24 个工具 + 权限分级(read/write/generate) + PolicyEngine + 审批门禁 + ToolBudget | ✅ 已具备 |
| 5. 结果验证 | E2E + 回归测试 137 passed；success_criteria 字段已落库，运行时验证逻辑未实施 | 部分具备 |
| 6. 重新规划与结束判断 | LLM 瞬态错误指数退避重试 3 次；TerminalStatus 终态枚举已定义；多步重规划未实施 | 部分具备 |
| 7. 记忆更新 | 仅会话内消息持久化（seq 自增），无长期记忆 | 缺失 |

## 2. 逐维度评估

### 2.1 目标与意图识别 —— ✅ 已具备

**已具备（代码+验证实证）：**
- 系统提示词 `_DIRECTOR_SYSTEM_PROMPT` 明确角色边界：制作阶段链路、不虚构资产/任务、生成类操作不虚构已执行
- **结构化意图识别（B 档已落地）**：规则快路径（`apply_fast_path_rules`，unsafe > generate_request > query 优先级）+ LLM 结构化识别（Pydantic Schema 校验，json_mode）+ 高风险关键词规则兜底 + 置信度阈值 0.7
- **NextAction 枚举路由**：`route_action` 返回类型安全枚举（CHAT/REJECT/GUIDE/PIPELINE/CLARIFY），编排层不解析裸字符串
- **风险等级标注**：IntentResult 携带 risk_level（low/medium/high），高风险关键词规则兜底提级
- **澄清机制**：ambiguous 且带缺失信息时 `build_clarification_question` 确定性拼接，不进入主对话
- E2E 实证：剧本分析不触发生成（INT-01）、普通问答不建任务（INT-06）、含糊请求主动澄清（INT-07）

**差距：**
- 规则快路径 risk_level 恒 low（仅 LLM 路径产出 risk_level）
- intent_goal 仅 LLM 路径生效

### 2.2 上下文组装 —— ✅ 已具备

**已具备：**
- 动态注入：关联项目时通过 execute_tool 注入 project_overview / asset_stats / list_jobs 真实数据库结果
- **意图驱动工具选择**：`build_read_tool_calls` 按用户消息内容动态选择只读工具（20 个只读工具按意图-工具映射过滤）
- **条件注入**：`list_assets` 仅当用户消息命中资产类关键词时追加查询
- **ToolBudget 预算控制**：max_calls=12 / max_total_duration_ms=30000 / max_context_chars=24000，超限安全降级，违规记录进 violations 供审计
- 历史窗口：最近 40 条 user/assistant 消息（seq 升序），控制 Token 消耗
- CTX-001 缺陷已修复并复测

**差距：**
- 无用户偏好/长期记忆注入通道
- 无知识库（docs/）检索注入

### 2.3 任务规划与状态管理 —— 缺失

**现状：** chat 接口为单轮同步请求-响应，无多步任务拆分、无状态机。

**差距：**
- 无任务状态机（暂停/恢复/重试/取消均不支持）
- 无步骤级输入输出/前置依赖/成功条件定义
- 长耗时操作遵循项目 P0 约束走 202+job+SSE 通道，但该通道与 Agent 对话层未打通（Agent 不能创建/跟踪 job）

### 2.4 工具调用与权限控制 —— ✅ 已具备

**已具备（agent_tools.py + agent_governance.py 代码实证）：**
- **24 个工具注册**：20 个只读 + 4 个写/生成（request_generate / batch_request_generate / run_generate / request_style_update / run_style_update）
- **权限分级**：read / write / generate 三级，TOOL_REGISTRY 每个工具声明 permission / risk_level / side_effect / idempotent / requires_approval
- **PolicyEngine 策略引擎**：`evaluate_tool_policy` 确定性判定（requires_approval 且未获批 → REQUIRE_CONFIRMATION；risk_level=high 且未获批 → REQUIRE_CONFIRMATION；其余 ALLOW）
- **审批门禁**：request_generate / batch_request_generate 只建审批记录不执行；run_generate / run_style_update 须 confirmation_id + approved=True
- **视频速度参数**：request_generate / batch_request_generate 支持 speed（fast/std/quality），按 Workflow.capabilities.tier 匹配档位工作流
- 标准化结果：ToolResult(ok/data/error_code/error_message/duration_ms)
- 错误分类：tool_not_found / tool_invalid_args / tool_internal_error / 业务错误码
- 参数校验：Pydantic Schema model_validate，limit 钳制
- **ToolBudget 预算控制**：调用数/耗时/上下文字符数上限，超限安全降级
- 安全设计：模型只负责选择与解释，执行由确定性业务代码完成（符合 AGENTS.md P0）

**差距：**
- 工具由后端按意图固定注入，模型不能自主选择调用工具（无 function calling 循环）
- 无超时时间/重试策略的运行时执行（Schema 中已声明 timeout_seconds/max_retries 字段）

### 2.5 结果验证 —— 部分具备

**已具备：**
- E2E 验证层：CTX-01 助手回复与数据库逐项比对一致；SEC-01 注入防护三项拒绝；NEG-01 负向路径 422/404
- 工具返回真实数据库查询结果，禁止模拟数据（代码注释明示）

**差距：**
- 运行时无自动结果校验（如助手回复中的数字与工具数据二次核对）
- 无任务成功标准的机器可读定义

### 2.6 重新规划与结束判断 —— 部分具备

**已具备：**
- **LLM 瞬态错误重试**：unreachable/timeout/llm_error 指数退避重试最多 3 次；llm_unconfigured 等配置类错误不重试
- **TerminalStatus 终态枚举**：COMPLETED / WAITING_FOR_USER / WAITING_FOR_HUMAN / DENIED_BY_POLICY / FAILED_SAFE，API 响应与审计统一携带
- 用户消息已持久化，重试幂等安全

**差距：**
- 无参数修正重试、无更换工具策略
- 无多步任务的重新规划能力（依赖 2.3 任务规划未实施）
- 工具调用失败后无自动重试（Schema 中已声明 max_retries 字段，运行时未执行）

### 2.7 记忆更新 —— 缺失

**现状：** 仅会话内消息持久化（AgentMessage，seq 唯一索引），会话归档后消息保留可恢复。

**差距：**
- 无跨会话长期记忆（情景/语义/程序记忆均无）
- 无价值判断/去重/冲突处理/有效期管理机制
- 无用户偏好沉淀

## 3. 对照三阶段建设优先级

| 阶段 | 框架要求 | Director-Lab 现状 |
| --- | --- | --- |
| 一：基本执行闭环（意图→上下文→工具→验证） | 必备 | **大部分达成**：意图识别✅ 上下文组装✅ 工具调用✅（只读） 结果验证✅（E2E 层） |
| 二：复杂任务能力（规划→状态→重试→重规划） | 进阶 | **未启动**：与项目阶段 E（审批事件/意图路由工具化）规划部分重叠 |
| 三：个性化与持续学习（长期记忆/偏好/经验） | 增强 | **未启动** |

## 4. 框架三大核心能力对照

1. **明确任务成功标准**：部分具备——E2E 验证有明确通过判据（job 数不变、回复与 DB 一致），但产品运行时无成功标准定义
2. **保存和管理任务状态**：部分具备——会话/消息持久化完善（含归档/恢复），但任务级状态机缺失
3. **验证工具执行结果**：部分具备——工具结果结构化+错误分类+E2E 实证，运行时自动校验缺失

## 5. 建议路线（按框架优先级与项目约束排序）

1. ~~**P1｜工具权限分级与确认机制**~~ ✅ 已完成：TOOL_REGISTRY 24 个工具权限分级 + PolicyEngine 策略引擎 + 审批门禁
2. ~~**P1｜意图路由工具化**~~ ✅ 已完成：结构化意图识别 + NextAction 枚举路由 + 置信度阈值 + 澄清机制
3. ~~**P2｜LLM 调用重试策略**~~ ✅ 已完成：瞬态错误指数退避重试 3 次
4. **P2｜流式输出**（FLOW-001 既有遗留）：SSE 输出助手回复，与项目 SSE 基建复用
5. **P2｜结果验证器**：success_criteria 字段已落库，待补确定性验证规则
6. **P3｜任务状态机**：多步任务（分析→生成→跟踪）状态管理，与 job 通道打通；AgentTask/AgentPlanStep 表已就位
7. **P3｜长期记忆**：跨会话项目事实/用户偏好沉淀，需先解决 SEC-001（全站无鉴权）前提
8. **P3｜工具调用重试**：Schema 中 max_retries 字段已声明，运行时执行逻辑待实施

## 6. 结论

Director-Lab Agent 的设计哲学与框架高度一致：『模型负责推理、业务代码负责确定性控制』即框架『工具由确定性代码执行』的落地。第一阶段（基本执行闭环）已全面达成：24 个工具注册（20 只读 + 4 写/生成）、权限分级 + PolicyEngine 策略引擎 + 审批门禁、结构化意图识别 + NextAction 枚举路由、ToolBudget 预算控制、LLM 瞬态错误重试、TerminalStatus 终态枚举，经 E2E 验证与 137 passed 回归测试。主要差距集中在第二阶段（任务规划运行时逻辑/结果验证器/工具调用重试/流式输出）与第三阶段（长期记忆）。
