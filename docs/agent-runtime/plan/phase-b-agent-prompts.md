# Director-Lab · Phase B「最小可用版本（MVP）」编码 Agent 任务提示词

> 前置条件：**Phase A 全部合并**，并且 `tests/test_agent_runtime_*.py` 全绿。基线以 Phase A 合并后的主分支为准；行号只作参考，请按函数名定位。
> 用法：每次会话先粘贴 **〇、通用上下文**，再粘贴**一个**任务。一个任务对应一个 PR。
> 执行顺序：**B0 → B1 → B2 → B3 → B4 → B5 → B6 → B7 → B8 → B9 → B10 → B11**
> 对应最终 Plan 的 Phase B。本文件对 Plan 做了一处设计细化：**恢复请求用"步骤上的标志位 + 周期兜底扫描"实现，不按事件流消费**（原因见 §〇 设计说明）。

---

## 〇、通用上下文（每个任务前都要粘贴）

```text
你在 Director-Lab 仓库（FastAPI + SQLAlchemy 2 + SQLite WAL + Alembic，后端在 backend/）中工作。
当前任务属于「Agent Runtime Phase B：接入主链路，打通恢复闭环」。Phase A 已修好 Runtime 自身的正确性。

Phase B 目标（MVP）：
1. 用户只在 /api/agent/{session_id}/chat 对话，多步生产类请求交给 Agent Runtime 执行；
2. Job 完成、审批通过后，Agent 自动继续，不依赖页面在线或 HTTP 长连接；
3. 后端重启后自动接着跑，且不重复创建 Job；
4. 页面刷新后能从 API 读到真实状态。

关键文件：
- app/services/agent_runtime.py      Runtime（Phase A 后已有：run_agent_loop / run_agent_loop_in_new_session / Goal 锁 / 步骤锁）
- app/services/execution_manager.py  execute_step（只走 execute_tool；Job → waiting_job；审批 → waiting_approval）
- app/services/task_orchestrator.py  bridge_job_event / attach_job_stage / create_task / cancel_task
- app/services/approvals.py          create_confirmation / resolve_confirmation / find_approved / mark_task_finished
- app/services/workspace_event.py    write_workspace_event + EVENT_TYPES 白名单（outbox，event_seq 全局递增）
- app/worker/scheduler.py            run_worker 主循环（单进程锁 worker.lock；可内嵌在 API 进程，见 DLAB_WORKER_EMBEDDED）
- app/worker/executor.py 等           Job 执行器（bridge_job_event 的实际调用方，请先 grep 确认）
- app/services/job_cancel_ops.py     cancel_jobs(db, jobs, reason=...)
- app/api/v1/agent.py                /{session_id}/chat（约 L1828 起）；审批 resolve（L373）/ execute（L396）
- app/services/agent_tool_impls.py   tool_request_generate（返回 confirmation_id）/ tool_run_generate（幂等，返回 job_id）

设计说明（请遵守，不要改成别的方案）：
- 恢复请求 = agent_plan_step.resume_requested_at 标志位。bridge_job_event / 审批裁决只负责"打标记"
  （与业务写入同一事务），**不在 Worker 的 Job 执行线程或 API 请求里同步调用 Runtime**，避免重入、长事务和死锁。
- Worker 中有一个独立的 agent-resume 线程：每 2 秒认领带标记的步骤并调用 tick；每 30 秒做一次兜底扫描
  （Job 已终态但仍是 waiting_job 的步骤、审批已裁决但仍是 waiting_approval 的步骤、超时的 running 步骤）。
  标记丢失、进程重启都由兜底扫描补齐，所以不需要"事件游标"表。
- workspace_event 只用于前端展示（agent.* 事件类型），不作为恢复驱动。
- 一个 Goal ↔ 一个 AgentTask ↔ 一个统一 Task。产生 Job 的步骤把 TaskStage 挂到这个统一 Task 上，不再一步一个 Task。

硬性约束：
1. 继续遵守 Phase A 的 10 条约束（只走 execute_tool；不把请求 Session 传入线程；Job 创建 ≠ 成功；
   审批按步骤绑定；迁移只增不删；测试只用 tmp 库；统一状态词表；不新建事件表或并行执行器……）。
2. 新增的 Runtime 接入**必须放在配置开关后面**：settings.agent_runtime_chat_enabled（环境变量
   DLAB_AGENT_RUNTIME_CHAT_ENABLED，默认 False）。开关关闭时，/chat 的行为必须与改动前逐字节一致（有回归测试证明）。
3. tick 每次调用最多推进 MAX_STEPS_PER_TICK（默认 10）个步骤，遇到等待态立即提交并返回；单次 tick 墙钟上限 60 秒。
4. 生成类 Job 失败时，Phase B **不自动重新提交生成**（Worker 内部已有重试和熔断）；步骤置为 failed 后进入 waiting_user，由用户决定。
5. 已知事务例外：approvals.create_confirmation / resolve_confirmation / mark_task_finished 内部会 db.commit()。
   本阶段**不重构 approvals.py**；调用它们之后要假设事务已提交，不能依赖回滚撤销它们的写入。
6. 不改前端（Phase E），但所有新接口要在 docs/API.md 中补文档。

交付要求：
- 代码 + 测试一起提交；`cd backend && pytest -q tests/test_agent_runtime_*.py tests/test_agent_phase_b_*.py` 全绿；
  全量 pytest 的失败数不多于改动前（在 PR 描述中附统计）。
- PR 描述包含：改了什么、为什么、如何验证、遗留问题。
- 完成后在 docs/agent-runtime/implementation-plan.md 中勾选对应条目。
```

