# Director-Lab · Phase E「Agent 工作台前端」编码 Agent 任务提示词

> 前置条件：**Phase D 全部合并**（审核接口、开关、最小审核 UI 已上线），Phase B–D 的端到端测试全绿。行号只作参考，请按函数名或组件名定位。
> 用法：每次会话先粘贴 **〇、通用上下文**，再粘贴**一个**任务。一个任务对应一个 PR。
> 执行顺序：**E0 → E1 → E2 → E3 → E4 → E5 → E6 → E7 → E8 → E9 → E10**（E1 是后端小补充，可与 E0 并行）
> 原则：在现有导演台上**增量添加**，不推倒重写；**页面状态只来自后端接口，不从聊天文字推断**。

---

## 〇、通用上下文（每个任务前都要粘贴）

```text
你在 Director-Lab 仓库中工作。前端 frontend/：Vue 3.5 + TypeScript 5.6 + Vite 6 + Pinia + Naive UI + @tanstack/vue-query + axios，
图标统一用 @vicons/ionicons5。后端 backend/：FastAPI + SQLAlchemy + SQLite。
当前任务属于「Agent Runtime Phase E：Agent 工作台前端」。

后端在 Phase B–D 已经提供（请先阅读 docs/API.md 和对应代码，确认实际字段名后再写类型）：
- 目标状态：GET /api/agent/{session_id}/goal、GET /api/agent/{session_id}/goals（历史，分页）、GET /api/agent/goals/{goal_id}
  返回 goal（含 lifecycle_state、fingerprint、constraints.goal_spec、budget_usage）、最新 plan（revision、status）、
  steps（step_id、step_key、step_type、tool_name、status、job_id、confirmation_id、error_code、result）、最近执行日志
- 控制：POST /api/agent/goals/{goal_id}/pause | /resume | /cancel、POST /api/agent/goals/{goal_id}/budget
- 审核（Phase D）：GET /api/projects/{id}/reviews/queue?stage=assets|keyframes、POST .../assets/{asset_id}/review、POST .../reviews/batch、
  GET/PUT /api/projects/{id}/review-policy
- 事件（workspace_event outbox，经 /api/workspace/events SSE + /api/workspace/events/backfill 补齐）：
  agent.goal.created / agent.goal.updated / agent.goal.superseded / agent.goal.finished / agent.plan.created /
  agent.step.updated / agent.tick.finished / review.asset_updated / review.policy_updated
- 助手消息 payload：route_action="agent_runtime"，带 goal_id、plan_id、lifecycle_state、tick；Worker 推进时写入的消息 payload.source="agent_runtime"
- Goal 对应一个统一 Task（goal.task_id），所有产生 Job 的步骤都挂在它的 TaskStage 上，可复用现有任务卡片和任务事件流显示 Job 进度

lifecycle_state 取值：planning / running / waiting_job / waiting_approval / waiting_user / paused / partially_completed / completed / failed / cancelled
step_type 取值：tool_call / track_job / stage_expand / clarification / review_gate
step status 取值：pending / ready / running / waiting_job / waiting_approval / waiting_user / succeeded / failed / skipped / cancelled

现有前端结构（必须复用，不要另起一套）：
- 两个对话界面：views/DirectorConsoleView.vue（/director 整页导演台：SessionRail + MessageStream + ChatComposer + ReviewTimeline + ContextSidebar）
  和 components/DirectorAgentPanel.vue（全局右侧助手面板，状态在 stores/agent.ts）。
  注意：DirectorConsoleView 自己维护了一份 approvals / reviewCards 本地状态，与 stores/agent.ts 重复。
  本阶段**新增的目标相关状态只放在一个 Pinia store 中**，两个界面共用。
- 事件：composables/useWorkspaceEvents.ts（进程级单例，按 lastSeq 补齐，onWorkspaceEvent 订阅）；
  composables/useTaskEvents.ts（任务事件单例，taskSnapshots 按 task_id 合并进度）；components/TaskCard.vue。
- 前端事件类型常量在 api/workspace.ts::WORKSPACE_EVENT_TYPES，需要与后端白名单同步。
- 确认弹窗：composables/useConfirmDialog.ts；导航：services/workspaceNavigation.ts（禁止在组件里直接 router.push 到业务页面）。
- HTTP 封装：api/client.ts 的 get / post 等，地址必须经 apiUrl() 生成（兼容子路径部署）。

设计约束（DESIGN.md，CI 强制）：
1. .vue 中禁止出现 #xxxxxx、rgba()/rgb()/hsl() 字面量，只能用 frontend/src/styles/global.css 中的 var(--token)；
2. 响应式断点只允许 1100px / 768px / 480px，桌面优先，@media 只写降级；
3. 文字对比度令牌在四档背景上 ≥ 4.5:1；
4. 状态不能只靠颜色表达：使用 .status-tag（自带圆点）+ 文字；生成中用 status-tag-pulse；
5. 可点击区域最小 28×28px，纯图标按钮必须带 aria-label；键盘焦点使用 var(--focus-ring)；尊重 prefers-reduced-motion；
6. 卡片层级靠边框 + --card-sheen，不用阴影；不新增字体、颜色、圆角体系；不在单个页面写 themeOverrides。

硬性约束：
1. 页面上的目标、计划、步骤状态**只来自后端接口**。事件只作为"需要刷新"的信号：收到 agent.* 事件后重新拉取目标状态，
   不用事件 payload 拼凑或推算状态；不从聊天文字推断状态。
2. 无法精确计算的进度不显示百分比（显示"进行中"或不确定进度条）；不伪造耗时或剩余时间。
3. 文案如实：只有 lifecycle_state=completed 才能出现"已完成"；waiting_job 时不能出现"生成完成"。
4. 不修改现有审批卡片、任务卡片、审核时间线对非 Agent 场景的行为。
5. 开关 agent_runtime_chat_enabled 关闭（后端不会产生任何 Goal）时，新 UI 完全不出现，页面与改动前一致。
6. 不引入新的 UI 库或状态管理库。E0 允许新增 vitest 作为开发依赖（仅用于纯逻辑单测），其他新依赖需在 PR 中说明理由。

交付要求：
- `cd frontend && npm run check:colors:strict && npm run check:breakpoints:strict && npm run check:contrast:strict && npm run build` 全部通过；
  E0 之后 `npm run test` 也必须通过。
- 每个 PR 附桌面（1440）、平板（768）、手机（390）三档截图（可用 scripts/uiux-shots.mjs 的方式生成），放在 docs/uiux/screenshots/agent-workbench/ 下。
- PR 描述包含：改了什么、为什么、如何验证、遗留问题。完成后在 docs/agent-runtime/implementation-plan.md 中勾选对应条目。
```

