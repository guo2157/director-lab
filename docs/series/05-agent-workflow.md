# 05 Agent 工作流（Episode Writing Agent）

> 事实来源：`backend/app/worker/series_executor.py`（311 行）、`backend/app/services/series/continue_service.py`（130 行）、`backend/app/services/series/episode_writing_service.py`（193 行）、`backend/app/worker/scheduler.py:219-221`。

## 1. 定位与边界

Episode Writing Agent 是**系列续写的异步执行体**，负责「上下文 → 剧本草稿 → 连续性检查 → 待审核」四步，产出**待审核草稿**。

明确的边界：

| 能做 | 不能做 |
|------|--------|
| 读取已确认剧情事实与系列设定 | 直接把草稿当作正式剧情 |
| 生成剧本正文与事件候选 | 直接写 `story_events` 为 confirmed |
| 生成状态变更提案（proposal） | 直接修改 `series.state_version` |
| 触发连续性检查并落结果 | 绕过 StateCommitService 提交正式状态 |

> 正式剧情状态只能由 `state_commit_service.confirm_script` 在用户点击确认时写入。Agent 的产物是**候选**，`fact_status` 一律为 `proposed`。

## 2. 任务创建（同步侧）

入口：`POST /api/series/{series_id}/episodes/continue` → `continue_service.create_continue_task`。

### 2.1 前置校验

1. `series.status != 'active'` → `invalid_state_transition`（系列已归档，不能继续创作）
2. `episode.script_status == 'confirmed'` → `series_script_status_conflict`（该集剧本已确认，如需修改请创建新的草稿版本）

### 2.2 创建内容

`ensure_project(db, series, episode)` 确保剧集绑定一个生产 Project（复用既有 `project` 表，不新建第二套项目体系），随后：

1. 创建 `Task`：`task_type='series_continue'`，`title` 形如「续写《系列名》第 N 集」
2. 按 `STAGE_SEQUENCE` 顺序添加 4 个 `TaskStage`，串行依赖（`depends_on=[previous]`），`agent_name='episode_writing_agent'`
3. 创建 `Job`：`job_type='series_agent'`，`status='queued'`，`params` 含 `series_id` / `episode_id` / `task_id` / `user_instruction` / `direction` / `constraints`；`input_snapshot` 额外记录 `episode_number` 与 `series_state_version`
4. 剧集 `script_status` 置为 `draft`，提交事务

返回 `(task_id, job_id)`，接口响应 202。

### 2.3 阶段序列（代码原样）

```python
STAGE_SEQUENCE: tuple[tuple[str, str], ...] = (
    ("context_building", "构建续写上下文"),
    ("writing", "生成剧情大纲与剧本"),
    ("validating", "执行连续性检查"),
    ("awaiting_review", "等待剧本审核"),
)
```

## 3. 调度与执行（异步侧）

调度器按 `job_type` 分派：

```python
if job_type == "series_agent":
    from app.worker.series_executor import execute_series_agent_job
    return execute_series_agent_job(job_id)
```

### 3.1 执行前守卫

| 条件 | 处理 |
|------|------|
| Job 不存在 | 返回 `{ok: False, reason: 'job_not_found'}` |
| Job 状态非 `queued` | 返回 `{ok: False, reason: 'invalid_status:{status}'}`，避免重复执行 |
| 系列或剧集不存在 | `_fail` + `series_not_found` |
| 剧集剧本已确认 | `_fail` + `episode_already_confirmed`（**不覆盖既有确认版本**） |
| `user_instruction` 为空 | `_fail` + `invalid_request` |

通过后 `job.status='running'`、`started_at` 置位、`attempts += 1`，写事件 `series.started`。

### 3.2 阶段一：context_building

1. 剧集 `script_status` 置为 `analyzing`
2. 调用 `context_builder_service.build_and_persist_for_episode(...)` 构建并**落库** `episode_context_manifests`
3. 成功：阶段进度 100、状态 `succeeded`，结果含 `manifest_id` 与 `context_version`；写事件 `series.context_ready`（含 `context_hash`）
4. 失败：回滚并 `_fail(code='series_context_unavailable')`，错误信息截断 1000 字符

### 3.3 阶段二：writing

1. 调用 `episode_writing_service.generate_draft(db, episode, manifest, manifest_id)`
2. `LLMError` → `_fail(code=exc.code)`；其他异常 → `_fail(code='series_writing_failed')`
3. 成功：阶段结果含 `script_version_id` 与 `version`；写事件 `series.script_drafted`

`generate_draft` 内部：

