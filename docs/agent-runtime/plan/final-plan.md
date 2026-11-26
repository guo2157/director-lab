# Director-Lab Agent Runtime 升级 · 最终实施 Plan（基于最新代码修订版）

> 修订日期：2026-11-26（v1.6：§六 决策已确认；Phase A–F 已全部细化为编码 Agent 提示词，详见 phase-a~f-agent-prompts.md）
> 审查基线：`guo2157/director-lab` 默认分支 HEAD（tree `1df9fe8b`）
> 输入：原始升级 Plan（readme.txt）＋ 仓库内 `docs/agent-runtime/*`（2026-11-25 Phase 0 产出）＋ 实际代码
> 一句话结论：**原 Plan 方向正确，但已经过时。** Phase 0 已完成，Phase 1/2 的骨架代码也已入库；当前真正的问题不是"缺模块"，而是 **新 Runtime 是一座没接上主链路的孤岛，而且存在多处 P0 级缺陷**。最终 Plan 应从"新建 Runtime"改为"**修正 → 接入 → 收敛 → 再扩展**"。

---

## 一、原 Plan 本身的问题

| # | 问题 | 说明 | 修订处理 |
|---|---|---|---|
| 1 | **基线过时** | Plan 要求"新建" Goal Manager / Context Resolver / Constraint Manager / Planner / Executor / Verifier / Replanner，但这些文件已经在 `backend/app/services/` 中，迁移 `g1a2b3c4d5e6` 也已创建 `agent_goal / agent_plan / agent_execution_log`。照原 Plan 执行，会让编码 Agent 再造一套。 | 改为"审计现有实现 → 修缺陷 → 接入" |
| 2 | **缺 MVP 与优先级** | 有 32 条最终验收、20 个场景、6 个 Phase，但没有定义"最小可用闭环"，所有事项权重一样。 | 定义 MVP 闭环（见 §四 Phase B 验收） |
| 3 | **权限体系过度设计** | README 写明"多用户 / 权限体系：未开始，单用户自托管"。Plan 中 User / Project / Task / Approval 四层权限加 Critical 分级，目前没有落地对象。 | 收敛为：**工具风险分级 + 服务端审批门禁 + 项目隔离**；多用户留作后续 |
| 4 | **与"禁止重复建设"自相矛盾** | Plan 一边禁止重复建设 Task/Job/Event，一边建议新增 AgentEvent / AgentVerification / AgentExecutionLog。仓库里已经有 `job_event`、`task_event`、`workspace_event` 三套 outbox（全局 seq）。 | **不新建 agent_event 表**，在 `task_event` / `workspace_event` 白名单中增加 `agent.*` 事件类型；verification 结果写进 `agent_plan_step.verification` |
| 5 | **Step 与 TaskStage 双重记账** | Plan 同时要求 AgentPlanStep 和 Task/TaskStage 表示"计划步骤"。现有代码中每个 step 都会再创建一个 Task + TaskStage（`execution_manager._map_step_to_task`），同一件事有两份状态。 | 明确单一事实源：**AgentPlanStep = 计划状态；Task/TaskStage/Job = 生产执行**，只有产生 Job 的 step 才挂 TaskStage |
| 6 | **"动态规划"没有说明怎么实现** | 没说 Planner 用 LLM 还是规则。这个项目的生产流程本身就是一张固定 DAG（剧本 → 解析 → 资产 → 审核 → 分镜图 → 审核 → 视频 → 音频 → 合成 → 导出），并且已有 `pipeline_state.derive_progress`、`stage_readiness` 作为权威判定。 | **状态驱动的确定性 Planner**：用"目标阶段"减去"当前真实状态"，得到剩余 DAG；LLM 只负责把用户话解析成 Goal（目标阶段 / 范围 / 约束） |
| 7 | **没有定义谁来驱动 Loop** | 要求"不依赖 HTTP 请求、禁止无限轮询、服务重启可恢复"，但没说明由什么进程推进执行。 | 由 **Worker 进程**驱动：`bridge_job_event` / 审批裁决写入"恢复请求"，scheduler 消费后推进一步（§四 Phase B） |
| 8 | **Human Gate 和现状冲突** | 审计结论 P0-2：资产 / 分镜审核闸口在 2026-11 已被**有意移除**；Plan 要求强制恢复，还新增了"视频审核"。这需要产品决策，不能直接强推。 | 改为**项目级审核策略开关**（`review_policy`），新项目默认开启、存量项目默认关闭；视频审核放后续 |
| 9 | **事务与并发没有约束** | 没有提 SQLite WAL + API/Worker 并发写的事务纪律。现有 Runtime 代码因此出现"写了但不提交"的问题（见 §二 P0-1）。 | 新增"事务边界"硬约束，并纳入验收 |
| 10 | **成本控制对象错误** | 渲染全部走自建 ComfyUI，没有按秒计费。"Model Cost / Job Cost"没有实际意义。 | 预算对象改为：LLM tokens、生成 Job 数、GPU 时长估算、单 Goal 最长运行时间 |
| 11 | **测试体系没有可执行框架** | 列了 20 个场景，但没要求隔离 DB 和 Mock ComfyUI/LLM。现有 `test_agent_loop_main.py` 直接连生产库。 | 要求 pytest + 临时 SQLite + `mock_render` + LLM stub |
| 12 | **文档膨胀** | `docs/` 下已有 `agent/`、`agent-runtime/`、`agent-*.md`、`refactor/` 等十几份重叠的审计和计划文档（其中一份 136KB）。原 Plan 又要求新增 6 份。 | 收敛为 `docs/agent-runtime/` 一处，旧文档归档到 `docs/_archive/` |