---

## B0 · 迁移与事件白名单

```text
任务：新增一个 Alembic 迁移（down_revision = 当前 head），并同步模型与事件白名单。

迁移新增列（全部可空，带 downgrade）：
1. agent_plan_step.resume_requested_at  DateTime nullable, index   —— 恢复请求标志位
2. agent_plan_step.resume_reason        String(32) nullable        —— job_terminal / approval_resolved / sweep / manual
3. agent_goal.agent_task_id             String(32) nullable, index —— 对应的 AgentTask（用户级任务）
4. agent_goal.task_id                   String(32) nullable, index —— 对应的统一 Task（承载所有 Job 阶段）
5. agent_goal.lifecycle_state           String(32) nullable        —— TickResult 推导的生命周期，供前端直接读
   （取值：planning / running / waiting_job / waiting_approval / waiting_user / paused /
     partially_completed / completed / failed / cancelled）
6. agent_goal.last_tick_at              DateTime nullable

workspace_event.EVENT_TYPES 白名单新增（只加不改；在 docstring 中说明每个类型的 payload 字段）：
- agent.goal.created / agent.goal.updated / agent.goal.superseded / agent.goal.finished
- agent.plan.created（payload: goal_id, plan_id, revision, steps[{step_id, step_key, tool_name, status}]）
- agent.step.updated（payload: goal_id, plan_id, step_id, step_key, tool_name, status, job_id?, confirmation_id?, error_code?）
- agent.tick.finished（payload: goal_id, lifecycle_state, counts{succeeded, failed, waiting, pending}）
注意 event_type 列是 String(48)，名字不要超长。

验收：
- tests/test_agent_phase_b_migration.py：upgrade 后新列存在、downgrade 后消失（复用 Phase A 迁移测试的写法）。
- write_workspace_event 写入新类型成功；写入未登记类型仍然被拒绝（已有行为，补一条断言）。
```

---

## B1 · 抽出 `tick`：幂等的单次推进

```text
任务：把 Runtime 的"推进"逻辑抽成一个幂等函数 tick，供 /chat、Worker、调试接口共用。

1. 新增数据类 TickResult：
   goal_id, plan_id, lifecycle_state, advanced_steps(本次推进的 step_id 列表),
   waiting: {"job": [step_id...], "approval": [{step_id, confirmation_id}], "user": [{step_id, error_code, message}]},
   failed: [{step_id, tool_name, error_code, message}], counts, message, busy(bool)。
   提供 to_dict()。

2. 新增 agent_runtime.tick(session_factory, goal_id, *, trigger: str, progress_callback=None) -> TickResult：
   a. 取 Goal 锁（Phase A 的 Goal 级条件 UPDATE）；拿不到锁就返回 busy=True，不做任何写入。
   b. 读 Goal；如果 status 不是 active（或 lifecycle_state 为 paused / cancelled），直接返回当前状态。
   c. 处理带 resume_requested_at 的步骤（见 B3、B4 的恢复规则），处理完清除标记。
   d. 执行 _execute_plan_loop，最多 MAX_STEPS_PER_TICK 步、最长 60 秒；每步提交一次（沿用 Phase A）。
   e. 用 Phase A 的 _derive_loop_status 计算 lifecycle_state，写入 agent_goal.lifecycle_state / last_tick_at；
      同步更新 AgentTask.lifecycle_state（通过 parent_agent 已有的状态机函数，不要直接赋值 status）。
   f. 释放 Goal 锁，最终提交。异常时回滚、释放锁，在新事务中记录 execution_log(status="tick_error")，返回 failed，不向外抛出。
   trigger 取值：chat / worker_resume / worker_sweep / debug / manual，写入 execution_log。

3. run_agent_loop 改为：解析或创建 Goal（含 Phase A 的指纹逻辑）→ 获取或生成 Plan → 调用 tick。
   resume_after_job / resume_after_approval 改为"设置标记 + 调用 tick"的薄封装（保留函数名，供现有测试使用）。
4. 调试接口 /api/agent/loop/run 改为调用 run_agent_loop_in_new_session（内部走 tick），行为不变。

验收测试 tests/test_agent_phase_b_tick.py：
- test_tick_is_idempotent：对一个已处于 waiting_job 的 Goal 连续调用 tick 3 次，工具调用计数不变、步骤状态不变。
- test_tick_respects_max_steps：12 个串行同步步骤 → 第一次 tick 推进 10 步，第二次推进 2 步并 completed。
- test_tick_busy_when_locked：持有 Goal 锁时 tick 返回 busy=True。
- test_tick_paused_goal_noop。
- test_tick_exception_releases_lock：注入异常后，Goal 锁已释放，下一次 tick 可以正常执行。
- test_lifecycle_written_to_goal_and_agent_task。
```