---

## E0 · 前端基础：类型、接口、Store、事件订阅、单测框架

```text
任务：
1. 单测框架（仅纯逻辑）：
   - devDependencies 新增 vitest（与 Vite 6 兼容的版本）；package.json 增加 "test": "vitest run"；
     vite.config.ts 或独立 vitest.config.ts 中配置 @ 别名与 environment: 'node'（本阶段不做组件挂载测试，不引入 jsdom / @vue/test-utils）。
   - .github/workflows/frontend-checks.yml 在"类型检查与构建"之前增加一步 npm run test。
   - 更新 package-lock.json（CI 使用 npm ci）。
2. 类型与接口：新建 src/api/agentGoal.ts
   - 类型 AgentGoal、AgentPlan、AgentPlanStep、GoalStatusResponse、GoalBudget、LifecycleState、StepType、StepStatus，
     字段名以后端实际返回为准（先阅读 backend 对应接口和 docs/API.md，在文件头注释中写明对应的后端函数）。
   - 函数 getActiveGoal(sessionId)、listGoals(sessionId, page)、getGoal(goalId)、pauseGoal、resumeGoal、cancelGoal(goalId, reason?)、
     updateGoalBudget(goalId, body)，以及 E1 新增的接口（先写好签名，E1 合并后联调）。
   - api/index.ts 中导出。
3. 事件常量：api/workspace.ts::WORKSPACE_EVENT_TYPES 增加 agent.* 与 review.* 共 9 个类型，与后端白名单逐字一致；
   新增一个单测，读取后端 backend/app/services/workspace_event.py 的 EVENT_TYPES（用正则解析字符串字面量），
   断言前后端集合一致——防止以后有一边漏改。
4. 状态映射（纯函数，放在 src/components/agent/agentStatus.ts）：
   - lifecycleView(state) → { label, tagClass, pulse, isTerminal, isWaiting }，例如
     waiting_approval → "等待你审批"，waiting_user → "需要你处理"，waiting_job → "生成中"，partially_completed → "部分完成"；
   - stepView(step) → { label, tagClass, icon, actionable }，step_type=stage_expand 显示"待展开"、track_job 显示"跟踪已有任务"、
     review_gate 显示"等待审核"、clarification 显示"需要你确认"；
   - errorView(error_code, result) → 人话说明 + 建议操作类型（retry / go_script / raise_budget / review / keep_or_regenerate / none）。
     error_code 的映射写成数据表，覆盖 Phase A–D 中出现过的所有错误码（在各阶段提示词与后端代码中 grep error_code= 收集，表头注释写来源）；
     未知错误码显示"执行失败（错误码 xxx）"，不要吞掉。
   - 单测覆盖所有枚举值和至少 15 个错误码。
5. Store：新建 src/stores/agentGoal.ts（Pinia，setup 写法，与 stores/agent.ts 风格一致）
   - 状态：bySession: Record<sessionId, GoalStatusResponse | null>、byId: Record<goalId, GoalStatusResponse>、loading / error 标记、
     historyBySession（分页）。
   - 动作：refreshActive(sessionId)、refreshGoal(goalId)、loadHistory(sessionId, page)、pause / resume / cancel / updateBudget（成功后立即 refreshGoal）。
   - 事件联动：store 初始化时通过 onWorkspaceEvent 订阅；收到 agent.* 事件时，按事件中的 goal_id / session_id
     对相关目标做**节流刷新**（同一个 goal 300ms 内最多刷新一次，合并突发事件）；收到 review.* 事件时，刷新该项目下 lifecycle_state=waiting_approval 的目标。
     只刷新当前已加载过的会话或目标，不主动拉取无关数据。
   - 断线恢复：useWorkspaceEvents 的 connected 从 false 变为 true 时，刷新所有已加载的非终态目标（补齐期间可能错过事件）。
   - 页面可见性：document.visibilitychange 变为 visible 时同样刷新一次非终态目标（浏览器后台时 SSE 可能被挂起）。
   - 单测：节流合并、终态目标不刷新、事件中的 goal_id 不在已加载集合时不请求（用 vi.mock 模拟 api 模块）。
6. 不修改任何现有组件。

验收：npm run test 通过；CI 四项检查和构建通过；PR 中列出错误码映射表的来源清单。
```

