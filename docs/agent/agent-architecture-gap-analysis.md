# Director-Lab Agent 架构差距分析

- 分析日期：2026-10-27 14:00–14:05 (UTC+8)
- 分析性质：**纯只读对照分析，本轮零代码改动、零迁移、零依赖新增**
- 对照基准：《单 Agent 具体实现方案》20 节
- 被分析对象：`/home/ubuntu/director-lab`（main @ 0ede9d9 + 未提交改动）
- 证据来源：`app/api/v1/agent.py`、`app/services/agent_tools.py`、`app/models/agent.py`、`app/models/production.py`、`app/api/v1/events.py`、`app/worker/scheduler.py`、`app/core/config.py`、`app/integrations/llm.py`，以及实测运行环境版本与依赖清单

---

## 1. 总体判定

| 维度 | 判定 |
| --- | --- |
| 整体形态 | **线性管道，不是 Agent 闭环** |
| 现有链路 | 接收请求 → 拼固定 system prompt → 硬编码调 2 个只读工具 → 调 LLM → 落库回复 |
| 方案要求的闭环 | 接收请求 → 识别意图 → 组装上下文 → 制定计划 → 调用工具 → 验证结果 → 重规划或结束 → 更新记忆 |
| 差距定位 | 缺「意图 → 计划 → 验证 → 记忆」四段；中间「工具调用」一段虽已存在但**无审计、无 Schema、无风险元数据** |
| 落地难度 | 中低。异步任务底座、SSE 通道、幂等键、重试上限、状态持久化**均已具备可复用基础**，不需要引入新框架 |

一句话结论：**方案值得借鉴，但不能照搬技术栈；真正该补的是「工具调用可审计」与「计划-验证」两段，而不是推倒重来。**

---

## 2. 技术栈等价替换（不可照搬的 5 项）

| 方案推荐 | 项目实际 | 替换结论与影响 |
| --- | --- | --- |
| PostgreSQL | **SQLite 3.45.1**（SQLAlchemy 2.1.6 + Alembic，head `b2c3d4e5f6a7`，WAL 模式） | 单机单进程定位，够用。无法承载高并发写与行级锁，但本项目 `render_concurrency=1`，非瓶颈 |
| pgvector | **未安装**（实测无 pgvector/redis/celery/temporal 依赖） | **必须替换**。建议第一阶段完全不引入向量检索；如需语义召回，优先 `SQLite FTS5` 全文检索，或数据量小时用内存余弦相似度。不要为记忆层引入向量库 |
| Redis | **无**；用 `runtime_settings.json` + 内存缓存（`_settings_cache` + mtime 失效） | 会话缓存非必需，SQLite 直读足够。审批等短时状态可直接落表，无需 KV |
| Celery / RQ / Temporal | **自研 worker + scheduler**（`app/worker/`，含优先级、`job_max_attempts=3`、stall 自愈） | **禁止引入**。方案第 12 节要的「重启恢复、重试上限、超时」现有 scheduler 已具备，引入外部框架属倒退 |
| OpenTelemetry / structlog | **标准 `logging`**，无结构化日志、无 Token/成本采集 | 建议最小补齐：工具调用与任务状态变更写 JSON 结构化日志字段，不必引入 APM 全家桶 |

其余（FastAPI 0.161.1、Pydantic 2.14.1、pytest）与方案一致，无需替换。

---

## 3. 方案 20 节逐项对照矩阵