---

## 二、最新代码审计结论（以代码为依据）

### 2.1 现状总览

| 模块 | 文件 | 状态 | 结论 |
|---|---|---|---|
| 主对话链路 | `api/v1/agent.py`（`/{session_id}/chat`，约 2100 行） | 生产在用 | 完全**没有**引用 goal_manager / planner / agent_runtime，仍是 意图路由 → 工具调用 → 审批卡片 |
| Runtime 入口 | `services/agent_runtime.py` | 已入库 | 有 Loop、日志、resume 函数，但有 P0 缺陷，且没有被主链路调用 |
| Loop API | `api/v1/agent_loop.py`（`POST /api/agent/loop/run`） | 已注册 | 前端**没有任何调用**（`frontend/src` 中没有 `loop`、`goal_id`、`plan_id`） |
| Goal / Plan 表 | migration `g1a2b3c4d5e6` | 已建 | 只有 agent_goal / agent_plan / agent_execution_log；没有 event / verification 表（这是好事，见 §一-4） |
| Planner | `services/planner.py` | 关键词规则 | 不读 ContextSnapshot，生成计划和项目真实状态无关 |
| Executor | `services/execution_manager.py` | 已入库 | 绕过工具网关，不区分同步和异步 |
| Verifier | `services/verifier.py` | 已入库 | 状态检查恒为 passed，失败步骤也会被判"验证通过" |
| Replanner | `services/replanner.py` | 已入库 | 重规划生成的是同一份计划，revision 不递增 |
| Job 桥接 | `task_orchestrator.bridge_job_event / bridge_approval_resolved` | 生产在用 | **没有**回调 `resume_after_job / resume_after_approval` |
| 工具网关 | `agent_tools.execute_tool` | 生产在用 | 链路完整：审批 → 前置条件 → Schema → 执行 → 分类错误。**这是应复用的唯一入口** |
| 事件 | `job_event` / `task_event` / `workspace_event` outbox | 生产在用 | 已具备全局 seq 和增量拉取，足以承载 agent 事件 |

### 2.2 P0 缺陷清单（必须先修）