- `build_user_prompt(manifest)` 把上下文序列化为 JSON 用户消息，含 `target_episode_number`、`series`、`series_bible`、`continuity_state`、`previous_episode`、`open_plot_threads`、`history_events`、`assets`、`user_instruction`、`constraints`、`allowed_event_types`
- 提示词明确要求「严格依据其中的**已确认事实**续写」
- `client.chat_json(SYSTEM_PROMPT, user_prompt)`，非 JSON 或空正文 → `llm_invalid_output`
- 解析 `script` / `outline` / `synopsis` / `scenes` / `characters` / `props` / `events` / `plot_thread_updates` / `self_check`
- 新版本号 = `max(version) + 1`（集内递增，不覆盖历史）
- 落库字段：`source_context_manifest_id`（可追溯）、`prompt_template_version`、`model`、`validation_result`（含 events 候选）
- 状态直接置为 `awaiting_review`，同时更新 `episode.script_status` 与 `script_current_version_id`

### 3.4 事件候选规范化（关键约束）

`_normalize_events` 对模型输出做强制收敛：

| 处理 | 规则 |
|------|------|
| 条数上限 | 最多 60 条 |
| 非 dict 条目 | 丢弃 |
| `event_type` 非法 | 强制回退为 `other` |
| `title` 为空 | 补为「事件 N」，截断 200 |
| `event_key` 为空 | 补为 `ev_{index:03d}`，截断 64 |
| `importance` | 非整数回退 3，并 clamp 到 1–5 |
| `fact_status` | **一律强制 `proposed`**，必须人工确认后才能成为正式剧情 |
| `involved_asset_names` | 截断 100 字符、最多 20 个 |

> 这条规则是「Agent 不得直接改正式连续性状态」的代码级保证：模型没有能力把事件写成 confirmed。

### 3.5 阶段三：validating

1. 调用 `episode_writing_service.run_continuity_check(db, series, episode, script_version)`
2. 异常 → `_fail(code='series_validate_failed')`
3. 成功：阶段结果含 `continuity_status`；写事件 `series.validated`，携带 `issue_count`

### 3.6 阶段四：awaiting_review

1. 标记阶段成功（结果含 `script_version_id`）
2. `job.status='succeeded'`，写入 `job.result`：`episode_id`、`episode_number`、`script_version_id`、`script_version`、`manifest_id`、`continuity_status`、`issue_count`
3. 写事件 `job.finished`（摘要「剧本草稿已生成，等待审核」）
4. 调用 `task_orchestrator.complete_task`，`result_data` 含 `result_type='series_script_draft'`、`episode_id`、`episode_number`、`script_version_id`、`continuity_status`、`issues`（最多 20 条）

## 4. 失败处理与兜底

- 每个阶段失败都会 `db.rollback()`，避免半成品写入
- `_fail` 统一写入 Job 失败态、Task 阶段失败、任务失败摘要
- 最外层 `except Exception` 兜底：记录 `logger.exception`，再尝试把 Job 置失败（`series_internal_error`）；若落错本身失败则不再抛出，避免阻塞 worker
- `finally: db.close()`

## 5. 状态流转总览

```
Job:     queued ──> running ──> succeeded
                          └──> failed

Episode.script_status:
  draft ──> analyzing ──> awaiting_review ──[用户确认]──> confirmed
                                                └──[新版本确认后]──> superseded
```

`production_status` 与上述流转**完全独立**，视频生产未完成不阻止下一集创作。

## 6. 前端可见性

| 观察点 | 数据来源 |
|--------|----------|
| 任务整体状态与耗时 | `GET /api/tasks/{task_id}` → `status`、`progress`、`elapsed_seconds` |
| 当前步骤 | 同接口 `current_step` / `current_message` |
| 阶段明细 | 同接口 `stages[]`（含 `stage_key`、`stage_name`、`status`、`progress`） |
| 产物版本 | 同接口 `result.script_version_id` |
| 连续性问题 | `GET /api/episodes/{id}/continuity/issues` |

前端续写页按 2.5s 轮询，进入终态自动停止。

## 7. 与单集生产 Agent 的区别

| 维度 | 单集生产链路 | 系列续写 Agent |
|------|-------------|----------------|
| 触发 | 项目页解析/生成/合成 | 系列详情页「续写」 |
| `Task.task_type` | parse / generate / compose / export | `series_continue` |
| `Job.job_type` | 各自既有类型 | `series_agent` |
| 产物 | 场景、分镜、视频 | 剧集剧本草稿 + 事件候选 |
| 是否写正式剧情 | 否 | 否（仅提案，正式写入需用户确认） |

## 8. 已知限制

1. 上下文构建与写作目前串行执行，未做并行加速。
2. 事件候选中的 `involved_asset_names` 为名称，需在审核阶段由 `assets/resolve` 协助人工映射为 `asset_id`，代码层面禁止按名称自动合并。
3. 重新生成（`regenerate`）复用同一执行器，但要求目标剧本版本未确认。
4. V1 只支持单主线（`mainline_id` 固定 `main`），多主线不在本期范围。