| 方案章节 | 核心要求 | 项目现状 | 判定 |
| --- | --- | --- | --- |
| §5 任务状态机 | TaskStatus 十态 + goal/success_criteria/iteration | `AgentSession.status` 仅 active/archived 两态；`Job.status` 服务于生产任务 | **缺失** |
| §6 意图识别 | IntentResult 结构化 + confidence 阈值 + 缺失信息澄清 | 无。`chat()` 直接拼固定 prompt 调 LLM | **缺失** |
| §7 上下文构建 | Token 预算分配 + 摘要 + 知识库 + 记忆 + 工具结果 | 仅有：最近 40 条消息（`_CHAT_HISTORY_WINDOW`）+ 固定 prompt + 两行项目状态 | **部分具备（约 20%）** |
| §8 任务规划 | PlanStep 含依赖、成功条件、requires_confirmation | 无任何计划结构 | **缺失** |
| §9 工具注册中心 | Pydantic input_model + risk_level + has_side_effect + timeout | `TOOL_REGISTRY` 为 dict，`params` 是自然语言字符串，校验靠捕获 `TypeError` | **部分具备（约 40%）** |
| §10 工具执行器 | call_id/duration_ms/retryable + 权限 + 幂等 + 审计 | `execute_tool()` 直接调用，**结果只拼进 prompt 字符串，用完即弃，零落库** | **严重缺失（审计为零）** |
| §11 结果验证 | 确定性 verifier + 五类决策 | 无。LLM 返回非空即落库为 assistant 消息 | **缺失** |
| §12 核心循环 | while 循环 + 迭代/Token/时间/调用次数上限 | 单次同步请求-响应，无循环。已有单点上限：`llm_max_retries=2`、`job_max_attempts=3`、`llm_stream_idle_timeout_seconds=60` | **缺失（但有边界控制基础）** |
| §13 记忆实现 | agent_memories 表 + 写入条件 + 检索排序 | 无记忆表、无提取器、无检索 | **完全缺失** |
| §14 API 设计 | 任务式 API + 确认接口 + 取消接口 | 仅有会话/消息 CRUD + 同步 `/chat` | **部分具备（约 30%）** |
| §15 安全控制 | 身份隔离、权限、注入检测、脱敏、确认 | **全站无鉴权**（见审计 SEC-001/SEC-002）；有工具名白名单、SQLAlchemy 参数化、无 shell/eval 执行面 | **部分具备（安全基线不足）** |
| §16 可观测性 | Token/成本/Trace/十项指标 | 实测无 `prompt_tokens`/`cost_usd`/`opentelemetry`/`structlog`。Job 表有 attempts/error_code/耗时 | **严重缺失** |
| §17 测试方案 | 单元 + 集成 + Agent 评估集 | 118 passed，含 agent 会话与工具用例；**无评估集目录** | **部分具备** |

---

## 4. 五个关键差距（按投入产出排序）

### 差距 1：工具调用零审计 —— 最高优先，改动最小
- 现状：`agent.py` 的 `chat()` 中 `execute_tool(db, 'project_overview', ...)` 的结果仅拼接进 `system_prompt` 字符串，**调用参数、耗时、结果、成败全部不落库**。
- 后果：违反方案 §20 第 9 条「所有工具调用和状态变化必须可追踪、可审计」；出问题时无法回答『Agent 当时查到了什么』。
- 低成本补法（**无需新表**）：`AgentMessage.role` 已预留 `tool_event` 枚举值，`payload` 为 JSON 字段。每次工具调用落一条 `role=tool_event` 消息，payload 存 `tool_name / args / ok / error_code / duration_ms` 即可。
- 预计改动：`agent_tools.py` 增加计时与返回结构，`agent.py` 增加一次 insert。不涉及迁移。

### 差距 2：工具注册表缺 Schema 与风险元数据
- 现状：`params` 是给人看的字符串；非法参数靠 `except TypeError` 兜底成 `tool_invalid_args`。
- 方案要求：`input_model: type[BaseModel]` + `risk_level` + `has_side_effect` + `timeout_seconds` + `max_retries`。
- 意义：一旦未来注册写操作工具（生成触发、批量重做），**没有 `has_side_effect` 与 `requires_confirmation` 就没有审批门禁的挂载点**。
- 预计改动：`TOOL_REGISTRY` 的 3 个只读工具各补一个 Pydantic 入参模型与元数据；`execute_tool` 增加 `model_validate` 校验。无迁移。

### 差距 3：无意图识别与计划 —— 线性管道与闭环的分水岭
- 现状：模型对「调哪个工具」**没有选择权**，两个工具是硬编码在 `chat()` 里的。
- 后果：用户问「我的项目卡在哪」和「帮我分析这段剧本」，走完全相同的代码路径。
- 注意：这不是简单加个 prompt 能解决的，需要 `IntentResult` 结构化输出 + 计划持久化 + 循环执行体，属**架构级改动**。

### 差距 4：无结果验证器
- 现状：`chat()` 只在 `LLMError` 时失败；LLM 返回任意字符串都当作成功落库。
- 方案要求：优先确定性规则验证（如产物存在、状态到位），复杂情况才交给模型。
- 价值：本项目**天然适合确定性验证**——项目/任务/资产状态都在库里，`derive_progress()` 已经是现成的确定性函数。

