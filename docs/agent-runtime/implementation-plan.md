# Agent Runtime 实施计划

> 基于 Phase 0 审计结果修订
> 执行原则: 分阶段实施, 阶段间验收通过才继续

---

## Phase 0: 审计与设计 ✅ 当前阶段

**产出** (docs/agent-runtime/):

| 文件 | 状态 |
|------|------|
| audit-report.md | ✅ 已完成 |
| current-architecture.md | ✅ 已完成 |
| target-architecture.md | ✅ 已完成 |
| data-model-design.md | ✅ 已完成 |
| migration-plan.md | ✅ 已完成 |
| implementation-plan.md | ✅ 本文件 |

---

## Phase 1: Agent Runtime 基础 (M1)

**目标**: 建立可持久化、可恢复的 Agent Runtime 核心

### 1.1 数据模型 (M1 Migration)
- [ ] 创建 agent_goal 表
- [ ] 创建 agent_plan 表
- [ ] 创建 agent_execution_log 表
- [ ] agent_task 扩展(goal_id/plan_id/current_step_id/lifecycle_state等)
- [ ] Alembic Migration + 验证

### 1.2 Goal Manager
- [ ] 新建 app/services/goal_manager.py
- [ ] create_goal / get_active_goal / update_goal_status
- [ ] 集成到 Agent API: 首次消息自动创建或更新Goal

### 1.3 Context Resolver
- [ ] 新建 app/services/context_resolver.py
- [ ] 整合 context_manager + intent_resolver
- [ ] 分层上下文: User/Session/Project/Task/Job/Memory
- [ ] Context Snapshot 机制

### 1.4 Constraint Manager
- [ ] 新建 app/services/constraint_manager.py
- [ ] 整合 agent_governance PolicyDecision
- [ ] 项目权限/任务权限/业务约束/资源限制

### 1.5 Agent Loop 基础
- [ ] 新建 app/services/agent_runtime.py (Runtime入口)
- [ ] 基础Loop: Goal→Context→Constraint→(Plan待Phase2)→Execute→Log
- [ ] 保留现有 Agent API 对话流(SSE), Runtime 注入阶段

### 1.6 任务恢复机制
- [ ] 从 agent_plan/agent_plan_step 恢复中断执行
- [ ] 页面刷新/服务重启后可重读状态
- [ ] 确认重复请求防重(goal_id + session_id 去重)

### Phase 1 验收

| 验收项 | 验证方式 | 期望 |
|------|---------|------|
| Goal 持久化 | 创建Goal后刷新页面 | Goal存在且状态正确 |
| Context Snapshot | 执行Plan前后检查 | 版本/阶段信息正确 |
| AgentTask 扩展 | 查询新字段 | goal_id/plan_id可写可读 |
| 任务恢复 | 创建Goal→刷新→继续 | 不重复创建, 续上状态 |
| Agent Loop | 单步骤执行 | 正确日志落库 |

---

## Phase 2: Planner + Executor + Verifier + Replanner (M2)

**目标**: 建立动态计划与可靠执行闭环

### 2.1 Planner + Plan Validator

- [ ] 新建 app/services/planner.py
- [ ] Plan生成: Goal+Context→plan_json(含依赖/前置/并行)
- [ ] Plan Validator: 14项检查(工具/权限/依赖/循环/资源)
- [ ] Plan Revision: 变更递增revision, 保留历史
- [ ] 复用现有 agent_plan_step 表(plan_id非NULL)

### 2.2 Executor 升级

- [ ] 新建 app/services/execution_manager.py
- [ ] 整合 task_orchestrator: 创建Task+TaskStage映射Step
- [ ] Tool Selection: agent_tools动态筛选(按阶段/权限/前置)
- [ ] 执行锁: plan_step级别的并发控制
- [ ] 超时+Retry: 每Step timeout_seconds + max_retries

### 2.3 Verifier 升级

- [ ] 从 execution_verifier.py 扩展
- [ ] State/Artifact/Business/Quality/Human 五维验证
- [ ] 验证失败→不标记完成→触发Replanner

### 2.4 Replanner

- [ ] 新建 app/services/replanner.py
- [ ] 错误分类→Retry/Replan/Wait/Fail
- [ ] Plan Revision 递增+保留历史
- [ ] 限制: 最大Retry=3, 最大Replan=3

### 2.5 Agent Loop 完整

- [ ] Agent Runtime 完整Loop集成
- [ ] Async Job完成后自动恢复Agent
- [ ] bridge_job_event→Verifier→下一步/Replan
- [ ] bridge_approval_resolved→恢复执行

### Phase 2 验收

| 验收项 | 验证方式 | 期望 |
|------|---------|------|
| 动态计划 | 有项目时输入目标 | 自动生成含依赖的Plan |
| 跳过已完成 | 重复相同目标 | 识别并跳过已完成Step |
| Job→Agent恢复 | Job完成后 | Agent自动继续下一步 |
| 验证失败 | 构造无效产物 | 不完成, 触发Replan |
| Replan | 模拟Job失败 | 有限Replan, 不无限 |

---

## Phase 3: Tool Registry 统一

**目标**: 将现有业务能力统一接入 Agent Runtime

### 3.1 Tool Spec 完善