---

## B2 · Agent 事件发布（给前端看的事实流）

```text
任务：新增 app/services/agent_events.py，集中发布 agent.* 事件到 workspace_event outbox。

1. 函数：publish_goal(db, goal, event_type, extra=None)、publish_plan_created(db, goal, plan)、
   publish_step(db, goal, step)、publish_tick_finished(db, goal, tick_result)。
   - 一律调用 write_workspace_event，session_id / project_id 取自 goal；与业务写入同一事务（不自行 commit）。
   - payload 只放事实字段（id、状态、错误码），禁止放 LLM 文本、提示词或进度百分比估算。
2. 在 Runtime 中接入：
   - Goal 新建 / 指纹替代 / 终态 → publish_goal
   - Plan 保存 → publish_plan_created
   - 步骤状态每次变化（包括 Phase A 的 _release_step_lock 和等待态切换）→ publish_step。
     要求实现成"状态确实变化才发"，同一状态重复写入不发事件。
   - tick 结束 → publish_tick_finished
3. progress_callback（调试 SSE 使用）保持不变，与 outbox 并存；两者共用同一份事件构造函数，保证字段一致。

验收测试 tests/test_agent_phase_b_events.py：
- test_events_written_in_same_tx：在业务提交前回滚 → workspace_event 中没有对应事件。
- test_step_event_only_on_change：同一状态写两次，只产生一条 agent.step.updated。
- test_event_sequence_for_simple_plan：两步同步计划 → 事件顺序为 goal.created, plan.created, step.updated×4(running/succeeded×2), tick.finished，event_seq 严格递增。
- test_payload_has_no_free_text：断言 payload 的键都在允许列表内。
```

---

## B3 · Job 终态 → 恢复标记

```text
任务：Job 进入终态时，给对应的 Agent 步骤打恢复标记。

1. 先 grep 全仓库 bridge_job_event( 的调用方（预计在 app/worker/executor.py、compose_executor.py、
   export_executor.py、parse_executor.py 以及 scheduler 的对账路径中），确认所有 Job 终态路径都会经过它。
   如果发现某条终态路径不经过 bridge_job_event，在 PR「遗留问题」中列出，并保证 B5 的兜底扫描能覆盖它。
2. 在 task_orchestrator.bridge_job_event 中，当 event_type ∈ _TERMINAL_JOB_EVENTS 时（与现有逻辑同一事务）：
   UPDATE agent_plan_step SET resume_requested_at=now, resume_reason='job_terminal'
   WHERE job_id = :job_id AND status = 'waiting_job' AND resume_requested_at IS NULL。
   注意：即使该 Job 没有关联 TaskStage（stages 为空、函数提前 return 0），也要执行这一步，
   所以把它放在函数开头，或单独抽成 mark_agent_steps_for_job(db, job_id)，在 return 之前调用。
3. tick 中对 resume_reason='job_terminal' 的步骤做恢复（替代 Phase A resume_after_job 中的逻辑）：
   - 读取 Job 真实状态：succeeded → 交给 Verifier（Phase A 规则：Job succeeded 才算通过）→ succeeded；
     failed / cancelled → 步骤 failed，error_code = "job_failed:" + (job.error_code or job.status)，
     然后按通用约束 4 进入 waiting_user（Replanner 的映射表新增 job_failed → WAIT_USER）。
   - 仍是 queued / running（标记打早了，或者已被手动重新入队）→ 清除标记，保持 waiting_job。
   - 同一个步骤挂多个 Job 时（批量），全部终态才判定；部分失败 → failed 并在 result 中列出失败的 job_id。
4. Job 的 retry / quality_retry 事件不打标记（Job 还没结束）。

验收测试 tests/test_agent_phase_b_job_resume.py：
- test_terminal_job_marks_step：Job 走一遍 bridge_job_event("job.finished", status=succeeded) → 步骤带标记；
  调用 tick → 步骤 succeeded，下游步骤开始执行。
- test_job_failed_goes_waiting_user。
- test_marker_set_even_without_stage。
- test_retry_event_does_not_mark。
- test_early_marker_cleared_if_job_running。
- test_bridge_does_not_call_runtime：monkeypatch agent_runtime.tick 并计数，bridge_job_event 调用后计数为 0。
```