### 差距 5：无记忆层与 Token/成本可观测
- 现状：零记忆；零 Token 统计。
- 判定：**价值高但优先级最低**。在没有意图与计划的情况下，记忆层无处挂载；Token 统计可先做最小版（在 `LLMClient` 返回处累计 usage 落库）。

---

## 5. 三项已有资产（不要重复造轮子）

| 资产 | 位置 | 可复用点 |
| --- | --- | --- |
| 幂等键 | `Job.request_key`（`models/production.py`）+ `executor.py` 提交前持久化 | 方案 §10「不可逆操作必须使用幂等键」**已实现**，Agent 任务可直接复用同一模式 |
| SSE 事件通道 | `api/v1/events.py`：`/events` 流 + `/events/backfill` 补发，含 `event_seq` | 方案 §14 的审批推送、§16 的进度可观测，可直接复用，无需新建推送机制 |
| 任务自愈 | `scheduler.py` 的 `_recover_stalled_llm_jobs`（stall 阈值 + 重试上限 + 达上限标 failed） | 方案 §12「重启后任务恢复」「避免无限循环」**已有实现范本**，Agent 循环可直接套用同一套 stall 判定 |

---

## 6. 一处需要决策的架构取向冲突

- 方案 §9 隐含假设：**模型通过 function calling 自主选择工具**。
- 项目 `AGENTS.md` P0 纪律：**工具只能由确定性业务代码执行，模型只负责选择与解释**；当前实现是硬编码调用。
- 两者不必然冲突，但引入 Planner 时必须先定规则，否则会退化成「模型自述已调用某工具」的虚报风险：
  1. 计划中的 `tool_name` 必须强制校验落在 `TOOL_REGISTRY` 白名单内，非法即拒；
  2. `requires_confirmation` 由确定性规则（`has_side_effect` + 风险等级）判定，**不允许模型自述**；
  3. 工具执行结果只能来自真实函数返回，模型的自然语言描述不得作为成功依据。
- 建议：保持现有 P0 纪律不放松，把方案中的「模型选工具」降级为「模型提议、代码裁决」。

---

## 7. 建议路线（本轮未实施，供决策）

| 阶段 | 内容 | 风险 | 是否需要迁移 |
| --- | --- | --- | --- |
| A（可立即做） | 差距 1 工具调用审计落库 + 差距 2 注册表 Schema 化 | 低，可逐项回滚 | 否 |
| B（需设计） | 意图识别结构化 + 计划表（`agent_task` / `agent_plan_step` / `agent_confirmation`） | 中，新增表与状态机 | 是 |
| C（长期） | 记忆层、Token/成本可观测、评估集 | 中高，依赖 B 的落地 | 是 |

---

## 8. 本次分析未覆盖

- 未对方案中的 mermaid 架构图做逐节点映射（信息密度低，已由矩阵覆盖）。
- 未评估多 Agent 演进路径（方案明确首版不需要，一致）。
- 未量化 Token 成本（项目当前无 usage 采集，无可信数据，不编造金额）。
- 未做浏览器层交互验证（无自动化工具）。

---

## 9. A 档实施记录（2026-10-27 14:18 用户确认后执行）

### 9.1 改动内容

**改动一：`backend/app/services/agent_tools.py`（整体重写）**
- 新增 3 个 Pydantic 入参 Schema：`ProjectOverviewInput` / `ListJobsInput` / `AssetStatsInput`（含长度与范围约束，如 `limit: ge=1, le=50`）。
- `TOOL_REGISTRY` 每个工具补齐元数据：`input_model` / `risk_level="low"` / `has_side_effect=False` / `timeout_seconds=30` / `max_retries=0`（只读查询失败原因确定，重试无意义）。
- `execute_tool()` 改为 `model_validate` 统一校验；`ValidationError` 转为结构化 `tool_invalid_args`（含逐字段错误信息）。
- `ToolResult` 新增 `duration_ms` 字段，全路径（成功/ToolError/校验失败/内部错误）均记录耗时。

