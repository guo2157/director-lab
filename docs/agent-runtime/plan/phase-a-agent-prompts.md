# Director-Lab · Phase A「止血与正确性」编码 Agent 任务提示词

> 适用于 Codex / Claude Code / 其他编码 Agent。基线为 `guo2157/director-lab` HEAD（tree `1df9fe8b`）。行号以该版本为准，如有偏移请按函数名定位。
> 用法：每次会话先粘贴 **〇、通用上下文**，再粘贴**一个**任务（A0、A1……）。一个任务对应一个 PR / 提交，不要合并。
> 执行顺序：**A00（测试隔离）→ A0 → A12a（测试夹具）→ A1 → A2 → A3 → A4 → A5 → A6 → A7 → A8 → A9 → A10 → A11 → A12b（回归汇总）**
>
> ⚠️ 2026-11-26 实际执行中发现：`backend/alembic/env.py` 会用 `get_settings().database_url` 覆盖 Alembic Config 中的地址，
> 而 `conftest.py` 只重定向了数据目录、没有重定向数据库地址（默认值 `sqlite:///./data/director_lab.db` 在 backend/ 下运行时就是真实库）。
> 按原 A0 写法运行迁移测试会**直接修改真实库**。因此新增 A00，必须最先完成；只核对主库文件哈希不能发现问题（改动会先写进 WAL）。

---

## A00 · 测试隔离：数据库地址强制指向临时库（必须最先完成）

```text
1. backend/tests/conftest.py 最顶部、导入任何 app 模块之前：把 DLAB_DATABASE_URL 设为与 DLAB_DATA_DIR 同一临时根目录下的 SQLite 文件，并清空 Settings 缓存。
2. 增加 session 级 autouse 夹具：get_settings().database_url 解析后指向 backend/data/director_lab.db（按绝对路径比较）或不在临时目录下时，立即让整个测试会话失败。
3. 所有迁移测试通过环境变量 DLAB_DATABASE_URL 指向 tmp 库并清空配置缓存（只设置 alembic Config 无效），并断言迁移实际连接的地址就是 tmp 库。
4. 真实库核对改为只读比较：alembic_version，以及 SELECT sql FROM sqlite_master ORDER BY name 的结果哈希；不再以主文件哈希作为依据。
验收：tests/test_test_isolation.py——故意把 DLAB_DATABASE_URL 改回真实路径时，守卫夹具会使会话失败（用子进程运行 pytest 验证）。
```

---

## 〇、通用上下文（每个任务前都要粘贴）

```text
你在 Director-Lab 仓库（FastAPI + SQLAlchemy 2 + SQLite WAL + Alembic，后端在 backend/）中工作。
当前任务属于「Agent Runtime Phase A：止血与正确性」。目标是修复已提交的 Agent Runtime 代码中的缺陷，
让它本身正确、可测，**不在本阶段把它接入主对话链路**（那是 Phase B）。

相关文件：
- backend/app/services/agent_runtime.py      Agent Loop 入口（run_agent_loop / _execute_plan_loop / resume_*）
- backend/app/api/v1/agent_loop.py           调试接口 POST /api/agent/loop/run（SSE）、GET /status/{goal_id}
- backend/app/services/execution_manager.py  execute_step / 执行锁 / Task 映射
- backend/app/services/verifier.py           verify_step 五维验证
- backend/app/services/replanner.py          classify_error / replan
- backend/app/services/planner.py            build_plan / save_plan / get_next_pending_steps
- backend/app/services/goal_manager.py       get_or_create_goal / get_active_goal
- backend/app/services/agent_tools.py        TOOL_REGISTRY + execute_tool（唯一合法的工具执行网关）
- backend/app/models/agent.py                AgentGoal / AgentPlan / AgentPlanStep / AgentConfirmation
- backend/app/core/database.py               make_engine / SessionLocal / get_db（get_db 只 close，不 commit）
- backend/tests/conftest.py                  已有全局夹具（测试隔离、禁止真实 LLM / vision）

硬性约束（违反任何一条即视为任务失败）：
1. 工具只能通过 agent_tools.execute_tool 执行，禁止直接调用 TOOL_REGISTRY[name]["fn"]。
2. 禁止把 FastAPI 请求作用域的 Session（Depends(get_db)）传入其他线程。
3. Job 创建成功 ≠ 步骤成功。返回 job_id 的步骤只能进入 waiting_job。
4. 审批按步骤绑定：一个 approved 的 AgentConfirmation 只能放行它绑定的那个步骤。
5. 数据库迁移只增不删：新列一律可空或有安全默认值，必须写 downgrade；不修改现有业务表的已有列。
6. 测试只能使用 tmp_path 下的临时 SQLite（参考 tests/test_agent_tasks.py 的 db fixture），
   禁止连接 ./data/director_lab.db，禁止真实调用 LLM / ComfyUI。
7. 步骤状态词表统一为：pending / ready / running / waiting_job / waiting_approval / waiting_user /
   succeeded / failed / skipped / cancelled。禁止使用 "completed" 表示步骤成功。
8. 不改动 /api/agent/{session_id}/chat 主链路、前端、Worker 的行为（本阶段只修 Runtime 自身）。
9. 不新建事件表、不新建并行的工具注册表或执行器。
10. 只改与本任务相关的代码；发现任务范围外的问题，在 PR 描述「遗留问题」中列出，不要顺手修。

交付要求：
- 代码 + 测试一起提交；`cd backend && pytest -q tests/test_agent_runtime_*.py` 全绿，
  且不引入现有测试的新失败（如本地全量过慢，至少运行 tests/test_agent_*.py）。
- PR 描述包含：改了什么、为什么、如何验证、遗留问题。
- 完成后在 docs/agent-runtime/implementation-plan.md 中勾选对应条目（不要新建文档）。
```

