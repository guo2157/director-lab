# 导演台 Agent 意图识别逻辑改造交付报告

- 日期：2026-11-04
- 范围：Phase 1 架构审计 + Phase 2 意图识别改造 + Phase 3 任务运行时最小核心
- 验证：Agent 相关测试 110 passed；后端全量 453 passed / 7 failed（失败均为 /tmp/p0.png root 权限环境问题，与本次改动无关）

## 一、改动清单

### Phase 1：架构审计（零代码改动）
- 输出 `docs/agent/agent-intent-architecture-audit-20261104.md`：
  - 现有链路全景（规则快路径 + LLM 结构化识别 + NextAction 枚举路由）
  - 对照目标架构的 7 模块差距矩阵与 8 问题清单
  - 既有可复用资产清单与 P0 纪律约束

### Phase 2：意图识别改造

**1. Context Manager（新建 `app/services/context_manager.py`）**
- 结构化上下文：FocusContext（当前关注实体，保存 ID 不只保存名称）+ OperationContext（最近操作）
- 持久化：`AgentSession.context_state` JSON 字段（新增 Alembic 迁移 `b5f7a9c1d3e2`，已执行）
- 项目切换清理：`clear_on_project_switch`（update_session 端点接入，防止跨项目上下文污染）
- 焦点验证：`validate_focus`（存在 + 属于当前项目 + 未过期，任一不满足即弃用）
- LLM 注入：`build_focus_hint`（结构化焦点提示注入意图识别，供指代消解）

**2. Intent Resolver（新建 `app/services/intent_resolver.py`）**
- 融合层：规则结果 + LLM 结果 + 结构化上下文 → 统一标准化意图（ResolvedIntent）
- 实体引用解析优先级：显式 ID > 焦点上下文 > 澄清（不猜测执行）
- 实体 ID 一律数据库验证（存在 + 项目归属）；跨项目 ID 拒绝并澄清
- 目标歧义（镜号命中多个）输出澄清而非挑一个
- 无项目上下文：generate_request 透传（保留自动建项目能力），pipeline_action 澄清

**3. intent_router.py 增强**
- `classify_intent` / `_build_intent_user_prompt` 新增 `focus_hint` 参数（结构化焦点注入）

**4. chat 端点集成（`app/api/v1/agent.py`）**
- 意图识别前加载并验证焦点，注入 focus_hint
- 意图识别后经 Intent Resolver 融合，焦点更新随事务持久化
- Intent Resolver 判定需澄清时转 CLARIFY，优先使用其确定性澄清问题
- 助手消息 payload 记录 `intent_resolution`（resolution_path/target/clarification_code）审计

### Phase 3：任务运行时最小核心

**Parent Agent（新建 `app/services/parent_agent.py`）**
- `create_task_from_intent`：执行类意图（generate_request/pipeline_action）创建 AgentTask（goal/risk_level/confidence/success_criteria 快照落库）；query/analysis/ambiguous 不建任务
- 任务状态机：pending → running → completed/failed/cancelled（非法流转拒绝，终态不可变更）
- chat 端点集成：任务创建 + 各执行分支终态流转（GUIDE 落审批申请即完成 / PIPELINE 完成 / 澄清拒绝取消）
- 助手消息 payload 记录 `task_id`/`task_status`（前端任务计划区域展示用）

## 二、测试

- 新增 `tests/test_context_and_resolver.py`（11 用例）：焦点读写/项目切换清理/焦点验证（跨项目/过期）/显式 ID 定位/焦点回退/无目标澄清/镜号歧义澄清/跨项目 ID 拒绝/query 透传
- Agent 相关回归：110 passed（含既有意图路由/会话/工具/治理/E2E/生成审批全量用例）
- 后端全量：453 passed / 7 failed；7 个失败均为 `/tmp/p0.png` root 所有权导致的环境问题（该文件 2026-11-04 07:11 由 root 创建，早于本次改动），与本次改造无关

## 三、验收标准对照