---

## B4 · 审批闭环：自动申请、裁决后恢复、自动执行

```text
背景：现有审批是两步——resolve（批准/拒绝）和 execute（创建 Job）。生成类工具分为
request_generate（只建审批记录，返回 confirmation_id）和 run_generate（凭 confirmation_id 建 Job，幂等）。

任务：
1. 自动申请：execute_step 收到 error_code="approval_required"（工具本身要求审批，例如 run_style_update）时，
   调用 approvals.create_confirmation(db, session_id=goal.session_id, tool_name=step.tool_name, args=<注入后的参数>,
   goal=step.description) 创建审批记录（它是幂等的，同会话同工具同参数会复用 pending 记录），
   把 confirmation_id 写入 step，步骤置为 waiting_approval。注意它内部会 commit（见通用约束 5）。
2. 裁决打标记：在 approvals.resolve_confirmation 中，于最后的 db.commit() 之前加入：
   UPDATE agent_plan_step SET resume_requested_at=now, resume_reason='approval_resolved'
   WHERE confirmation_id = :id AND status = 'waiting_approval'。
   这是对 approvals.py 唯一允许的修改，不要改动其他逻辑。
3. tick 中对 resume_reason='approval_resolved' 的步骤做恢复：
   a. 审批 approved，且步骤工具是 request_generate / batch_request_generate →
      Runtime 调用 execute_tool(db, "run_generate", approved=True, confirmation_id=...)。
      run_generate 是幂等的：如果用户已经在页面上点过"执行"，会返回已存在的 job_id。
      （请阅读 tool_run_generate 确认幂等分支，在 PR 中写明依据的代码位置。）
      返回 job_id → 写入 step.job_id，用 attach_job_stage 挂到 Goal 的统一 Task（见 B6）→ waiting_job。
   b. 审批 approved，且步骤工具本身需要审批（第 1 条的情况）→ execute_tool(db, step.tool_name, approved=True, **args)；
      按 Phase A 的结果分流处理。
   c. rejected → 步骤 waiting_user，error_code="approval_rejected"，message 带上审批意见（comment）。
      不自动重新规划（Phase D 再接入按拒绝原因重新生成）。
   d. 审批仍是 pending（标记来源异常）→ 清除标记，保持等待。
4. 流程工具（PIPELINE_TOOL_REGISTRY）和返工工具（REGEN_TOOL_REGISTRY）的审批：本阶段**不接入** Runtime，
   Planner 不得生成这类步骤；在 Plan Validator 中加一条规则：工具不在 TOOL_REGISTRY 中 → 报错。

验收测试 tests/test_agent_phase_b_approval.py（使用真实的 request_generate / run_generate 和 mock 渲染配置）：
- test_request_generate_waits_then_runs_on_approve：步骤 waiting_approval → resolve(approve=True) → 步骤带标记 →
  tick → 生成 Job、步骤 waiting_job、job_id 已写入。
- test_user_clicked_execute_first_no_duplicate_job：resolve 后先调用 execute 接口，再 tick → Job 表中只有 1 条。
- test_reject_goes_waiting_user_with_comment。
- test_tool_requiring_approval_auto_creates_confirmation：同一步骤 tick 两次只产生 1 条 AgentConfirmation。
- test_pipeline_tool_rejected_by_validator。
- test_resolve_confirmation_does_not_call_runtime：tick 调用计数为 0。
```

---

## B5 · Worker 中的 agent-resume 线程与兜底扫描