---

## A0 · 数据模型补丁（一次迁移，为后续任务铺路）

```text
任务：新增一个 Alembic 迁移（down_revision 指向当前 head，先用 `alembic heads` 确认），补充 Phase A 需要的列，
并同步更新 app/models/agent.py。

新增列：
1. agent_plan_step.confirmation_id  String(32) nullable, index   —— 步骤绑定的 AgentConfirmation
2. agent_plan_step.job_id           String(32) nullable, index   —— 步骤产生的 Job（Phase B 恢复时按它反查）
3. agent_plan_step.step_key         String(64) nullable          —— 计划内语义 key（目前只在 plan_json 中，落库后丢失）
4. agent_goal.fingerprint           String(64) nullable, index   —— 目标指纹（A8 使用）
5. agent_plan.parent_plan_id        String(32) nullable          —— Replan 来源计划（A9 使用）

另外：
- AgentGoal 的 docstring / 状态机注释中加入 "superseded" 状态（新目标替代旧目标时使用），GOAL_STATUSES 同步加入。
- agent_plan_step.task_id 目前是 NOT NULL，planner 写入了空字符串 ""。本任务**不改列约束**，
  但在模型注释中写明："Runtime 步骤（plan_id 非空）task_id 恒为 ''，与 AgentTask 的关联通过 agent_goal / agent_task.goal_id"。

验收：
- `alembic upgrade head` 和 `alembic downgrade -1` 在一个空库和一个已执行到当前 head 的库上都能成功。
- 新增 tests/test_agent_runtime_migration.py：在 tmp 库上执行 upgrade → 检查 5 个新列存在 → downgrade → 列消失。
  **数据库地址必须通过环境变量 DLAB_DATABASE_URL 指向 tmp 库并清空配置缓存**——alembic/env.py 会覆盖 Config 中设置的 URL，
  只设置 Config 会让迁移落到真实库（见文件开头的说明，依赖 A00）。
- 现有 agent 相关测试不受影响。
```

---

## A12a · 测试基建：Runtime 专用夹具（先于 A1 完成）

```text
任务：为 Runtime 建立隔离的测试基建，并移除污染生产库的脚本式测试。

1. 新建 backend/tests/agent_runtime_fixtures.py（或放进 conftest.py，二选一，推荐独立文件后在 conftest 中导入）：
   - fixture `rt_engine(tmp_path)`：make_engine(f"sqlite:///{tmp_path/'rt.db'}") + Base.metadata.create_all
   - fixture `rt_sessionmaker(rt_engine)`：sessionmaker(bind=..., autoflush=False, expire_on_commit=False)
   - fixture `db(rt_sessionmaker)`：yield 一个 Session，结束时 close
   - fixture `fresh_session(rt_sessionmaker)`：返回一个工厂函数，用于「新连接验证是否真的落库」
   - fixture `patch_runtime_sessionmaker(monkeypatch, rt_sessionmaker)`：把 app.core.database.SessionLocal
     以及 agent_runtime / agent_loop 中引用的 SessionLocal 替换为 rt_sessionmaker（A1、A2 会用到）
   - helper `mk_project(db, *, with_script=False, with_parse=False, with_assets=0)`：参考 tests/test_agent_tasks.py::_mk_project
   - helper `fake_tool(monkeypatch, name, *, returns=None, raises=None, spec_overrides=None)`：
     临时向 TOOL_REGISTRY 注册或替换一个工具（含 input_model=None、preconditions=[]），用于构造成功、失败、返回 job_id、返回 confirmation_id 等场景；
     测试结束自动还原。
   - helper `mk_job(db, project_id, status="queued")`：创建一条真实 Job 记录，供 verifier 校验。

2. 删除或改写 backend/tests/test_agent_loop_main.py：
   - 该文件在模块顶层执行 `create_engine("sqlite:///./data/director_lab.db")`，pytest 收集时就会连接真实库。
   - 改写为 tests/test_agent_runtime_loop.py，使用上面的 fixtures 和真正的 assert；保留原有 5 个场景的意图，
     但**删除「场景2：复用同一 plan_id」这条断言**（这是 A8 要修复的错误行为），改为 TODO 注释并指向 A8。
3. 同样检查 tests/test_agent_loop_e2e.py：如果有模块级的真实库连接，按同样方式改写。
4. 新增一个守卫测试 tests/test_no_real_db_in_tests.py：扫描 tests/ 下所有 .py，
   断言不存在 "data/director_lab.db" 字面量（本文件除外）。

验收：`pytest -q tests/test_agent_runtime_loop.py tests/test_no_real_db_in_tests.py` 通过；
运行前后 backend/data/director_lab.db 的 mtime 不变（在 PR 描述中说明如何验证）。
```