**改动二：`backend/app/api/v1/agent.py` `chat()`（+31 / −2）**
- 每次工具调用落一条 `role=tool_event` 消息，payload 记录 `tool_name / args / ok / error_code / duration_ms`。
- 审计消息在 LLM 调用**之前**提交——LLM 失败也不丢失审计记录。
- 助手消息 `seq` 改为动态计算（`tool_event_seq + 1`），适配 tool_event 数量可变的场景。
- `tool_event` 不进入对话上下文（历史窗口仍仅取 user/assistant，代码未变）。

### 9.2 验证证据
- 后端全量 `pytest tests/ -q` → **118 passed**，退出码 0（含 7 个既有 agent_tools 用例全部通过，Schema 化未破坏契约）。
- 端到端实测（重启后端，测试会话 `ags_48558bfb4232` 关联真实项目，验证后已归档）：
  | seq | role | 内容 |
  | --- | --- | --- |
  | 1 | user | 『用一句话说明当前项目进度』 |
  | 2 | tool_event | project_overview, ok=True, 28ms |
  | 3 | tool_event | asset_stats, ok=True, 1ms |
  | 4 | tool_event | list_jobs, ok=True, 1ms |
  | 5 | assistant | 真实引用工具查询结果（clips 阶段 0/18、ComfyUI 不可达失败） |
  - seq 连续无空洞；tool_event_count=3；助手回复内容与工具数据一致（证明上下文注入仍正常）。
- 健康检查：重启后 `/api/health` → 200。

### 9.3 回归判定
- 差距 1（工具调用零审计）→ **已消除**：每次调用可追溯（tool_name/args/成败/耗时）。
- 差距 2（注册表缺 Schema 与风险元数据）→ **已消除**：入参强校验 + 审批门禁挂载点就位（`has_side_effect` / `risk_level`）。
- 差距 3/4/5（意图识别、验证器、记忆层）→ 保持缺失，属 B/C 档范围，未实施。

### 9.4 回滚方式
- `agent_tools.py`：整体重写，回滚需恢复原文件（git 工作区或备份）。
- `agent.py`：`file-undo-7e1be8f7`。
- 两处需同时回滚（agent.py 依赖 ToolResult.duration_ms 字段）。

### 9.5 遗留说明
- 后端为加载改动重启，当前仍以 root 运行（与第 12 章记录一致，降权基线待统一处理）。
- `tool_event` 消息会随会话消息列表接口返回（`page_size` 默认 50），前端消息区按 role 渲染时需注意：当前前端仅渲染 user/assistant 气泡，tool_event 的 content 为空字符串不会产生可见气泡，但如后续前端做消息过滤，应显式排除 `tool_event`/`system`/`approval` 角色。

## 10. B 档实施记录（2026-10-27）

### 10.1 改动内容

**意图识别结构化（差距 3 消除）**

- `app/services/intent_router.py`（重写）：
  - IntentResult 结构化：intent/goal/entities/missing_information/risk_level/confidence/classified
  - LLM 输出经 Pydantic `_IntentPayload` Schema 校验（json_mode）
  - 规则 + LLM 组合：高风险关键词（删除/退款/付款/发送/发布/密钥/密码/凭据）规则兜底提升 risk_level
  - 置信度阈值 INTENT_CONFIDENCE_THRESHOLD=0.7：低于阈值且 intent 为 query/analysis 时改判 ambiguous
  - 新增 build_clarification_question()：确定性拼接澄清问题，不调用模型
  - 保留 classify_intent/route_action 兼容签名；瞬态错误指数退避重试最多 3 次；失败降级 query 不阻断对话
- `app/api/v1/agent.py` chat()（+39/−15）：
  - 新增 clarify 动作：ambiguous 且带 missing_information 时返回澄清问题，不进入主对话
  - assistant payload 新增 intent_goal/risk_level/confidence（reject/guide/clarify/chat 全路径记录）
  - 工具执行改为意图-工具映射过滤（get_allowed_tools），识别失败降级 query 时映射全部只读工具，行为与阶段 E 一致

**工具注册表 Schema 化（差距 2 补齐）**

- `app/services/agent_tools.py`（重写）：
  - Pydantic 入参 Schema：ProjectOverviewInput/ListJobsInput/AssetStatsInput
  - TOOL_REGISTRY 每工具补元数据：input_model/risk_level/timeout_seconds=30/max_retries=0
  - execute_tool() 改为 model_validate 校验；ValidationError → tool_invalid_args
  - 新增 INTENT_TOOL_MAPPING 意图-工具映射与 get_allowed_tools()
  - tool_specs() 输出 input_schema（model_json_schema）
  - 保留 duration_ms 计时与审批门禁（requires_approval）