| ID | 缺陷 | 证据 | 后果 |
|---|---|---|---|
| **P0-1** | Runtime 全程只 `flush` 不 `commit`；`get_db()` 结束时只 `close()` | `core/database.py:get_db`；agent_runtime / planner / execution_manager / replanner / goal_manager 中没有任何 `commit()` | 经 API 调用时，Goal / Plan / Step / 日志在请求结束后**全部回滚**。"持久化、可恢复"实际上不成立 |
| **P0-2** | 请求作用域的 `db` Session 被传进新线程（`threading.Thread(asyncio.run(...))`） | `agent_loop.py:132-162` | Session 跨线程使用不安全；请求结束后 Session 被关闭，后台线程会读写已关闭的会话 |
| **P0-3** | 后台线程里调用 `asyncio.get_event_loop()`，拿到的是**子线程自己的 loop**，再对主 loop 的 Queue 执行 `call_soon_threadsafe` | `agent_loop.py:86` | SSE 事件可能丢失或无法唤醒，前端拿不到进度 |
| **P0-4** | `progress_callback` 没有传给 `_execute_plan_loop`；最终 `_emit` 在 `result.completed_steps` 等字段赋值**之前**调用 | `agent_runtime.py:428-454` | step_started / step_completed 事件从未发出；最终事件中的统计值永远为 0 |
| **P0-5** | Executor 直接调用 `tool_fn(db, **args)`，没有走 `execute_tool` | `execution_manager.py:375-380` | 跳过了 input_model 校验、preconditions、错误分类，**违反"工具执行必须经过业务网关"** |
| **P0-6** | 工具一返回就标记 `succeeded`，从不进入 `waiting_job` | `execution_manager.py:395` | 正是原 Plan 明令禁止的"Job 创建成功 = 完成" |
| **P0-7** | Verifier 的 `step_status` 检查无条件 `passed` | `verifier.py:157-165` | 工具抛错导致 step=failed，依然可能被判验证通过并计入 completed |
| **P0-8** | Plan 中的 `step.requires_approval` 被忽略，只看工具 spec；`resume_after_approval` 把单次 `approved` 传给整个后续循环 | `execution_manager.py:314-315`；`agent_runtime.py:824-831` | 一次审批会放行后续所有高风险步骤，审批粒度失效 |
| **P0-9** | `resume_after_job / resume_after_approval` 没有任何调用方；`resume_after_job` 也只做验证、不推进下一步 | grep 全仓库 | 进入 waiting_job / waiting_approval 后**永远卡住** |
| **P0-10** | 同一 session 复用活跃 Goal 时会覆盖 description，但 `_get_or_create_plan` 直接复用旧 Plan | `goal_manager.get_or_create_goal`；`agent_runtime.py:491-493` | 用户换了新目标，系统仍执行旧计划（测试场景 2 还把这一点当成"期望行为"断言） |
| **P0-11** | Replan 调用 `build_and_save_plan` 时没有传 `previous_plan`，revision 恒为 1；描述只加了前缀，关键词匹配结果不变 | `replanner.py:254-259`；`planner.py:636` | 重规划得到一模一样的计划，失败会重复 3 次后才终止 |
| **P0-12** | `GET /api/agent/loop/status/{goal_id}` 调用的是 `get_active_goal(db, goal_id)`，而该函数的参数是 session_id | `agent_loop.py:184` | 接口恒返回 404 |
| **P0-13** | `test_agent_loop_main.py` 在模块级直接连接 `./data/director_lab.db` 执行 | `tests/test_agent_loop_main.py:30` | pytest 收集时会污染真实数据，而且不是断言式测试 |

### 2.3 P1 问题

- Planner 只按关键词匹配，生成类计划只有"概览 + 一个 request_generate"，参数缺少 `shot_id/entity_id`，必然被前置条件或 Schema 拒绝（`planner.py:238-332`）。
- `request_generate` 只会创建 AgentConfirmation，Plan 中没有对应的 `run_generate` 步骤，也没有把审批 ID 和 step 绑定。
- 执行锁是"读后写"，不是条件 UPDATE；API 与 Worker 并发时存在竞态（`execution_manager.py:88-114`）。
- `check_goal_completion` 只看 plan.status，而 plan.status 从未被置为 completed；Goal 完成与 success_criteria、项目真实状态都无关。
- `agent_plan_step.task_id` 写入空字符串（`planner.py:609`），该字段在新旧两种语义下含义不同。
- 状态词表不统一：同时存在 `succeeded` 和 `completed`（`ExecutionStatus`）。
- 失败步骤之后，如果没有可执行步骤，就返回 `completed / 无可执行步骤`；没有 `partially_completed`。
- 根目录有 10+ 个 `_probe_*.py`、`_rebuild_prj_*.py` 临时脚本入库。

### 2.4 可直接复用的资产

`agent_tools.execute_tool`（网关）、`TOOL_REGISTRY`（29 个工具，已有 risk / approval / preconditions / input_model）、`approvals.py`（审批与执行分离、入参指纹幂等）、`task_orchestrator`（DAG、job→stage 桥接、对账）、`pipeline_state.derive_progress`、`stage_readiness`、`assets_gate`、`parse_gate`、`dependency_impact`（版本失效分析）、三套 outbox、`ToolBudget`、`AgentMemory/SessionMemory`、Worker 启动恢复与对账。