---

## A1 · 事务边界：Runtime 入口自管 Session 与提交（P0-1）

```text
问题：agent_runtime / planner / execution_manager / replanner / goal_manager 中没有任何 commit()，
而 core/database.py::get_db 结束时只 close()。经 API 调用时，Goal、Plan、Step、执行日志会在请求结束后全部回滚。

要求：
1. 约定事务边界（写进 agent_runtime.py 模块 docstring）：
   - 内部函数（goal_manager / planner / execution_manager / verifier / replanner 的所有函数）只 flush，不 commit。
   - 只有 Runtime 对外入口负责提交：run_agent_loop、resume_after_job、resume_after_approval，
     以及下面新增的 run_agent_loop_in_new_session。
2. 在 _execute_plan_loop 中，每个步骤到达状态转换点后提交一次：
   execute_step 返回 → verify_step → (replan) → db.commit()。
   Goal 创建 / 复用、Plan 保存之后也各提交一次。这样进程崩溃时，已完成的步骤不会丢失。
3. 任一步骤抛出未捕获异常时：db.rollback()，然后在新事务中把该步骤标记为 failed（error_code=type(exc).__name__），
   写一条 agent_execution_log，再 commit，最后让 run_agent_loop 返回 status="failed"，不要向上抛出。
4. 新增 run_agent_loop_in_new_session(session_factory=SessionLocal, **kwargs) -> AgentLoopResult：
   内部 `with session_factory() as db:` 调用 run_agent_loop，供后台线程使用（A2 依赖它）。
5. 不改 get_db 的行为（全仓库都依赖它）。

验收测试 tests/test_agent_runtime_tx.py：
- test_loop_persists_across_sessions：用 fake_tool 注册一个返回 {"ok":1} 的只读工具，并让 planner 生成只含该工具的计划
  （可 monkeypatch planner.build_plan 返回固定 Plan）；run_agent_loop 后，用 fresh_session() 新开连接查询：
  agent_goal、agent_plan、agent_plan_step(status=succeeded)、agent_execution_log 均存在。
- test_crash_midway_keeps_completed_steps：两个串行步骤，第二个工具 raises=RuntimeError；
  新连接查询：第一步 succeeded，第二步 failed，run_agent_loop 返回 failed 且没有抛出异常。
- test_internal_functions_do_not_commit：在 db 上 monkeypatch commit 计数，单独调用 planner.build_and_save_plan、
  execution_manager.execute_step，断言 commit 次数为 0。
```

---

## A2 · 调试接口的线程与 SSE 桥接（P0-2、P0-3）

```text
问题（backend/app/api/v1/agent_loop.py）：
- L132-162：请求作用域的 db 通过 threading.Thread(target=asyncio.run, ...) 传进新线程。
- L86：回调在后台线程里调用 asyncio.get_event_loop()，拿到的是子线程自己的 loop，再对主 loop 的 Queue 执行 call_soon_threadsafe，事件可能丢失。
- seq 计算方式（seq_counter[0] - queue.qsize()）不可靠。

要求：
1. 路由函数中先取 `main_loop = asyncio.get_running_loop()`，回调闭包里使用 main_loop.call_soon_threadsafe(queue.put_nowait, ev)。
2. 后台执行改为 `main_loop.run_in_executor(None, functools.partial(run_agent_loop_in_new_session, ...))`
   或 asyncio.to_thread；**不要**再用 threading.Thread + asyncio.run。去掉路由参数中的 Depends(get_db)。
3. 每个事件在入队时就分配单调递增的 seq（放进 AgentLoopEventOut），SSE 的 id 直接使用它。
4. 后台任务结束（无论成功或异常）时，保证推送一个终止事件（completed/failed/blocked/waiting*/waiting），
   sse_generator 收到终止事件后结束。注意："waiting" 状态当前不在终止集合里，会导致连接一直挂起，需要补上。
5. 客户端断开时（request.is_disconnected()）停止推送，但不取消后台执行（执行状态已落库，可通过 status 接口查询）。
6. 接口保留为调试用途：在 config 中新增 `agent_loop_debug_enabled: bool = False`（环境变量 DLAB_AGENT_LOOP_DEBUG_ENABLED）；
   关闭时 POST /run 返回 404。main.py 的路由注册保持不变。

验收测试 tests/test_agent_loop_api.py（使用 httpx.AsyncClient + ASGITransport，配合 patch_runtime_sessionmaker）：
- test_sse_emits_ordered_events_and_terminates：开启 debug 开关，单步计划；读取完整 SSE 流，断言事件序列至少包含
  step_started、step_completed，并以终止事件结尾；seq 严格递增；流在 5 秒内结束。
- test_disabled_by_default：不设开关时 POST /run 返回 404。
- test_no_request_session_in_thread：monkeypatch run_agent_loop_in_new_session，断言它收到的是 session_factory 而不是 Session 实例。
```