---

## E1 · 后端小补充：前端需要但现在缺少的接口

```text
任务（后端，backend/）：先对照 E2–E6 的需求逐项确认现有接口是否已满足，**已满足的不要重复实现**，在 PR 中列出对照结果。

1. 计划历史：GET /api/agent/goals/{goal_id}/plans → 所有 revision 的摘要（plan_id、revision、status、parent_plan_id、reason、created_at、步骤数统计）；
   GET /api/agent/goals/{goal_id}/plans/{plan_id} → 该 revision 的完整步骤列表（只读）。
2. 阶段进度：目标状态接口增加 stage_progress 字段，来自 Phase C 的 production_dag 阶段判定：
   [{stage, label, total, done, generating, failed, reviewed?, complete_as_target, satisfied_for_downstream}]，
   只包含从起点到 goal_spec.target_stage 的阶段。数据全部来自判定函数，不自己计算。
3. 单步重试：POST /api/agent/goals/{goal_id}/steps/{step_id}/retry
   - 只允许 status ∈ {failed, waiting_user} 且 error_code 不属于"确定性错误、重试无意义"的步骤
     （复用 Replanner 的错误码映射表判断；不允许时返回 409 并说明原因）；
   - 行为：重置为 pending、retry_count+1、写执行日志（trigger=manual），然后调用一次 tick；遵守 Goal 预算。
4. 调整目标：POST /api/agent/goals/{goal_id}/spec  body: GoalSpec 的部分字段（target_stage、scope、stop_before）
   - 服务端用 Phase C 的 GoalSpec 模型校验；指纹变化时按 Phase A 的规则 supersede 旧目标、创建新目标（在途 Job 由新计划 track）；
     返回新目标状态。不提供"直接编辑计划步骤"的接口（计划由代码根据状态生成，这是已确认的决策）。
5. 等待项汇总：目标状态接口增加 waiting 字段（如 Phase B 的 TickResult 已有，就直接透传）：
   {approvals: [{step_id, confirmation_id, tool_name, title}], jobs: [{step_id, job_id}], user: [{step_id, error_code, message, options?}],
    reviews: [{step_id, stage, count}]}，前端直接用它渲染等待入口。
6. 所有新接口写入 docs/API.md，并补测试 tests/test_agent_phase_e_api.py（重试的允许/拒绝分支、调整目标后的 supersede、计划历史顺序、stage_progress 与判定函数一致）。
```