**三张新表 + Alembic 迁移**

- `app/models/agent.py`：新增 AgentTask（intent/intent_goal/risk_level/confidence/success_criteria/status）、AgentPlanStep（step_type=tool_call/clarification/reply、tool_name/args/status/result）、AgentConfirmation（task_id/tool_name/args/status=pending/approved/rejected/comment）
- `app/models/ids.py`：新增 agent_task_id（agt_）/agent_plan_step_id（aps_）/agent_confirmation_id（agc_）
- `alembic/versions/c3d4e5f6a7b8_agent_task_plan_confirmation.py`：三张表迁移，接续 b2c3d4e5f6a7

### 10.2 验证证据

- 全量 pytest：118 passed（改造前后各跑一次，均通过）
- Alembic：upgrade head 成功（b2c3d4e5f6a7 → c3d4e5f6a7b8），head 唯一无分叉；主库 data/director_lab.db 中 agent_task/agent_plan_step/agent_confirmation 三表已创建，alembic_version=c3d4e5f6a7b8
- 后端重启：/api/health → 200
- 端到端实测（会话 ags_a98222af6185，已归档清理）：
  - assistant payload 完整记录 intent=query/intent_goal/risk_level=low/confidence=0.92/route_action=chat
  - seq 1-5 连续：user → 3×tool_event（project_overview 27ms/asset_stats 1ms/list_jobs 1ms，均 ok）→ assistant
  - 助手回复真实引用工具数据（《小红帽》clips 阶段 0/18、资产 73 项等）

### 10.3 回滚方式

- agent.py：undo 记录 file-undo-d0f541fe-4bf3-43ef-984e-0368416c01b4
- intent_router.py / agent_tools.py：整体重写，回滚需恢复原文件
- models/agent.py：undo 记录 file-undo-165de1da-7d4a-4634-9367-2a5b2dbecdeb
- 数据库：alembic downgrade b2c3d4e5f6a7（删除三张新表）

### 10.4 差距状态更新

- 差距 1（工具调用零审计）：已消除（A 档）
- 差距 2（注册表缺 Schema）：已消除（B 档补齐 input_model/risk_level/timeout/retries + input_schema 输出）
- 差距 3（意图识别）：已消除（结构化 IntentResult + 置信度阈值 + 澄清流程 + 意图-工具映射）
- 差距 4（结果验证器）：部分缓解（success_criteria 字段已落库，验证器逻辑未实施）
- 差距 5（记忆层）：保持缺失（C 档范围）


## 11. B 档后续修复记录（2026-10-27 18:00-18:25 用户确认后执行）

本轮按用户反馈修复已知问题，并顺带修掉端到端复测时暴露的一处真实缺陷。

### 11.1 `list_assets` 排序与分页（提交 fc09c3c）
- 原实现按 `Asset` 排序，但 `Asset` 模型**没有时间戳字段**（列为 id/project_id/asset_type/name/version/parent_id/file_path/thumb_path/source_job_id/ref_count/locked/validity/input_snapshot/quality_status/quality_issues），按 `created_at` 排序会直接抛 `AttributeError` 崩溃。
- 改为 `select(Asset, Job.created_at).outerjoin(Job, Asset.source_job_id == Job.id)`，排序 `Job.created_at.is_(None).asc(), Job.created_at.desc(), Asset.id.asc()`：有任务的资产按生成时间倒序，无关联任务的排在最后。
- 新增 `offset` 入参与返回 `generated_at` / `offset` 字段；`count` 语义为「本次返回条数」。
- 排查过一例疑似重复：6 条结果里 `cover_f19dc8` 出现两次，取 30 条时 `returned 30 / unique 30 / dups []`，确认是不同 asset_id 同名，不是 outerjoin 放大。

### 11.2 资产缩略图按关键词条件注入（提交 fc09c3c）
- 工具列表拆为 `base_tools`（project_overview/asset_stats/list_jobs），仅当用户消息命中关键词 `图/视频/片段/素材/缩略/画面/看看/长什么样` 时才追加 `list_assets(validity="current", limit=6)`。
- 目的：避免每条回复都附带缩略图（UI 噪音 + 无谓带宽），且只展示仍有效的资产。
- 复测结果：资产类消息 `asset_refs=6`（全部 `has_thumb=True`）；进展类消息 `asset_refs=0` 且 `list_assets` 只执行 1 次。