---

## A3 · 进度回调透传与结果汇总顺序（P0-4）

```text
问题（agent_runtime.py）：
- run_agent_loop 调用 _execute_plan_loop 时没有传 progress_callback，因此 step_started / step_completed / step_failed 从未发出。
- 最终 _emit(result.status, {...completed/failed/total}) 在 result.completed_steps 等字段赋值之前调用，统计值永远为 0。
- 文档中声明的 goal_created / plan_generated 事件也从未发出。

要求：
1. run_agent_loop → _execute_plan_loop 透传 progress_callback；resume_after_approval 也透传（新增可选参数）。
2. 在 Goal 解析后发 goal_created（新建）或 goal_resumed（复用），Plan 获取或生成后发 plan_generated（附 plan_id、revision、steps 摘要）。
3. 先把 loop_result 全部写入 result，再发最终事件；最终事件的 payload 使用 result.to_dict()。
4. 所有事件名集中定义为常量（例如 LoopEvent 枚举），agent_loop.py 的终止事件集合引用这些常量，不再手写字符串。
5. step_skipped（RETRY 重置、SKIP）和 step_waiting（waiting_job / waiting_approval）也要发出。

验收测试（追加到 tests/test_agent_runtime_loop.py）：
- test_progress_events_order：用列表收集回调事件；两步成功计划 → 断言事件顺序为
  [goal_created, plan_generated, step_started, step_completed, step_started, step_completed, completed]，
  最终事件 payload 中 completed_steps == 2、total_steps == 2。
- test_progress_callback_exception_is_isolated：回调抛异常不影响 Loop 结果。
```

---

## A4 · Executor 只走 execute_tool 网关（P0-5）

```text
问题（execution_manager.py L373-386）：execute_step 直接调用 `tool_fn(db, **args)`，
跳过了 agent_tools.execute_tool 中的 input_model 校验、preconditions、错误分类。
此外，request_generate 等工具需要 session_id / project_id，planner 生成的 args 里没有 session_id。

要求：
1. execute_step 改为调用 `agent_tools.execute_tool(db, tool_name, approved=<A7 决定>, **args)`，返回 ToolResult。
   删除 _get_tool_fn 以及直接调用 fn 的所有代码。
2. 服务端参数注入（与主对话链路保持一致，先在 api/v1/agent.py 中找到现有的 project_id / session_id 注入逻辑并复用，
   必要时抽成 agent_tools 中的公共函数 inject_server_args(spec, args, *, session_id, project_id)）：
   - 工具 input_model 有 session_id / project_id 字段时，由服务端覆盖写入，忽略计划中的同名值（防止跨项目）。
3. PolicyDecision 判定不在 execute_step 中重复实现：execute_tool 已经调用 evaluate_tool_policy。
   execute_step 只根据 ToolResult.error_code 分支处理（approval_required 的分支由 A5 / A7 处理，本任务先映射为 waiting_approval）。
4. ToolResult → 步骤结果映射：
   - ok=True                                   → 交给 A5 的结果分流（本任务先按 succeeded 处理）
   - error_code in {tool_invalid_args, tool_not_found, precondition_failed} → failed，error_code 原样写入 step.error_code
   - error_code == tool_internal_error / 其他   → failed
   - step.result 中保存 ToolResult 的结构化字段（ok / error_code / error_message / data / duration_ms）
5. 删除 _map_step_to_task 对每个步骤都创建 Task + TaskStage 的行为：同步步骤不再创建 Task。
   （需要 Job 的步骤由 A5 用 task_orchestrator.attach_job_stage 关联。）注意同步移除 _complete_task_stage 的调用。
6. 删除 ExecutionStatus.COMPLETED（统一词表）。

验收测试 tests/test_agent_runtime_executor.py：
- test_uses_execute_tool_gateway：monkeypatch agent_tools.execute_tool 计数，执行步骤后计数为 1。
- test_precondition_failure_marks_failed：使用真实工具 list_project_shots，传入不存在的 project_id → step=failed，
  error_code=precondition_failed（或该工具实际返回的分类码）。
- test_invalid_args_marks_failed：fake_tool 设置 input_model 为必填字段模型，计划 args 缺字段 → error_code=tool_invalid_args。
- test_server_injects_project_and_session：计划 args 中 project_id="prj_other" → 实际传给工具的是 run 的 project_id。
- test_sync_step_creates_no_task：同步工具成功后，task 表中没有新增记录。
```