---

## E2 · GoalCard：当前目标卡片

```text
任务：新建 src/components/agent/GoalCard.vue（只依赖 agentGoal store 与 agentStatus 映射，不直接调接口）。

1. Props：goalId（或 sessionId，二选一），compact?: boolean（侧边助手面板用紧凑版）。
2. 内容：
   - 标题行：目标描述（goal_spec 的中文化描述，例如"完成分镜视频（第 1–2 集）"；描述由 target_stage + scope 拼出，映射表放在 agentStatus.ts）、
     lifecycle 状态标签（.status-tag + 文字；waiting_job / running 时 pulse）、关联项目名（可点击，经 workspaceNavigation 跳转到项目）。
   - 进度区：按 E8 的规则显示三类进度（计划步骤、生成任务、产物），不显示伪造的百分比。
   - 预算：显示 budget_usage 与上限（例如"已创建生成任务 18 / 200"）；接近上限（≥80%）时用警示令牌提示；
     lifecycle=waiting_user 且 error_code=budget_exceeded 时，在卡片内提供"提高上限"表单（数字输入 + 确认）。
   - 控制按钮：暂停 / 继续 / 取消。取消用 useConfirmDialog 二次确认，弹窗中说明"正在生成的任务会被取消，已完成的结果会保留"，
     可填写原因。按钮按 lifecycle 显示或禁用（终态不显示控制按钮）；请求进行中显示 loading，失败用 message.error 显示后端返回的错误信息。
   - 调整目标：一个"调整目标"按钮，打开小表单（目标阶段下拉、集数范围、"做到某阶段为止"），提交到 E1 的 spec 接口；
     提交前提示"这会生成新的计划，已完成的部分不会重做"。
3. 空状态：会话没有目标时组件不渲染（由父组件决定是否显示提示）。
4. 可访问性：卡片使用 <section aria-label="当前目标">，按钮都有可读文本；状态变化区域使用 aria-live="polite"（只播报 lifecycle 变化，不播报每次刷新）。

验收：在 E5 / E6 中挂载后截图；单测覆盖"按 lifecycle 决定显示哪些按钮"的纯函数（把判断抽到 agentStatus.ts）。
```