```text
任务：新增 app/services/agent_resume.py，并在 Worker 中启动它。

1. agent_resume.process_marked(session_factory, *, limit=5) -> int：
   - 认领：用条件 UPDATE 把一批带 resume_requested_at 的步骤"认领"（例如写 resume_reason 为 'claimed:<uuid>'，
     或者在同一语句中清空 resume_requested_at，并通过 RETURNING / 二次查询拿到被认领的行；SQLite 3.35+ 支持 RETURNING，
     请先确认运行环境的 SQLite 版本，不支持时用"按 id 逐条条件 UPDATE，rowcount==1 才算认领"）。
   - 按 goal_id 去重后，对每个 Goal 调用 agent_runtime.tick(session_factory, goal_id, trigger="worker_resume")。
   - tick 返回 busy 时，重新给这些步骤打标记，下一轮再处理。
   - 单个 Goal 的异常不影响其他 Goal（捕获并记录日志）。
2. agent_resume.sweep(session_factory) -> dict：兜底扫描，补打标记（resume_reason='sweep'）：
   - status='waiting_job' 且对应 Job 已是终态；
   - status='waiting_approval' 且对应 AgentConfirmation 已不是 pending；
   - 调用 Phase A 的 reclaim_stale_steps 回收超时的 running 步骤；
   - Goal 是 active、lifecycle_state 是 running，但 last_tick_at 超过 5 分钟、且存在 pending 步骤（tick 中途崩溃的情况）→ 直接 tick。
3. Worker 接入（app/worker/scheduler.py）：
   - 在 run_worker 拿到 worker.lock 之后，启动一个守护线程 agent-resume：
     循环 { process_marked(SessionLocal)；每 30 秒执行一次 sweep；sleep 2 秒 }，响应 _RUNNING / request_stop 退出。
   - run_worker 的启动对账阶段（现有 _recover_interrupted_jobs 等之后）先执行一次 sweep。
   - 加配置开关 settings.agent_resume_enabled（DLAB_AGENT_RESUME_ENABLED，默认 True；
     agent_runtime_chat_enabled 为 False 时，这个线程实际上不会遇到任何步骤）。
   - **不要**修改 Job 调度主循环的逻辑；agent-resume 线程的任何异常都不能让主循环退出。
   - 优雅停止：线程每次循环检查停止标志，run_worker 退出前 join（最多等待 5 秒）。
4. 测试中不启动线程（conftest 已设置 DLAB_WORKER_EMBEDDED=false），直接调用 process_marked / sweep。

验收测试 tests/test_agent_phase_b_resume_worker.py：
- test_process_marked_ticks_each_goal_once：同一 Goal 下 3 个带标记的步骤 → tick 调用 1 次。
- test_concurrent_process_marked_no_double_tick：两个线程同时调用 process_marked → 每个 Goal 的 tick 总共只执行 1 次（或第二次返回 busy 且不推进）。
- test_sweep_recovers_lost_marker：Job 已 succeeded、步骤 waiting_job、没有标记 → sweep 后有标记 → process_marked 后步骤 succeeded。
- test_sweep_recovers_crashed_tick。
- test_worker_thread_start_stop：用 run_worker(once=True) 或直接测试线程函数，确认能启动并在 request_stop 后 5 秒内退出。
```

---

## B6 · `/chat` 接入 Runtime（开关后面）