---

## A5 · 结果分流：Job → waiting_job，审批 → waiting_approval（P0-6）

```text
问题：execute_step 在工具一返回时就标记 succeeded，从不进入 waiting_job，违反「Job 创建成功 ≠ 完成」。

要求：
1. 在 execute_step 中，根据 ToolResult.data 分流（按以下优先级）：
   a. data 含 job_id（例如 run_generate 返回的 job_id）→
      step.status = waiting_job，step.job_id = data["job_id"]；
      调用 task_orchestrator.create_task + attach_job_stage(job_id=..., stage_key=step.step_key or tool_name)，
      让现有 bridge_job_event 能闭环；如果 attach_job_stage 发现 Job 已终态，按 Job 终态直接收敛（succeeded / failed）。
   b. data 含 confirmation_id 且对应 AgentConfirmation.status == "pending"（例如 request_generate）→
      step.status = waiting_approval，step.confirmation_id = data["confirmation_id"]。
   c. error_code == "approval_required"（工具本身需要审批，但当前没有可用的批准记录）→
      step.status = waiting_approval，step.error_code = "approval_required"（Phase B 负责自动创建审批记录，本任务只做状态映射）。
   d. 其他 ok=True → succeeded。
2. 批量生成工具（batch_request_generate）返回多个 confirmation_id 时：本任务把所有 id 存入
   step.result["confirmation_ids"]，step.confirmation_id 取第一个，状态为 waiting_approval；在 PR「遗留问题」中注明 Phase C 会把批量步骤拆分为逐实体步骤。
3. _execute_plan_loop 处理三种等待：
   - waiting_job → 发 step_waiting 事件，返回 status=waiting_job，wait_step_id=step_id（不再经过 Replanner 的 WAIT 分类）
   - waiting_approval → 返回 status=waiting_approval
   - 等待步骤之外仍有其他可并行的就绪步骤时，先执行完它们再返回（不阻塞无关分支）
4. get_next_pending_steps 中依赖满足的判定只认 succeeded / skipped（保持不变），waiting_* 不算满足。

验收测试（追加到 tests/test_agent_runtime_executor.py）：
- test_job_result_enters_waiting_job：fake_tool 返回 {"job_id": mk_job(...).id}（Job 状态为 queued）→
  step=waiting_job、step.job_id 已写入、存在 task_stage.job_id == 该 job；Loop 返回 waiting_job。
- test_job_already_succeeded_converges：Job 已是 succeeded → 步骤直接 succeeded。
- test_confirmation_result_enters_waiting_approval：fake_tool 返回 {"confirmation_id": <pending confirmation>}（在测试中直接插入
  AgentConfirmation 记录）→ step=waiting_approval、step.confirmation_id 已写入。
- test_parallel_branch_not_blocked：A、B 两个无依赖步骤，A 返回 job_id，B 是同步工具 → 循环返回时 B 已经 succeeded。
```

---

## A6 · Verifier 以真实状态判定（P0-7）

```text
问题（verifier.py L157-165）：verify_state 中的 step_status 检查无条件判 passed；
因此工具失败、step=failed 的步骤，仍可能整体判为 verified=True，并计入 completed。

要求：
1. verify_state：
   - step.status == succeeded → passed
   - step.status in {waiting_job, waiting_approval, waiting_user, running} → 新状态 "pending"
     （StepVerificationResult.finalize 中 pending → verified=False、overall="pending"，不触发 Replan）
   - step.status in {failed, cancelled} → failed
   - 含 job_id 时：Job 必须存在、归属同一 project，且状态为 succeeded 才 passed；
     queued/running → pending；failed/cancelled → failed（复用 execution_verifier.verify_job_submission，并补充终态判定）。
2. 从 step.result 中读取 ToolResult：ok=False → 增加 failed 检查 tool_result_ok。
3. finalize：保持现有规则，新增 pending 分支；「全部 skipped → verified=True」这条规则改为 verified=False、overall="skipped"
   （没有任何检查通过不能算验证通过）。
4. _execute_plan_loop 中：
   - verification.overall == "pending" → 按 A5 的等待处理，不调用 Replanner
   - verified=True → completed += 1
   - 其他 → Replanner
5. verify_plan_completion：全部步骤 succeeded/skipped 才 all_verified；存在 failed 且无可执行步骤时返回
   {"all_verified": False, "failed_steps": [...], "partial": True}，供 A10 使用 partially_completed。

验收测试 tests/test_agent_runtime_verifier.py：
- test_failed_step_not_verified：step=failed → verified=False。
- test_waiting_job_is_pending：step=waiting_job + Job 为 queued → overall=pending。
- test_job_failed_fails_verification：Job 为 failed → verified=False。
- test_job_other_project_fails：Job 的 project_id 与 run 的不一致 → failed。
- test_all_skipped_not_verified。
- test_loop_does_not_count_failed_as_completed：回归 P0-7——工具 raises 时，Loop 结果中 completed_steps == 0。
```