---

## E3 · PlanSteps：计划步骤列表

```text
任务：新建 src/components/agent/PlanSteps.vue。

1. 数据：当前计划的 steps（来自 store）；可切换查看历史 revision（E1 的计划历史接口，只读，按需加载，不放进全局轮询）。
2. 分组展示：按阶段分组（剧本 / 解析 / 确认解析 / 资产 / 分镜图 / 分镜视频 / 合成 / 导出），阶段标题显示该阶段的步骤统计；
   阶段内步骤超过 8 条时默认折叠，只显示未完成的步骤和"展开全部"。
3. 每个步骤一行：状态标签（stepView）、描述（实体名或"第 N 集 第 M 镜"——从步骤 args / result 中取，后端没有提供名称的话在 E1 中补充）、
   关联信息（job_id 关联的进度条，见 E8；confirmation_id 关联的审批入口，见 E4）、失败原因（errorView）。
   - stage_expand：显示为"待前面的阶段完成后展开"，不显示操作；
   - track_job：显示"跟踪已有任务"，并显示该 Job 的进度；
   - carried_over（result 中有 carried_over_from）：显示"沿用上一版计划的结果"。
4. 失败步骤的操作：errorView 给出 retry 时显示"重试"按钮（调用 E1 的重试接口，确认后执行）；后端返回 409 时显示原因。
5. Revision 切换：顶部显示"第 N 版计划"及生成原因（failure / expansion / upstream_version_changed / user_goal_changed 映射为中文），
   历史版本以只读样式展示并标明"已被替换"。
6. 性能：步骤可能上百条（批量资产、全部镜头），列表使用简单的分组折叠即可；如果单组超过 100 条，使用 Naive UI 的虚拟列表（NVirtualList）。

验收：用 E10 的模拟数据渲染 3 种计划（小、带历史版本、200 步）截图；单测覆盖分组、折叠、名称提取函数。
```

---

## E4 · 等待入口：审批、生成任务、需要你处理、审核

```text
任务：新建 src/components/agent/GoalWaitingPanel.vue，根据 E1 的 waiting 字段显示"现在需要你做什么"。放在 GoalCard 下方，有等待项时才显示。

1. 审批（waiting.approvals）：
   - 复用现有审批数据与动作（stores/agent.ts 的 resolveApproval 等），不要新写一套审批请求。
   - 对 Runtime 步骤绑定的审批只显示"批准 / 拒绝"，**不显示"执行"**：批准后由 Runtime 自动执行（Phase B 已保证即使用户点了执行也不会重复）。
     在现有 ApprovalList 与 DirectorAgentPanel 的审批列表中，同样对这类审批隐藏"执行"按钮，并显示提示"批准后 Agent 会自动执行"
     （判断依据：审批的 confirmation_id 出现在任一已加载目标的 waiting.approvals 中；把判断写成 store getter）。
   - 多条审批时提供"全部批准"（逐条调用现有接口，显示进度和失败项；不新增批量接口）。
2. 生成任务（waiting.jobs）：显示数量和一条汇总进度（E8）；"查看任务"跳转到现有任务卡片位置或任务详情。
3. 需要你处理（waiting.user）：逐条显示 errorView 的说明，并按建议操作类型提供按钮：
   - go_script：跳转到剧本页（workspaceNavigation）；
   - raise_budget：打开 GoalCard 中的提高上限表单；
   - keep_or_regenerate（分镜图审核过期，Phase D）：两个按钮"保留并重新审核"（跳到分镜图审核队列并筛选这些镜头）/
     "重新生成这些分镜图"（通过对话发送一条固定格式的指令，或调用后端已有的接口——先确认 Phase D 的实现方式，选择已存在的那一种）；
   - review：跳转到审核队列；
   - 其他：显示"在对话中告诉 Agent 怎么处理"，并把输入框聚焦。
4. 审核（waiting.reviews）：显示"待审核：角色/场景/道具 N 项"或"分镜图 N 张"，按钮跳转到 Phase D 的审核位置
   （资产库 / 分镜图页，带上 stage 与待审筛选参数；如 Phase D 的页面不支持筛选参数，在本任务中补上路由 query 参数的读取）。
   同时提供"快速审核"抽屉：调用审核队列接口显示缩略图、版本号、上一轮意见，支持逐条或全选通过、驳回（必须填写意见），
   提交时带 expected_version；版本已变化导致失败时，提示"该项已有新版本"并刷新队列。
5. 所有操作完成后只调用 store 刷新目标状态，不在前端自行修改步骤状态。

验收：截图覆盖四类等待；手动验证"审批后自动继续"（不需要点执行）、"快速审核驳回必须填写意见"、版本变化提示。
```