```text
任务：在 /{session_id}/chat 中，把"多步生产类请求"交给 Runtime；其他请求保持原样。

1. 新增 app/services/agent_runtime_router.py::should_use_runtime(intent, resolved, content) -> RuntimeRoute | None：
   - 开关 settings.agent_runtime_chat_enabled 关闭 → None。
   - 会话没有 project_id → None（交给现有逻辑引导用户先选项目）。
   - 只匹配 B7 支持的目标类型：生成本项目的角色/场景/道具资产、生成分镜图、"继续上次的任务"。
     判定依据：intent.intent / intent.subcategory / intent.entities（先阅读 intent_router.py，列出实际可用的意图和子类，
     在 PR 中给出映射表）。对单个镜头、单个实体的生成请求（已有 shot_id / entity_id），**仍走现有的随手生成或审批流程**，不进 Runtime。
   - 返回 RuntimeRoute(goal_type, target_stage, scope)。
2. 在 chat 中插入分支：位置在 route_action 计算之后、现有 GUIDE / PIPELINE 分支之前（约 L2174–2195）。
   命中 RuntimeRoute 时：
   a. 复用 chat 前面已经创建的 AgentTask（parent_agent.create_task_from_intent 的返回值）和统一 Task；
      写入 agent_goal.agent_task_id / task_id，以及 AgentTask.goal_id / plan_id（列在 Phase 1 迁移中已存在）。
   b. 调用 run_agent_loop（同步执行第一次 tick；遇到等待态就会返回，耗时受 MAX_STEPS_PER_TICK 和 60 秒上限约束），
      trigger="chat"。
   c. 用 B8 的 render_tick_reply(tick_result) 生成助手回复文本（确定性，不调用 LLM）。
   d. 助手消息 payload 写入 {"route_action": "agent_runtime", "goal_id", "plan_id", "lifecycle_state", "tick": tick_result.to_dict()}。
   e. 不再进入现有的 GUIDE / PIPELINE / LLM 主对话分支；按现有方式提交事务并返回。
3. 执行产生 Job 的步骤时（Phase A 中 execute_step 自建 Task 的逻辑）：改为挂到 Goal 的统一 Task（goal.task_id）上；
   goal.task_id 为空时（例如调试接口创建的 Goal）才新建一个统一 Task 并回写 goal.task_id。
4. 同一个 session 已有 active Goal、用户又发来同类请求（指纹相同）：直接 tick 并回复当前进度，不新建 Goal、Plan 或审批。

验收测试 tests/test_agent_phase_b_chat.py（用 TestClient 调用 /chat；LLM 意图识别通过 monkeypatch 返回固定结果）：
- test_flag_off_behavior_unchanged：开关关闭时，对一组固定输入（问答、导航、单镜头生成、批量资产生成）
  比较改动前后的响应与数据库写入（可以把改动前的响应保存为 fixture JSON）。
- test_runtime_route_creates_goal_and_replies：开关打开，输入"帮我生成这个项目所有角色资产" →
  响应 payload.route_action=="agent_runtime"，存在 goal / plan，lifecycle_state=="waiting_approval"。
- test_single_entity_generation_not_routed。
- test_no_project_not_routed。
- test_repeat_request_reuses_goal：同一句话发送两次 → 只有 1 个 Goal、1 组审批记录。
- test_job_steps_attach_to_goal_task：审批并 tick 后，所有 TaskStage.task_id == goal.task_id。
```

---

## B7 · 最小计划模板：资产生成与分镜图生成

```text
背景：Phase C 会把 Planner 改成完全由项目状态驱动。Phase B 只需要支持两种目标，先把这两种做对，
**不要在 Phase B 实现通用 Planner**。按已确认的决策，计划结构由代码生成，不使用 LLM。

任务：在 planner.py 中新增 build_plan_from_template(db, goal, route: RuntimeRoute) -> Plan，
当 goal.goal_type 属于模板类型时，run_agent_loop 用它代替 build_plan。

1. 模板 generate_assets（scope: character / scene / prop / all）：
   - 读取项目当前解析版本下的实体，筛选"没有 current 有效资产"的实体
     （复用现有查询函数：list_project_entities / assets_gate / stage_readiness 中已有的判定，不要自己写 SQL 判断资产有效性）。
   - 前置步骤：project_overview（只读）。
   - 每个缺资产的实体生成一个步骤：tool_name="request_generate"，
     args={"project_id", "entity_id", "target": <character|scene|prop>}，step_key=f"asset:{target}:{entity_id}"，
     depends_on=[overview]；requires_approval=False（工具本身只建审批记录，Runtime 进入 waiting_approval 等待裁决）。
   - 没有缺失实体 → 计划只有 overview，执行完即 completed，回复"资产已齐全"。
   - 前置条件不满足（剧本未解析等）→ overview 之后的步骤会被 precondition_failed 拦截，按 Phase A 规则进入 waiting_user。
2. 模板 generate_keyframes（scope: all / episode:<id>）：
   - 先检查资产就绪（assets_gate / stage_readiness）；不就绪 → 计划只包含一个 waiting_user 步骤
     （step_type="clarification"，message 说明缺哪些资产），不要偷偷先生成资产。
   - 每个缺分镜图的镜头一个 request_generate 步骤，target="keyframe"，step_key=f"keyframe:{shot_id}"。
3. 每个模板设置批量上限 settings.agent_max_batch_steps（默认 30）。超过上限时只规划前 30 个，
   在 Goal 中记录 remaining_count，回复中说明"还有 N 个，完成后继续"。下一次 tick 发现计划全部终态、
   且 remaining_count>0 时，按 Phase A 的规则生成新的 revision（carry_over 已完成的步骤）。
4. "继续上次的任务"：不生成新计划，只对当前 active Goal 调用 tick。

验收测试 tests/test_agent_phase_b_templates.py：
- test_assets_template_only_missing_entities：项目中 3 个角色有 1 个已有资产 → 计划包含 overview + 2 个 request_generate。
- test_assets_template_all_present_completes。
- test_keyframes_blocked_when_assets_not_ready → lifecycle_state=="waiting_user"，没有生成任何审批。
- test_batch_cap_and_continuation：35 个缺资产实体 → 第一个 revision 30 步；完成后生成第二个 revision，包含剩余 5 步。
- test_template_steps_pass_validator：所有模板计划都能通过 validate_plan。
```