---

## A7 · 审批按步骤绑定（P0-8）

```text
问题：
- execution_manager.execute_step 只用工具 spec 判定是否需要审批，忽略计划中的 step.requires_approval。
- run_agent_loop / resume_after_approval 接收单个 approved 布尔值，并把它传给整个后续循环——一次审批会放行后续所有高风险步骤。

要求：
1. 删除 run_agent_loop、_execute_plan_loop、execute_step 的 `approved` 参数（调试接口 / 测试同步修改）。
2. 新增 execution_manager._step_is_approved(db, step) -> bool：
   step.confirmation_id 非空，且对应 AgentConfirmation.status == "approved"，
   且该审批尚未被执行过（使用 approvals.py 现有的「已执行」标记字段 / mark_task_finished 逻辑判断，
   先阅读 approvals.py 确认字段名；如果没有可用字段，在 PR 中说明并改用 step.result 中的 executed_confirmation_id 记录）。
3. 需要审批的判定 = 工具 spec.requires_approval OR spec.risk_level == "high" OR step.requires_approval。
   - 需要审批且 _step_is_approved=False → 不执行，step=waiting_approval
   - 需要审批且已批准 → execute_tool(..., approved=True)，执行后记录该审批已被本步骤消费
4. resume_after_approval(db, *, goal_id, step_id, confirmation_id, ...)：
   - 校验 step.confirmation_id == confirmation_id，否则返回 {"resumed": False, "error": "confirmation_mismatch"}
   - 审批 approved → step 重置为 pending（不修改 step.requires_approval！现有代码把它改成 False，必须删除）
   - rejected / revision_requested → step=waiting_user，error_code="approval_rejected"（Phase D 再接 Replan）
   - 然后继续循环（不传 approved）
5. 被 resume 的 goal 通过 goal_id 查询（get_goal_by_id），不要再用 get_active_goal(session_id or "")。

验收测试 tests/test_agent_runtime_approval.py：
- test_step_requires_approval_flag_respected：工具 spec 不需要审批，但 step.requires_approval=True → waiting_approval。
- test_one_approval_releases_only_its_step：两个高风险步骤 S1、S2，只批准 S1 的 confirmation → resume 后 S1 执行，S2 仍为 waiting_approval。
- test_confirmation_mismatch_rejected。
- test_reject_goes_waiting_user。
- test_approval_not_reusable：同一个 confirmation 执行过一次后，再次 resume 不会重复执行（工具调用计数仍为 1）。
```

---

## A8 · 目标指纹：新目标不复用旧计划（P0-10）

```text
问题：
- goal_manager.get_or_create_goal 在同一 session 有 active 目标时，直接覆盖 description / constraints 并复用。
- agent_runtime._get_or_create_plan 只要存在 active plan 就复用。
  结果是：用户把目标从「查看进度」改成「生成分镜图」，系统仍然执行旧计划。

要求：
1. 新增 goal_manager.compute_fingerprint(*, project_id, goal_type, target_stage, scope) -> str（sha1 前 16 位）。
   Phase A 没有结构化目标解析，暂时按以下方式推导：
   target_stage = planner 对 description 的分类结果（把 build_plan 中 is_query / is_analyze / is_generate 和
   keyframe / clip 的判定抽成 planner.classify_description(description) -> str，返回 query/analyze/generate_keyframe/generate_clip/generate_default/default），
   scope = constraints 中的 scope 字段（没有则为 ""）。在注释中注明「Phase C 由结构化 Goal 解析替换」。
2. get_or_create_goal：
   - 存在 active 目标且 fingerprint 相同 → 复用（更新 updated_at；description 不覆盖，最新描述追加到 constraints["history"]，最多保留 10 条）
   - 存在 active 目标但 fingerprint 不同 → 旧目标 status=superseded（发 goal_superseded 事件），新建目标
   - project_id 不同视为不同目标
3. _get_or_create_plan 不变（新目标自然没有旧计划）；但如果复用的目标的 active plan 中所有步骤都已终态（succeeded/failed/skipped/cancelled），
   本次调用应生成新的 revision（previous_plan=旧计划），而不是直接返回「已完成」。
4. 更新 A12a 中留下的 TODO：「场景 2」改为断言——相同意图复用同一 goal 和 plan；不同意图生成新 goal，旧 goal 变为 superseded。

验收测试 tests/test_agent_runtime_goal.py：
- test_same_intent_reuses_goal_and_plan
- test_different_intent_supersedes_goal
- test_different_project_new_goal
- test_description_history_capped_at_10
```

---

## A9 · Replan 产生新 revision 并复用已完成步骤（P0-11）