---

## 三、最终目标架构（收敛版）

```
用户消息 ──► /{session_id}/chat（唯一入口，保留现有 SSE）
              │
              ├─ Intent Router（保留快路径；只产出意图，不决定流程）
              ├─ Goal Manager：意图 → Goal（目标阶段 / 范围 / 约束）；新目标才建新 Goal
              │
              └─ Agent Runtime.tick(goal)  ← 也可由 Worker 事件触发
                   ├─ Context Resolver：derive_progress + stage_readiness（只读真实状态）
                   ├─ Planner（确定性）：目标阶段 − 当前状态 = 剩余 DAG → Plan revision
                   ├─ Plan Validator：工具存在 / 参数 / 循环 / 门禁 / 预算
                   ├─ Executor：只经 agent_tools.execute_tool
                   │     ├─ 同步工具 → Verifier → completed / failed
                   │     ├─ 产生 Job → waiting_job（挂 TaskStage）
                   │     └─ 需审批 → 建 AgentConfirmation → waiting_approval（step↔confirmation 绑定）
                   ├─ Verifier：以执行结果 + DB 真实状态为准
                   └─ Replanner：错误分类 → retry / 基于新状态重算 Plan / wait_user / fail
                                │
Worker（scheduler）◄────────────┘
   bridge_job_event / 审批裁决 → 写 agent.resume_requested 事件 → scheduler 消费 → Runtime.tick（单步推进，幂等）
事件：task_event / workspace_event 新增 agent.* 类型 → 前端增量拉取
```

**核心原则**：每次 `tick` 只推进到下一个等待点就提交并返回；任何进程（API、Worker）都可以安全地重复调用 `tick`（幂等、加锁）。这样同时满足"不依赖 HTTP 请求"、"服务重启可恢复"和"不无限轮询"。

---

## 四、最终实施阶段

> 规模估计：S ≤ 1 天，M 2–4 天，L ≥ 1 周（单人＋编码 Agent）。每个 Phase 必须通过验收才能进入下一个。

### Phase A · 止血与正确性（M，最高优先级）

目标：让现有 Runtime 在被接入之前**本身是正确的**。

| 任务 | 修改点 | 对应缺陷 |
|---|---|---|
| A1 事务边界：Runtime 对外入口（`tick` / `run_agent_loop` / resume）内部自建 `SessionLocal()`，每推进一步 `commit`，异常时 `rollback`；内部函数仍只 `flush` | agent_runtime、agent_loop | P0-1, P0-2 |
| A2 SSE 桥接：在主线程取 `loop = asyncio.get_running_loop()` 传入回调；后台改用 `asyncio.to_thread` 或线程池执行，不再 `asyncio.run` | agent_loop.py | P0-3 |
| A3 把 `progress_callback` 透传到 `_execute_plan_loop`，并把最终 `_emit` 移到结果赋值之后 | agent_runtime.py | P0-4 |
| A4 Executor 改为只调用 `agent_tools.execute_tool(db, name, approved=..., **args)`，按 `ToolResult.ok / error_code` 分支处理；删除直接调用 fn 的代码 | execution_manager.py | P0-5 |
| A5 结果分流：返回值含 `job_id` → step=`waiting_job`，`attach_job_stage` 关联 TaskStage；`error_code=approval_required` → `waiting_approval` | execution_manager.py | P0-6 |
| A6 Verifier：step.status ∉ {succeeded} 或 ToolResult.ok=False 时判 failed；含 job_id 时必须等 Job 终态 succeeded 才通过 | verifier.py | P0-7 |
| A7 审批粒度：`approved` 不再作为循环参数，改为按 step 查已批准且未执行的 AgentConfirmation；Plan 中 `requires_approval` 与工具 spec 取 OR | execution_manager、agent_runtime | P0-8 |
| A8 Goal / Plan 匹配：Goal 增加 `target_stage + scope` 指纹；指纹变化时新建 Goal（旧 Goal 置为 superseded），不覆盖 description | goal_manager、agent_runtime | P0-10 |
| A9 Replan 传入 `previous_plan`，revision 递增，复用已完成且仍有效的步骤 | replanner、planner | P0-11 |
| A10 修复 status 接口（按 goal_id 查询）；统一状态词表，只用 `succeeded`，删除 `COMPLETED` 别名 | agent_loop、execution_manager | P0-12 |
| A11 执行锁改为条件 UPDATE：`UPDATE agent_plan_step SET status='running' WHERE id=? AND status IN ('pending','ready')`，按 rowcount 判断是否拿到锁 | execution_manager | P1 |
| A12 测试改造：`test_agent_loop_main.py` 改为 pytest fixture + 临时 SQLite；新增单测覆盖 P0-1 到 P0-11 | tests/ | P0-13 |