- [ ] 补充 output_schema(每个工具)
- [ ] 补充 retry_policy / timeout 默认值
- [ ] 补充 verification 规则(每个工具)
- [ ] 统一日志: 所有工具执行写 agent_execution_log

### 3.2 动态工具筛选

- [ ] 根据 pipeline_state 节点动态筛选
- [ ] 根据 Context Snapshot 排除不适用工具
- [ ] 根据权限动态裁剪

### 3.3 业务工具接入确认

- [ ] 确认 29 个工具全部经统一 execute_tool 网关
- [ ] 确认所有工具经业务服务(非直连DB写)
- [ ] 确认所有工具权限校验
- [ ] 确认无绕过业务约束的执行入口

### Phase 3 验收

| 验收项 | 验证方式 | 期望 |
|------|---------|------|
| Tool Spec完整 | 查询每个工具 | 有 input/output/retry/verification |
| 动态筛选 | 不同阶段查询工具列表 | 列表随阶段变化 |
| 执行日志 | 任意工具调用 | agent_execution_log 有记录 |
| 无绕过入口 | 代码审查 | 无直连DB写/ComfyUI绕过 |

---

## Phase 4: Human Gate 人工审核 (M3)

**目标**: 将用户审核真正融入 Agent Runtime

### 4.1 资产审核门禁恢复

- [ ] 角色/场景/道具生成后→人工审核(待确认→通过→进入分镜图)
- [ ] 审核状态验证: 后端检查 not 前端隐藏按钮
- [ ] 未审核→Agent plan_step=waiting_approval

### 4.2 分镜图审核门禁恢复

- [ ] 分镜图生成后→人工审核→通过才进入分镜视频
- [ ] 审核拒绝→Agent Replan
- [ ] 重新生成→审核失效→新版本重新审核

### 4.3 审核版本绑定

- [ ] Asset Review 绑定 Asset Version
- [ ] Shot Review 绑定 Storyboard Version
- [ ] 修改/重新生成后原审核自动失效
- [ ] 依赖旧审核的Step标记为stale

### 4.4 后端业务门禁

- [ ] 分镜图生成API: 检查资产审核
- [ ] 分镜视频生成API: 检查分镜图审核
- [ ] 合成/导出API: 检查各前置审核

### Phase 4 验收

| 验收项 | 验证方式 | 期望 |
|------|---------|------|
| 资产未审核→禁分镜图 | 直接调API | 返回业务门禁错误 |
| 分镜图未审核→禁视频 | 直接调API | 返回业务门禁错误 |
| 修改资产→审核失效 | 重新生成资产 | 旧审核不可用 |
| Agent等待审核 | 触发审核Step | plan_step=waiting_approval |
| 审核后恢复 | 审核通过 | Agent自动继续 |

---

## Phase 5: Agent Workbench 前端

**目标**: 建立真正的任务执行界面

### 5.1 组件升级

- [ ] GoalPanel: 当前Goal展示+修改/取消
- [ ] PlanPanel: Plan Steps+DAG可视化
- [ ] ProgressPanel: Step状态+进度+Job
- [ ] ReplanPanel: Replan触发+Revision对比
- [ ] ResumePanel: 历史任务恢复入口

### 5.2 事件驱动

- [ ] WebSocket/SSE 订阅 agent_event
- [ ] Plan变更实时推送
- [ ] Step状态实时更新
- [ ] Job完成实时反馈

### Phase 5 验收

- 用户能看到完整Plan与Step进度
- 用户能看到Job与产物
- 用户能暂停/取消/恢复任务
- 页面刷新不丢失任务状态

---

## Phase 6: Memory + 评估 + 优化

**目标**: 提高 Agent 长期工作能力和可靠性

### 6.1 Memory Retrieval

- [ ] 语义检索(替换简单列表)
- [ ] Context Resolver 集成
- [ ] 项目/偏好/经验分层注入

### 6.2 成本控制

- [ ] Token/调用次数/耗时统计
- [ ] Goal级成本预算
- [ ] 超限安全降级

### 6.3 质量评估

- [ ] Agent场景测试(20个场景)
- [ ] 单元测试补充(Planner/Replanner/Verifier)
- [ ] 集成测试(全链路)
- [ ] 失败率/Replan率/用户介入率监控

---

## 阶段依赖关系

```
Phase 0 (审计) ✅
  └─→ Phase 1 (Runtime基础+M1)
          └─→ Phase 2 (Planner/Executor/Verifier/Replanner+M2)
                  ├─→ Phase 3 (Tool Registry)  ≤ 可与Phase2并行
                  ├─→ Phase 4 (Human Gate+M3)
                  │       └─→ Phase 5 (Frontend)
                  └─→ Phase 6 (Memory+评估)
```

---

## 风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| M1列扩展与旧代码兼容 | 现有agent_task读写可能异常 | 新增列全NULLABLE, 旧代码不感知 |
| Agent Loop重构影响SSE | 对话流中断 | 保留现有SSE流, Runtime注入而非替代 |
| 恢复资产审核影响存量项目 | 历史项目无法继续 | 审核状态默认通过(向后兼容), 新项目启用门禁 |
| 前端改造成本高 | Phase 5周期长 | 渐进式: Plan/Step展示优先, 复杂可视化后续 |