```text
问题（replanner.py L252-268，planner.py L636）：
- replan 调用 build_and_save_plan 时没有传 previous_plan，revision 恒为 1。
- 只给描述加了 "[Replan vN]" 前缀，关键词匹配结果不变，新计划与旧计划完全相同，失败会重复发生。
- 失败步骤的错误信息不会影响新计划。

要求：
1. replan 中：previous = planner.get_latest_plan(db, goal_id)；新计划通过 build_and_save_plan(..., previous_plan=previous) 生成，
   revision = previous.revision + 1，新 AgentPlan.parent_plan_id = previous.plan_id。
2. 新增 planner.carry_over_steps(db, old_plan_id, new_plan)：新计划中 step_key 与旧计划相同、且旧步骤已 succeeded 的，
   直接以 succeeded 状态落库（result 复制，并在 result 中标记 carried_over_from=<old_step_id>），不重新执行。
   save_plan_steps 同时写入 step_key 列（A0 新增）。
3. 失败上下文：本阶段规则 Planner 无法真正「换方案」，因此按错误类别处理：
   - error_code in {tool_invalid_args, precondition_failed, tool_not_found} 且新计划中同一 step_key 的工具和参数与旧计划完全相同 →
     不生成新计划，直接 action=FAIL（或 WAIT_USER，见下），原因写「规则计划无法自动修正：<error>」。
     这样可以避免「生成同样的计划 → 同样失败 → 重复 3 次」。
   - 新增 ReplanAction.WAIT_USER：precondition_failed（例如缺少剧本、未解析）→ step=waiting_user，Loop 返回 waiting_user，
     message 中给出 precondition 的提示文本。
4. 超过 MAX_REPLAN_PER_GOAL 时，统计方式改为按 parent_plan_id 链长度，而不是 superseded 数量（A8 的 supersede 不应计入 replan 次数）。
5. classify_error 改为优先使用 error_code（ToolResult 的分类码），只在没有 error_code 时才回退到 message 关键词；
   删除 "invalid"、"pending"、"running" 这类过宽的关键词（例如 "invalid" 会把很多业务错误误判为 REPLAN）。
   建立显式映射表 ERROR_CODE_ACTIONS: dict[str, ReplanAction]，并为每个 error_code 写单测。

验收测试 tests/test_agent_runtime_replan.py：
- test_replan_increments_revision_and_links_parent
- test_carry_over_succeeded_steps_not_reexecuted：工具调用计数验证
- test_identical_plan_fails_fast：参数错误 → 一次后立即 FAIL，工具调用计数为 1，不是 4
- test_precondition_failed_waits_user
- test_error_code_mapping_table：参数化覆盖 ERROR_CODE_ACTIONS 的所有键
- test_supersede_not_counted_as_replan
```

---

## A10 · 状态接口、终态与词表收敛（P0-12 及相关）

```text
问题：
- agent_loop.py L184：GET /status/{goal_id} 调用 get_active_goal(db, goal_id)，而该函数的参数是 session_id → 恒返回 404。
- check_goal_completion 只看 plan.status，但 plan.status 从未被置为 completed；Goal 完成与否和步骤无关。
- 有失败步骤、且无可执行步骤时，Loop 返回 "completed / 无可执行步骤"。

要求：
1. status 接口改用 get_goal_by_id；返回 goal（含 status、fingerprint）、最新 plan（revision、status）、steps（含 status / job_id / confirmation_id / error_code）、
   最近 20 条 execution_log。新增 get_goal_status_by_id(db, goal_id) 实现，get_goal_status(session_id) 保留并内部调用它。
2. Loop 终态规则（集中在一个函数 _derive_loop_status(steps) 中，便于测试）：
   - 全部 succeeded/skipped → completed；plan.status=completed；goal → completed
   - 存在 waiting_approval → waiting_approval；存在 waiting_job → waiting_job；存在 waiting_user → waiting_user
     （优先级：waiting_user > waiting_approval > waiting_job）
   - 无等待、无可执行，存在 failed 且存在 succeeded → partially_completed；plan.status=failed；goal 保持 active（可继续）
   - 无等待、无可执行，且全部失败 → failed；goal → failed
   - 有 pending 但依赖无法满足（上游 failed/cancelled）→ 这些 pending 步骤标记为 skipped，error_code="upstream_failed"，再重新判定
3. check_goal_completion 改为基于最新 plan 的步骤状态（复用 _derive_loop_status），不再只读 plan.status。
4. AgentGoal 状态新增 partially_completed 的展示语义（goal.status 本身保持 active，在 status 接口中返回 loop_status 字段）。
5. 全仓库检索 "completed" 作为步骤状态的用法（execution_manager、verifier、planner、agent_runtime），统一改为 succeeded。

验收测试 tests/test_agent_runtime_status.py：
- test_status_endpoint_by_goal_id（API 层）
- test_derive_loop_status：参数化覆盖上面所有分支
- test_upstream_failed_skips_dependents
- test_partially_completed_keeps_goal_active
```