---

## E5 · 接入 /director 导演台

```text
任务：在 views/DirectorConsoleView.vue 中接入 E2–E4，并让消息流中的 Agent 消息和目标状态联动。

1. 布局：在右栏 ContextSidebar 的最上方新增"当前目标"区块（GoalCard + GoalWaitingPanel + 可折叠的 PlanSteps），
   会话没有目标时不显示该区块。不改动右栏现有的项目、审批、审核区块；但当前目标的审批已在目标区块中显示时，
   审批区块中这些审批显示"见上方目标"的标记（避免用户以为是两件事）。
   断点：≤1100px 时右栏按现有行为退让；目标区块在 ≤768px 时移到消息流顶部，以可折叠横条形式显示（只显示目标描述、状态、等待数量）。
2. 消息流：MessageStream 中 payload.route_action === "agent_runtime" 或 payload.source === "agent_runtime" 的助手消息，
   在文字下方渲染紧凑的 GoalProgressCard（新建，内容来自 store 的实时状态，而不是消息中的快照；
   如果该目标已被 superseded，显示"该目标已被新的目标替代"并提供"查看当前目标"）。
   同一目标的多条消息只有最新一条显示进度卡片，较早的消息只显示文字（避免刷屏）。
3. 目标的统一任务：goal.task_id 的任务卡片已经由现有 TaskCard 机制渲染时，不要再重复显示一份 Job 进度；
   GoalProgressCard 中只显示汇总，点击展开时复用 TaskCard。
4. 切换会话：调用 agentGoal store 的 refreshActive；切换前一个会话的目标数据保留在 store 中（再次切换回来时先显示缓存再刷新）。
5. 本地状态收敛（可选但推荐）：DirectorConsoleView 中重复的 approvals / reviewCards 本地状态如果可以安全地改为读取 stores/agent.ts，
   在本任务中完成；如有风险，在 PR 中说明并留作遗留问题，不要强行修改。

验收：截图（三档）；手动验证：发送"帮我生成这个项目所有角色资产" → 消息下出现进度卡片，右栏出现目标区块 → 审批后状态自动变化，不需要刷新页面。
```

---

## E6 · 会话侧栏与历史任务

```text
任务：
1. SessionRail：每个会话条目在有非终态目标时显示一个小的状态标记（lifecycle 的圆点 + 简短文字，例如"等待审批"），
   数据来自会话列表接口——如果后端会话列表没有目标摘要字段，在 E1 中补充 active_goal_summary（goal_id、lifecycle_state、target_stage），
   不要为每个会话单独请求目标接口。
2. 历史任务：新建 src/components/agent/GoalHistory.vue，在右栏目标区块中提供"历史目标"入口（抽屉）：
   - 列出该会话的所有目标（分页），显示描述、状态、开始和结束时间、计划版本数；
   - 点击某个历史目标查看其最终计划（PlanSteps 只读）和执行日志摘要；
   - 对 lifecycle ∈ {paused, waiting_user, partially_completed, failed} 且 goal.status 仍为 active 的目标提供"继续"按钮
     （paused → resume 接口；其他 → 在对话中发送"继续上次的任务"并切换到该会话，或调用重试接口——按 E1 的能力选择）。
   - superseded / cancelled 的目标只读。
3. 页面刷新恢复：进入 /director 时，如果当前会话有非终态目标，自动展开目标区块；不需要用户再问"进度怎么样"。

验收：截图；手动验证"刷新页面后目标区块和步骤状态与刷新前一致"、"暂停后在历史中继续"。
```