**验收**：所有单测在临时库上通过；用 API 调用 `/loop/run` 后再新开连接查询，Goal / Plan / Step / 日志仍然存在；人为让一个工具失败，step=failed 且不计入 completed；含 job 的步骤停在 waiting_job。

### Phase B · 接入主链路，打通恢复闭环（L）— **MVP**

目标：只保留一条用户入口，实现"Job 完成 / 审批后自动继续"。

| 任务 | 说明 |
|---|---|
| B1 抽出 `agent_runtime.tick(db, goal_id) -> TickResult`：幂等，加 Goal 级锁，推进到下一个等待点就返回 |
| B2 `/{session_id}/chat` 接入：Intent Router 判定为多步流程意图（PIPELINE / generate_request / 明确的"帮我完成…"）时，走 Goal Manager → `tick`；问答、导航类保持原样。**【已决策】`/loop/run` 保留为调试接口**：内部改为调用 `tick`，前端不调用、不在 API 文档中对外暴露，可加 `DEBUG_AGENT_LOOP` 配置开关 |
| B3 Job 恢复：`bridge_job_event` 遇到 Job 终态时，给 `job_id` 匹配、处于 waiting_job 的步骤设置 `resume_requested_at` 标志（同一事务，不同步调用 Runtime） |
| B4 审批恢复：`approvals.resolve_confirmation` 提交前，给 `confirmation_id` 匹配的步骤设置同样的标志；审批通过后由 Runtime 调用幂等的 `run_generate` 建 Job |
| B5 Worker 消费：Worker 内独立的 agent-resume 线程每 2 秒认领带标志的步骤、按 Goal 去重调用 `tick`；每 30 秒兜底扫描（Job 已终态 / 审批已裁决 / tick 中途崩溃），**不需要事件游标** |
| B6 启动对账：Worker 启动时先执行一次兜底扫描 |
| B7 事件：在 `workspace_event` 白名单中增加 `agent.goal.*`、`agent.plan.created`、`agent.step.updated`、`agent.tick.finished`，只供前端展示；**不新建 agent_event 表**（`task_event` 需要 task_id 外键，不适合承载） |
| B8 回复规范：assistant 回复由 TickResult 生成（已完成 / 等待 Job / 等待审批 / 失败原因），禁止把"已提交"说成"已完成" |
| B9 新增 lifecycle：`partially_completed`、`paused`；新增取消接口（同时取消运行中 Job，复用 `job_cancel_ops`） |

**MVP 验收（端到端，使用 mock_render）**：
1. 对话中输入"帮我生成这个项目所有角色资产" → 生成 Plan，提交 N 个生成申请，进入 waiting_approval。
2. 审批通过 → Worker 自动执行 → step 进入 waiting_job → Job 完成后自动 verify，并推进到下一步。
3. 推进过程中重启后端 → 重启后能自动继续，且不重复创建 Job。
4. 刷新页面后，从 API 读到的状态与 DB 一致。
5. 同一请求连续发送两次 → 只有一个 Goal、一组 Job。

### Phase C · 状态驱动的 Planner 与工具层收敛（M–L）