---

## A11 · 原子执行锁（P1）

```text
问题（execution_manager.py L88-114）：_acquire_step_lock 是「读状态 → 判断 → 写 running」，
API 线程与（Phase B 的）Worker 同时调用时存在竞态，同一步骤可能执行两次。
死锁检测把超时的 running 步骤直接改成 failed，可能误伤仍在运行的同步工具。

要求：
1. 改为条件 UPDATE：
   stmt = update(AgentPlanStep).where(AgentPlanStep.id == step_id,
            AgentPlanStep.status.in_(["pending","ready"])).values(status="running", started_at=now, lock_owner=<uuid>)
   rowcount == 1 才算拿到锁。
   lock_owner 需要新列——如果 A0 已合并，在本任务中新增一个小迁移补充 agent_plan_step.lock_owner String(36) nullable。
2. 超时回收单独写成 reclaim_stale_steps(db, now) 函数（不在获取锁时顺带执行）：
   running 超过 timeout_seconds×2（缺省 1800s）的步骤 → 回到 pending 并 retry_count+1；retry_count 达到 max_retries → failed(error_code="lock_timeout")。
   Phase A 只提供函数和测试，不接入调度（Phase B 由 Worker 调用）。
3. 释放锁时校验 lock_owner，防止 A 线程释放 B 线程的锁。
4. 同一 Goal 并发调用 run_agent_loop：新增 Goal 级锁（agent_goal 上的条件 UPDATE，字段 locked_until DateTime nullable，同样放进小迁移），
   拿不到锁时返回 status="busy"，不执行任何步骤。

验收测试 tests/test_agent_runtime_lock.py：
- test_concurrent_acquire_only_one_wins：两个独立 Session（同一个 tmp 文件库）对同一步骤并发 acquire（ThreadPoolExecutor），恰好一个成功。
- test_release_requires_owner
- test_reclaim_stale_steps：retry 与 failed 两个分支
- test_goal_lock_busy：持有 Goal 锁期间，第二次 run_agent_loop 返回 busy，工具调用计数为 0
```

---

## A12b · 回归汇总与文档收尾（最后执行）

```text
任务：Phase A 全部合并后进行收尾。

1. 新增 tests/test_agent_runtime_regression.py，每个 P0 一条命名回归用例（可以调用前面各测试文件中的 helper），用例名带缺陷编号：
   test_p0_1_persisted / test_p0_2_no_request_session_in_thread / test_p0_3_sse_events_delivered /
   test_p0_4_progress_events / test_p0_5_gateway_only / test_p0_6_job_waits / test_p0_7_failed_not_verified /
   test_p0_8_approval_per_step / test_p0_10_new_intent_new_goal / test_p0_11_replan_revision /
   test_p0_12_status_by_goal_id / test_p0_13_no_real_db
   （P0-9「resume 无调用方」属于 Phase B，这里写 pytest.mark.xfail(reason="Phase B") 占位。）
2. 增加一个静态守卫测试：用 ast 扫描 app/services/execution_manager.py、agent_runtime.py，
   断言不存在对 TOOL_REGISTRY[...]["fn"] 或 spec["fn"] 的调用。
3. 运行 `ruff check backend/app/services backend/tests` 无新增告警。
4. 更新 docs/agent-runtime/implementation-plan.md：新增「Phase A 止血」小节，逐条勾选 A0–A12，
   并列出 Phase B 的前置条件（resume 调用方、Worker 消费者、/chat 接入）。
5. 更新 docs/agent-runtime/audit-report.md 末尾，增加「2026-11 Phase A 修复记录」表：缺陷编号 → PR / 提交 → 回归用例名。

完成标准：
- `pytest -q tests/test_agent_runtime_*.py tests/test_agent_loop_api.py` 全绿；
- 全量 `pytest -q` 失败数不多于修复前（在 PR 中附修复前后的统计）；
- backend/data/director_lab.db 在测试前后没有被修改。
```

---

## 附：Phase A 完成后的状态（供 Phase B 衔接）

| 已具备 | 仍缺（Phase B） |
|---|---|
| Runtime 正确落库、按步骤提交、可从新连接读取 | `bridge_job_event` / 审批裁决调用 resume |
| 工具只走网关；Job / 审批进入正确的等待态 | Worker 消费 `agent.resume_requested` 事件，推进 tick |
| Verifier、Replanner、终态判定可信 | `/chat` 主链路接入 Goal Manager → tick |
| 审批按步骤绑定、不可复用 | `approval_required` 时自动创建 AgentConfirmation |
| 原子步骤锁、Goal 锁、超时回收函数 | 启动对账：waiting_job 且 Job 已终态时补发恢复 |
| 调试接口的 SSE 可用（默认关闭） | 前端展示、`agent.*` 事件写入 outbox |