---

## E7 · 侧边助手面板与项目切换

```text
任务：
1. DirectorAgentPanel.vue（全局右侧面板）：在消息列表上方加入紧凑版 GoalCard（compact）和等待数量徽标，
   点击展开 GoalWaitingPanel；完整计划不在面板中显示，提供"在导演台查看"链接（workspaceNavigation 跳到 /director 并选中该会话）。
   面板宽度最小 300px，紧凑版卡片在此宽度下不得横向溢出（截图验证）。
2. 项目切换：
   - 面板当前会话绑定的项目与用户正在浏览的项目页面不同时，GoalCard 顶部显示"该目标属于项目 X"，不要把目标状态显示得像是当前项目的；
   - 在项目页面中（/project/:id/...），面板优先显示与该项目关联的会话的目标（如果存在）；
   - 不同项目的目标数据在 store 中按 goal_id / session_id 隔离，切换项目不清空、也不串用。
3. 项目页面上的提示：资产库、分镜图、分镜看板页面顶部，当该项目存在非终态目标时，显示一条细横幅
   （"Agent 正在为该项目生成分镜图 · 等待你审核 12 张"），点击打开助手面板；用户关闭横幅后在本次会话内不再显示。

验收：截图覆盖面板最小宽度、跨项目提示、项目页横幅；手动验证在两个项目之间切换时状态不串用。
```

---

## E8 · 进度显示规则

```text
任务：把进度相关的计算全部放到 src/components/agent/agentProgress.ts（纯函数 + 单测），各组件只调用它。

1. 三类进度，分开显示，不合并成一个百分比：
   - 计划进度：已结束步骤 / 步骤总数，其中 stage_expand 占位步骤不计入总数，并注明"后续阶段待展开"；
     这是步骤计数，不是工作量，文案写"计划步骤 12 / 30"，不写百分比。
   - 生成任务进度：来自统一任务的 TaskStage（useTaskEvents 的 taskSnapshots[goal.task_id]）：排队 / 生成中 / 成功 / 失败的数量；
     单个 Job 的百分比只在后端事件提供 progress 时显示（与 TaskCard 一致），没有就显示不确定进度条。
   - 产物进度：来自 E1 的 stage_progress，例如"角色 3/3 · 场景 5/5 · 分镜图 18/26（已审 10）"。这是最能代表真实完成度的指标，放在最显眼的位置。
2. 不显示剩余时间、预计完成时间（后端没有可靠数据）。
3. 终态的文案：completed 时显示产物进度的最终数量；partially_completed 时明确列出未完成的数量；failed 时显示失败的步骤数和第一个失败原因。
4. 单测：各种组合（没有 Job、Job 无 progress 字段、存在占位步骤、部分失败、审核计数）下的输出。
```

---

## E9 · 响应式、可访问性与设计门禁