### 11.3 前端破图兜底 + 预览层（提交 fc09c3c、03ed39f）
- 破图兜底：新增 `failedAssetIds` 集合、`onThumbError(assetId)`、`visibleAssetRefs(m)`，`img @error` 时记录并隐藏该项，容器 `v-if` 改为按可见列表判断——历史消息中资产被清理后不再出现破图。
- 坑 5 修复（原问题：点缩略图新开标签页直接加载数 MB PNG）：图片类资产由 `<a target="_blank">` 改为 naive-ui `NImage`，气泡内仍只请求 `?thumb=true`，`preview-src` 指向原图，点击进入预览层（缩放/旋转/下载），样式加 `cursor: zoom-in`。
- 构建：`npm run build` 通过，`DirectorConsoleView-_qV9FSjX.js` 13.27 kB，新增懒加载分包 `Image-DUTQtE7_.js` 29.01 kB。

### 11.4 意图规则快路径误判（提交 96c05b3）
- 现象：端到端复测「资产统计，顺便看看最近生成的角色图长什么样」被判为 `generate_request` → 返回生成引导文案、`asset_refs=0`。
- 原因：生成规则把「生成…的角色图」这类**描述性表述**当成了生成请求。
- 修复：
  - 生成规则加负向断言，排除「最近生成的角色图」这类描述性表述；
  - 新增查看类 query 规则（看看/看一下/给我看看/长什么样 + 图/视频/片段/素材/角色/场景/道具/关键帧/分镜/资产）；
  - 补「批量出图/重新渲染」等无宾语的生成规则，避免退化到 LLM。
- 效果：15 条规则用例从 3 个 FAIL 收敛到 0 FAIL；复测同一句话 → `intent=query`、`route=chat`、`asset_refs=6`。

### 11.5 新增回归测试（净增 40 个用例）
| 文件 | 覆盖内容 | 结果 |
| --- | --- | --- |
| `tests/test_agent_tools.py` | 注册表四工具只读断言；list_assets 排序与字段、不泄露 file_path/thumb_path、分页、过滤、limit/offset 边界 | 12 passed |
| `tests/test_agent_sessions.py` | asset_refs 关键词门控：资产类消息注入 list_assets 且审计落库；进展类消息 refs 为空、list_assets 只执行 1 次；tool_event 不进上下文 | 9 passed |
| `tests/test_intent_router.py`（新建） | 规则快路径 query/generate/unsafe、描述性查看非生成请求、模糊消息回落 LLM、高风险关键词提级、意图-工具映射只读白名单、澄清文案 | 22 passed |

全量 pytest：**160 passed, 4 warnings**（本轮起点 120 passed）。前端 `npm run build` 通过。

### 11.6 验证证据
- 后端重启加载新代码（pid 2739950），`/api/health` → 200；缩略图接口 `GET /api/assets/ast_f884db6705a2/file?thumb=true` → 200 / image/webp / 6934B。
- 端到端（会话 ags_ee08e6b35b15）：
  - 「资产统计，顺便看看最近生成的角色图长什么样」→ intent=query / route=chat / risk=low / asset_refs=6，回复真实引用资产统计（角色 5、场景等）；
  - 「项目进展如何」→ intent=query / asset_refs=0（不注入缩略图）。

### 11.7 仍未处理（截至 B 档后续修复）
- 规则快路径的 `risk_level` 提级与 `intent_goal` 仍只在 LLM 路径生效：规则命中 query 时 `risk_level` 恒为 low、`goal` 为空（「含删除的查询」仍可能被判 low）。
- 生成类能力（文生图/参考图生图/视频）仍未注册为 Agent 工具（需审批门禁 + 幂等键 + 成本闸口）。
- C 档（记忆层、Token/成本采集、评估集）未启动。


## 12. Agent 优化记录（2026-11-03，对照《通用 Agent 开发指南》）

本轮基于代码走查识别的优化方向，补齐工具覆盖、增加视频生成速度参数、落地 NextAction 枚举，并确认治理模块已就位。

### 12.1 新增 4 个 Agent 工具