| 任务 | 说明 |
|---|---|
| C0 **统一工具目录**：`agent_tools` / `pipeline_tools` / `regen_tools` 三套注册表各有执行网关，新增门面 `agent_tool_catalog` 统一读取规格和分派执行（不合并实现），补充 execution_mode / produces / verify 元数据 |
| C1 定义生产 DAG 常量（阶段、依赖、门禁、对应工具），以 `pipeline_state` 的 7 个节点为基础 |
| C2 Planner：读取 `derive_progress / stage_readiness / assets_gate`，用目标阶段减去已满足阶段得到剩余步骤；步骤粒度为"阶段 × 单元"（每个缺失资产 / 镜头一条，分镜视频按集串行）。**滚动展开**：解析完成前镜头不存在，所以只有前沿阶段展开为具体单元，后续阶段用占位步骤表示，前置满足后再展开为新 revision |
| C3 **【已决策】Planner 不使用 LLM**：LLM 只做两件事：把用户话解析成 Goal（目标阶段 / 范围 / 约束，输出需经 Schema 校验）；生成解释文案。计划结构完全由代码生成；删除 `build_plan` 中的关键词匹配逻辑 |
| C3b 版本变化检测：计划保存剧本 / 解析 / 资产版本快照，执行生成类或高风险步骤前重新比较，变化即重新规划；在途 Job 只跟踪、不重复提交 |
| C4 ToolSpec 补字段：`execution_mode(sync/job)`、`produces(artifact type)`、`verify(规则名)`、`output_schema` |
| C5 动态工具暴露：主对话的 LLM 工具清单按当前阶段和风险裁剪（复用 `get_allowed_tools`，增加阶段维度） |
| C6 通用审批步骤：按工具元数据统一处理（request_generate→run_generate 配对，compose / export / regenerate 审批后执行），并处理用户先在页面点"执行"的幂等问题 |
| C7 预算：`ToolBudget` 扩展到 Goal 级（LLM tokens、Job 数、最长运行时间），超限进入 waiting_user |
| C8 产物级验证：步骤成功 = 工具 / Job 成功 + 产物验证通过；目标完成 = 目标阶段的真实状态满足 |

**验收**：在"已解析、资产 2/5、无分镜"的项目上，输入"完成分镜视频"，计划只包含缺失的 3 个资产、审核、分镜图等剩余步骤；已完成的部分不重做。

### Phase D · Human Gate 与版本绑定（M）

| 任务 | 说明 |
|---|---|
| D1 **【已决策】项目级开关**：存放在 `project.settings["review_policy"]`，**键不存在即视为关闭**，所以存量项目无需数据迁移；正式项目的创建路径写入默认开启，草稿项目关闭；从关闭改为开启需二次确认并展示影响 |
| D2 审核字段挂在 **asset 行**上（每个版本一行），新版本天然"未审核"；不复用遗留的 `Project.assets_review_status`、`Shot.keyframe_confirmed` |
| D3 资产门禁：在 `assets_gate.evaluate_assets_gate` 中加入审核判定，一处修改覆盖所有调用方 |
| D4 分镜图门禁：把只存在于 API 层的 `_check_keyframe_ready` 移到服务层并加入审核判定；记录审核时的参考资产，参考资产被替换后审核自动过期 |
| D5 Worker 兜底：Job 创建点分散在约 20 处，所以在执行器提交 ComfyUI 之前再检查一次，拦截所有绕过路径 |
| D6 Runtime 识别审核步骤 → waiting_approval；驳回 → 申请重新生成（需审批），超过 3 轮交给用户；分镜图审核过期时由用户选择保留或重做 |
| D7 对话中审核：只作用于展示给用户的具体版本；D8 最小前端：资产库、分镜图页的审核按钮和项目开关 |

**验收**：直接调用 API 绕过 Agent 也会被门禁拒绝；重新生成资产后，依赖它的分镜被标记 stale，Agent 计划会自动包含复核步骤。

### Phase E · Agent Workbench 前端（M）

在现有 `DirectorConsoleView` / `ContextSidebar` 中增量添加，不推倒重写：
- **两个界面共用一个 store**：整页导演台（`DirectorConsoleView`，目前自带一份重复的审批状态）和全局右侧助手面板（`DirectorAgentPanel`）都读取新的 `stores/agentGoal.ts`
- **事件只当刷新信号**：复用 `useWorkspaceEvents` 单例（已有按序号补齐），收到 `agent.*` / `review.*` 事件后节流重新拉取目标状态，不用事件内容拼状态；断线恢复和页面重新可见时也刷新一次
- GoalCard（目标、阶段、状态、取消 / 暂停）
- PlanSteps（步骤列表，显示状态、关联 Job / 审批，revision 可切换）
- 等待态入口：待审批直接跳转审批卡片，待 Job 显示 Job 实时进度（复用任务中心数据）
- **Runtime 步骤的审批只显示"批准 / 拒绝"**，不显示"执行"（批准后 Agent 自动执行）
- 数据来源：`GET goal status` 加事件增量拉取；**不从聊天文本推断状态**
- 进度分开展示 Plan / Job / 产物三类，无法精确计算的不显示百分比
- 前端目前没有单测框架，新增 vitest（仅纯逻辑）并接入 CI；后端补少量接口（计划历史、阶段进度、单步重试、调整目标）