---

## B8 · 确定性回复与状态查询接口

```text
任务：
1. 新增 app/services/agent_reply.py::render_tick_reply(tick_result, *, goal, plan_steps) -> str：
   - 纯函数、确定性，不调用 LLM。按 lifecycle_state 选用模板：
     waiting_approval：「已为 N 个角色/场景/道具提交生成申请，请在审批面板确认（共 N 项）」，列出前 5 个实体名称；
     waiting_job：「已提交 N 个生成任务，正在生成，完成后会自动继续」+ Job 数量；
     waiting_user：逐条列出原因（precondition 提示、审批被拒及意见、Job 失败的错误码）和建议操作；
     completed / partially_completed / failed：已完成、失败、跳过的数量，以及失败项列表。
   - 禁止出现"已完成"等措辞，除非 lifecycle_state == completed；
     禁止在 waiting_job 时出现"生成完成"。为此写一条断言测试，扫描所有模板输出。
2. 新增接口（app/api/v1/agent.py 或新文件 app/api/v1/agent_goals.py，注册到 main.py）：
   - GET  /api/agent/{session_id}/goal         当前 active Goal 的完整状态（复用 Phase A 的 get_goal_status_by_id）；没有则返回 {"goal": null}
   - GET  /api/agent/{session_id}/goals        该会话的历史 Goal 列表（分页，按 updated_at 倒序）
   - GET  /api/agent/goals/{goal_id}           按 id 查询
   状态全部来自数据库，不读取聊天消息。
3. Job 完成、tick 推进后，向会话写一条 assistant 消息（来自 Worker 的 tick 也要写），让用户回到对话时能看到进度：
   - 只在 lifecycle_state 发生变化时写；内容使用 render_tick_reply；payload 带 goal_id 和 "source": "agent_runtime"；
   - seq 的取号方式沿用 chat 中的 max(seq)+1 写法，并处理 (session_id, seq) 唯一索引冲突（冲突时重试一次）。

验收测试 tests/test_agent_phase_b_reply.py：
- test_reply_templates_by_state：参数化覆盖所有 lifecycle_state。
- test_no_false_completion_wording。
- test_goal_status_endpoints。
- test_worker_tick_posts_message_on_state_change_only。
```

---

## B9 · 暂停、继续、取消

```text
任务：新增 Goal 控制接口与服务函数。

1. agent_runtime.pause_goal(db, goal_id) / resume_goal(db, goal_id) / cancel_goal(db, goal_id, reason)：
   - pause：lifecycle_state=paused；tick 遇到 paused 不推进；已经在跑的 Job 不取消（完成后步骤照常打标记，
     但 tick 只更新步骤状态、不推进下游）。
   - resume：恢复为 running 并立即 tick（API 中同步调用一次）。
   - cancel：
     a. 所有 waiting_job 步骤的 Job 用 job_cancel_ops.cancel_jobs(db, jobs, reason=...) 取消（先阅读该函数，确认它对已终态 Job 是否安全）；
     b. 所有 waiting_approval 步骤关联的 pending 审批 → cancelled（如果 approvals.py 没有现成的取消函数，
        新增 cancel_confirmation(db, id, reason)，遵循该文件现有的写审计消息和事件的模式）；
     c. 所有非终态步骤 → cancelled；Plan → cancelled；Goal → cancelled；
        AgentTask / 统一 Task 经各自状态机置为 cancelled；
     d. 执行记录全部保留（不删除任何行）。
2. 接口：POST /api/agent/goals/{goal_id}/pause | /resume | /cancel（cancel 的请求体可带 reason）。
   对已终态的 Goal 调用返回 409。
3. /chat 中识别"暂停/继续/取消当前任务"：优先复用 intent_router 已有的控制类意图；没有的话，
   在 agent_runtime_router 中加一个严格的关键词匹配（只在存在 active Goal 时生效），并写测试防止误触发。

验收测试 tests/test_agent_phase_b_control.py：
- test_pause_stops_advancing：暂停后 Job 完成 → 步骤 succeeded，但下游步骤保持 pending。
- test_resume_continues。
- test_cancel_cancels_jobs_and_confirmations：Job 状态变为 cancelled，审批变为 cancelled，没有步骤是 running 或 waiting_*。
- test_cancel_terminal_goal_409。
- test_chat_control_keywords_only_with_active_goal。
```