```text
任务：对 E2–E8 新增的所有组件做一次集中检查和修正。
1. 三档断点（1100 / 768 / 480）下的布局：无横向溢出、按钮不被裁切、文字可换行；手机宽度下 PlanSteps 每行改为两行显示（状态 + 描述 / 操作）。
2. 键盘可达：Tab 顺序合理；抽屉、弹窗打开后焦点进入、关闭后焦点回到触发按钮；Esc 关闭。
3. 读屏：状态标签有文字；图标按钮有 aria-label；进度条有 aria-valuenow / aria-valuetext（不确定进度用 aria-busy）。
4. 减少动效：prefers-reduced-motion 下关闭 pulse 和入场动画。
5. 设计门禁：三项 strict 检查通过；不新增令牌。如果确实需要新的语义令牌（例如"需要你处理"的提示色），
   先在 DESIGN.md 和 global.css 中按规范添加并说明理由，再使用。
6. 新增 frontend/scripts/verify-agent-workbench.mjs（参照现有 verify-*.mjs 的写法，使用 playwright-core）：
   在三档视口下打开 /director 和一个项目页，检查目标区块存在、无横向溢出、所有按钮可见区域 ≥28×28，并输出截图到 docs/uiux/screenshots/agent-workbench/。
```

---

## E10 · 验收：模拟数据与端到端检查（Phase E 验收门禁）

```text
任务：
1. 模拟数据：新增 frontend/src/components/agent/__fixtures__/（只在单测和开发预览中使用，不打包进生产代码），
   包含以下场景的目标状态 JSON：等待审批、生成中、等待审核、预算超限、分镜图审核过期、部分完成、失败、已完成、已被替代、200 步大计划、带 3 个历史版本。
   字段必须与 E1 合并后的真实接口一致——写一个后端测试导出真实接口返回的样例（tests/test_agent_phase_e_api.py 中生成），
   前端单测断言 fixtures 的键集合与样例一致，防止前后端字段漂移。
2. 单测：用 fixtures 跑通 agentStatus / agentProgress / store 的所有分支。
3. 端到端手动验收清单（写进 docs/agent-runtime/phase-e-acceptance.md，逐项附截图或录屏）：
   a. 刷新页面：目标、计划、等待项与刷新前一致；
   b. 切换会话、切换项目：状态不串用；
   c. 重启后端：页面重新连上事件流后自动刷新，状态与数据库一致；
   d. 断网 30 秒后恢复：错过的事件被补齐，界面最终一致；
   e. 审批后自动继续：不需要点"执行"，也不需要刷新；
   f. 失败步骤一键重试；确定性错误不显示重试按钮；
   g. 暂停、继续、取消；取消后正在生成的任务被取消，已完成的结果保留；
   h. 调整目标后生成新计划，已完成的部分不重做；
   i. 预算超限后提高上限继续；
   j. 分镜图审核过期时的"保留 / 重新生成"两种选择；
   k. 开关 agent_runtime_chat_enabled 关闭时，页面与改动前一致（与改动前的截图对比）；
   l. 侧边助手面板在 300px 宽度下可用；手机宽度可用。
4. 文档：docs/FRONTEND.md 增加"Agent 工作台"一节（组件结构、数据流：接口为准、事件只触发刷新），
   docs/agent-runtime/implementation-plan.md 勾选 E0–E10。

完成标准：CI 全部通过；验收清单全部完成并附证据；遗留问题列入 Phase F 输入。
```

---

## 附：Phase E 完成后的状态（供 Phase F 衔接）

| 已具备 | 仍缺（Phase F） |
|---|---|
| 导演台和侧边面板都能看到当前目标、计划、等待项 | Memory 检索接入上下文、经验记忆只来自验证过的结果 |
| 审批、审核、需要你处理的事项都有直接入口，审批后自动继续 | 指标统计接口与看板（完成率、重试率、用户介入率等） |
| 进度分为计划 / 生成任务 / 产物三类，不伪造百分比 | 原始 Plan 的 20 个场景落成自动化测试并进 CI |
| 刷新、切换会话或项目、重启后端、断网恢复后状态一致 | 重复文档归档、根目录临时脚本清理、README 更新 |
| 历史目标、计划版本、暂停 / 继续 / 取消 / 调整目标 / 单步重试 | |
| 前端纯逻辑单测框架（vitest）进入 CI | |