**验收**：刷新、切换项目、重启后状态一致；失败步骤可一键重试或修改计划。

### Phase F · Memory、评估与清理（M，可与 E 并行）

- 追踪：执行日志补齐触发来源、Job、审批、触发消息，提供目标完整链路接口；日志写入前统一脱敏。
- LLM 计量：`LLMClient` 已有 token 用量元数据，按目标归属累计（线程间用 contextvars 隔离），预算可按 token 限制。
- Memory：检索按"用户明确保存 → 与当前对象相关 → 最近使用"排序，只用确定性匹配、**不引入 embedding**（单文件 exe、离线可用）；增加过期、软删除、敏感内容拒绝；记忆只用于目标解析的默认值和对话措辞，**不进入 Planner、门禁、审核、Verifier**（静态守卫测试）。
- 经验记忆：只从执行记录中的确定性模式生成候选，用户确认后才生效。
- 指标：直接用 SQL 聚合现有表，16 项可统计，工具选择准确率、模型费用、上下文错误率明确标为不可统计；配一个只读统计页面。
- 评测：目标解析评测集（80+ 条中文表述，规则路径进 CI，真实 LLM 评测为离线脚本）；原始 Plan 的 20 个场景与 29 项回归清单全部落成测试并出对照表。
- **后端 CI**：目前只有前端检查，新增 lint、迁移升降级、pytest（可提前到 Phase F 之前做）。
- 清理：`docs/agent/`、`docs/agent-*.md` 等归档到 `docs/_archive/`，`docs/AGENTS.md` 的索引改为指向 `docs/agent-runtime/`；根目录 `_probe_*.py` 移到 `backend/scripts/dev/`；README 的工具数和测试数改为脚本统计。
- 运维：运维手册 + 默认只读的排障脚本 `agent_doctor.py`；最终按原始 Plan 的 32 条验收标准逐条对照，并做真实环境与 exe 打包验收。

---

## 五、硬性约束（给编码 Agent）

1. 唯一用户入口是 `/{session_id}/chat`；Runtime 只是服务层，不能再开新的并行对话系统。
2. 工具执行只能走 `agent_tools.execute_tool`；禁止直接调用工具函数，禁止 Agent 直接写业务表或调用 ComfyUI。
3. Runtime 入口自管 Session 和事务，每推进一步提交一次；禁止把请求作用域 Session 传入线程。
4. Job 创建成功只能让步骤进入 `waiting_job`；只有 Job 终态 succeeded 且验证通过，步骤才算 succeeded。
5. 审批以 step 为粒度绑定 confirmation_id；禁止"一次审批放行整个循环"。
6. 不新建事件表；新增事件类型必须登记白名单。
7. 所有迁移只加不删、新列可空、带 downgrade。
8. 测试只能用临时库；禁止连接 `data/director_lab.db`。
9. 每个 Phase 结束时更新 `docs/agent-runtime/implementation-plan.md` 的勾选状态，不新开平行文档。

---

## 六、已确认决策（2026-11-26）

| 决策 | 结论 | 影响 |
|---|---|---|
| 资产 / 分镜审核门禁 | ✅ **项目级开关**：新项目默认开启，存量项目默认关闭 | Phase D1–D4；Planner 按开关决定是否插入审核步骤 |
| Planner 是否用 LLM 生成计划 | ✅ **不用**：计划由代码根据真实进度生成，LLM 只解析目标 | Phase C2–C3；Phase C 跑稳后可再评估 |
| `/api/agent/loop/run` | ✅ **保留为调试接口**，前端不暴露 | Phase B2；`/chat` 是唯一用户入口 |
| 多用户权限 | ✅ **延后**（默认） | 本期只做工具风险分级 + 服务端审批门禁 + 项目隔离 |

---

## 附：建议执行顺序与里程碑

```
A 止血（P0 全修 + 测试隔离）
  └─► B MVP：主链路接入 + Worker 驱动恢复   ← 第一个可演示里程碑
        ├─► C 状态驱动 Planner + 工具收敛
        │     └─► D Human Gate + 版本绑定（先拍板 §六）
        │           └─► E Workbench 前端
        └─► F Memory / 指标 / Eval / 清理（可并行）
```