---

## B10 · 端到端 MVP 验收测试

```text
任务：新增 tests/test_agent_phase_b_e2e.py，用 render mock 跑通完整链路。这是 Phase B 的验收门禁。

环境：tmp SQLite + tmp media_dir；Settings(render_mock=True)；LLM 意图识别用 monkeypatch 返回固定意图；
不启动线程，"Worker"由测试代码直接调用 scheduler 的单轮执行（run_worker(once=True) 或 _execute_job）和 agent_resume.process_marked。

场景（每个场景一个测试函数）：
1. test_mvp_full_flow：
   已解析项目，3 个角色缺资产 → /chat "帮我生成这个项目所有角色资产" →
   waiting_approval（3 条审批）→ 逐条调用审批 resolve 接口 → process_marked →
   3 个 Job queued、步骤 waiting_job → 执行 Job（mock 渲染）→ bridge 打标记 → process_marked →
   步骤 succeeded、Goal completed；会话中有完成消息；GET goal 接口与数据库一致。
2. test_restart_mid_flow：在 Job 执行完、process_marked 之前"重启"——丢弃所有 Session 和 sessionmaker，
   新建 sessionmaker 指向同一个 tmp 库，清空所有恢复标记（模拟标记丢失），调用 sweep + process_marked →
   流程完成，且 Job 表中每个实体只有 1 条 Job。
3. test_duplicate_chat_request：同一句话连续发两次 → 1 个 Goal、3 条审批、最终 3 个 Job。
4. test_refresh_consistency：每个阶段调用 GET /api/agent/{session_id}/goal，与直接查库的结果逐字段一致。
5. test_reject_one_approval：拒绝其中 1 条 → 最终 lifecycle_state=="waiting_user"，另外 2 个资产成功生成，
   回复中包含被拒绝的实体和审批意见。
6. test_job_failure：让 1 个 Job 失败（mock 抛错）→ waiting_user，error_code 以 "job_failed:" 开头，没有自动重新提交。
7. test_flag_off_untouched：开关关闭时，同样的输入走旧链路，数据库中没有任何 agent_goal 记录。

完成标准：以上全部通过；同时把 Phase A 中标记为 xfail 的 test_p0_9_resume_has_callers 改为正常测试并通过。
```

---

## B11 · 收尾：文档、开关策略、清理

```text
任务：
1. docs/agent-runtime/target-architecture.md：更新为 Phase B 实际落地的架构图（标志位 + 兜底扫描、
   Goal↔AgentTask↔统一 Task、workspace_event 的 agent.* 事件）。
2. docs/API.md：补充 B8、B9 的新接口，以及助手消息 payload 中 route_action="agent_runtime" 的字段说明。
3. docs/agent-runtime/implementation-plan.md：勾选 B0–B11；写明开关策略——
   默认关闭；在本地用示例项目《游乐场》手动验收后，把 .env.example 中的 DLAB_AGENT_RUNTIME_CHAT_ENABLED 改为 true；
   保留开关至少到 Phase C 完成。
4. 在 README 的 AI 导演台部分加一段说明（3–5 行）：可以让 Agent 批量生成资产或分镜图，审批后自动继续，可暂停、取消。
   不要夸大能力（例如不要写"一句话完成整部短剧"）。
5. 遗留问题汇总：列出 Phase B 中发现、但按约束没有修的问题（例如 approvals.py 内部 commit、
   resolve_confirmation 写事件时 session_id 取不到值、流程工具和返工工具尚未接入 Runtime），作为 Phase C/D 的输入。

完成标准：`pytest -q` 全量失败数不多于 Phase A 结束时；ruff 无新增告警；文档链接有效。
```

---

## 附：Phase B 完成后的状态（供 Phase C 衔接）

| 已具备 | 仍缺（后续阶段） |
|---|---|
| `/chat` 可以把批量资产生成、分镜图生成交给 Runtime（开关控制） | 通用的状态驱动 Planner、整条生产 DAG（C） |
| 审批 → 自动执行 → Job → 自动继续，全程不依赖页面在线 | 流程工具、返工工具接入 Runtime（C） |
| 重启后由兜底扫描恢复，不重复创建 Job | 审核开关、审核绑定版本、拒绝后重新生成（D） |
| Goal 状态接口、暂停 / 继续 / 取消 | Workbench 前端展示目标卡片和计划步骤（E） |
| agent.* 事件写入 workspace_event | Goal 级预算、指标、场景评测进 CI（C7 / F） |