| 验收项 | 状态 |
| --- | --- |
| 明确命令快速路由 | ✅ 既有规则快路径保留（零 LLM 调用） |
| 自然语言正确识别 | ✅ LLM 结构化识别 + 焦点上下文注入 |
| 多轮对话理解上下文 | ✅ Focus Context + 近 10 条消息 + 会话摘要 |
| 『重新生成』正确定位目标 | ✅ 显式 ID > 焦点回退 > 澄清，不猜测 |
| 多项目上下文隔离 | ✅ 项目切换清空焦点 + 焦点项目归属验证 + 跨项目 ID 拒绝 |
| 正则与 LLM 冲突消歧 | ✅ Intent Resolver 统一决策层 |
| 简单任务不调用多个 LLM | ✅ 规则快路径零 LLM；主对话分类+生成各 1 次 |
| 工具调用权限/参数校验 | ✅ 既有 TOOL_REGISTRY + PolicyEngine + 审批门禁保留 |
| 任务状态管理 | ✅ AgentTask 运行时 + 状态机（最小核心） |

## 四、复查修复记录（2026-11-04 10:20）

复查发现并修复 3 处任务状态机缺陷：

1. **GUIDE 回落主对话时任务悬挂**：generate_request 意图先创建 AgentTask，`_try_create_generate_approval` 定位不到目标时 `action=None` 回落主对话，但任务状态流转只在四个同步分支内执行，任务永远停在 pending。修复：主对话分支助手消息落库前 `complete_task` 闭环，payload 补记 task_id/task_status。
2. **LLM 失败时任务悬挂**：主对话 LLM 调用失败抛 biz_error 时任务停留在 pending。修复：抛错前 `fail_task` 落 failed 终态并单独 commit（用户重试产生新任务，旧任务保留审计）。
3. **审批申请提交失败虚报完成**：GUIDE 分支生成申请提交失败（approval_info.ok=False）时任务仍标记『已创建审批申请，等待人工审批』，摘要与事实不符。修复：按 approval_info.ok 区分 completed/failed。

修复后验证：Agent 相关回归 110 passed。

## 五、Execution Verifier 落地（2026-11-04 10:25，Phase 3 收尾）

**新建 `app/services/execution_verifier.py`**
- `verify_job_submission`：异步 Job 类执行结果确定性验证——工具返回的 job_id 必须对应库中真实 Job（存在/状态合法/项目归属与审批参数一致），不信任工具返回成功消息
- `verify_execution_result`：统一入口，区分两类结果——含 job_id 的异步 Job 类必须验证；不含 job_id 的同步状态变更类（create_project/save_script/review_assets/confirm_keyframes）透传不误报

**集成 `execute_approval` 端点**
- 审批执行结果经确定性验证后才返回成功（响应携带 verified/job_status），验证失败返回结构化错误码（job_id_missing/job_not_found/job_invalid_status/job_project_mismatch）

**测试**：新增 `tests/test_execution_verifier.py` 7 用例全部通过；Agent 相关回归 110 passed。

## 六、未完成事项与遗留风险

1. **子 Agent 调度（Phase 4）**：Script/Analysis/Prompt/Asset/Storyboard/Video/Quality 子 Agent 未实施；当前为单 Agent + 受控工具模式，复杂任务拆解依赖编排未建设
2. **执行结果验证器**：success_criteria 已落库，运行时确定性验证规则（产物存在性/数据库状态二次核验）未实施
3. **前端改造（Phase 5）**：payload 已携带 task_id/task_status/intent_resolution 供前端展示，但对话区任务计划区域/工具执行记录 UI 未改造
4. **意图分类细化**：仍为 6 粗类（query/analysis/generate_request/pipeline_action/ambiguous/unsafe），文档要求的细粒度意图树（asset.regenerate 等）未拆分
5. **多意图识别**：单消息多意图（『改提示词然后重新生成』）依赖主对话 LLM 兜底，无结构化多意图输出
6. **环境问题**：/tmp/p0.png 为 root 所有导致 7 个既有测试失败，需 root 清理后复测
7. **全站无鉴权（SEC-001）**：既有问题，长期记忆/跨用户隔离建设的前置条件