| 工具 | 权限 | 风险 | 用途 |
| --- | --- | --- | --- |
| `get_shot_prompt` | read | low | 分镜图入参展示，返回提示词/确认状态/编译预览 |
| `script_normalize` | read | low | 剧本校验优化，复用 script_writing.normalize_script |
| `script_evaluate` | read | low | 剧本质量评估，复用 script_evaluation.evaluate_script |
| `batch_request_generate` | write | medium | 批量生成资产图，最多 20 个 entity_ids，全量校验后逐条建审批 |

- 修复 TOOL_REGISTRY 中 4 个工具的 fn 引用名（tool_ 前缀）
- 更新 test_intent_router.py 只读命名约定（纳入 script_normalize/script_evaluate）

### 12.2 视频生成速度参数（speed）

`_match_generate_workflow` 增加 `speed: str | None` 参数，仅对 clip 生效：
- speed 枚举：`fast` / `std` / `quality`
- 通过 `Workflow.capabilities.tier` 匹配档位工作流
- 未指定 speed 时保持原行为（默认 → 内置 → 任意）

联动改动（`agent_tool_impls.py`）：
- `RequestGenerateInput` / `BatchRequestGenerateInput` 增加 `speed` 字段
- `tool_request_generate` / `tool_batch_request_generate` 签名增加 `speed` 参数
- 两处 `_match_generate_workflow` 调用传入 `speed=speed`
- 两处 confirmation args 增加 `"speed": speed`

### 12.3 NextAction 枚举落地

- `agent_governance.py`：`NextAction` 枚举扩展 5 个导演台专用值（`CHAT`/`REJECT`/`GUIDE`/`PIPELINE`/`CLARIFY`）
- `intent_router.py`：`route_action` 返回类型从 `str` 改为 `NextAction`，返回枚举值而非裸字符串
- `agent.py`：导入 `NextAction`，所有 action 比较改用枚举值
- `test_intent_router.py`：断言同步改用 `NextAction` 枚举值

### 12.4 治理模块确认（agent_governance.py，已存在无需新增）

| 组件 | 状态 | 说明 |
| --- | --- | --- |
| PolicyEngine | ✅ 已就位 | `evaluate_tool_policy` 确定性判定，不依赖模型置信度 |
| ToolBudget | ✅ 已就位 | max_calls=12 / max_total_duration_ms=30000 / max_context_chars=24000 |
| TerminalStatus | ✅ 已就位 | COMPLETED/WAITING_FOR_USER/WAITING_FOR_HUMAN/DENIED_BY_POLICY/FAILED_SAFE |
| PolicyDecision | ✅ 已就位 | ALLOW/DENY/REQUIRE_CONFIRMATION |

### 12.5 agent_tools.py 拆分确认（已完成）

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `agent_tools.py` | 592 | TOOL_REGISTRY + execute_tool + 路由函数 |
| `agent_tool_impls.py` | 1617 | 所有 tool_* 函数实现 + Input Schema |
| `agent_governance.py` | ~130 | NextAction / TerminalStatus / PolicyDecision / ToolBudget / evaluate_tool_policy |
| `agent_context_tools.py` | 300 | build_read_tool_calls / format_tool_context |

### 12.6 验证证据

- 回归测试：**137 passed**（10 个测试文件）
- 备份目录：`backend/.bak_agent_opt_20261103/`

### 12.7 差距状态更新（截至 2026-11-03）

- 差距 1（工具调用零审计）：✅ 已消除（A 档）
- 差距 2（注册表缺 Schema）：✅ 已消除（B 档 + 本轮新增 4 个工具）
- 差距 3（意图识别）：✅ 已消除（B 档 + 本轮 NextAction 枚举落地）
- 差距 4（结果验证器）：部分缓解（success_criteria 字段已落库，验证器逻辑未实施）
- 差距 5（记忆层）：保持缺失（C 档范围）
- 新增：agent_tools.py 拆分 ✅、独立 PolicyEngine ✅、NextAction 枚举 ✅、预算控制模块 ✅

### 12.8 仍未处理
- 规则快路径 risk_level 提级与 intent_goal 仅 LLM 路径生效
- 结果验证器运行时逻辑未实施
- 工具调用重试（max_retries 字段已声明，运行时未执行）
- 流式输出（FLOW-001 既有遗留）
- C 档（记忆层、Token/成本采集、评估集）未启动
