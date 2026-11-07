# 统一对话式任务执行改造 Phase 1 代码审计报告

- 日期：2026-11-04
- 范围：backend（app/services、app/worker、app/api/v1、app/models）+ frontend（DirectorConsoleView.vue 等）
- 目的：为 Phase 2 统一 Task Orchestrator 及后续阶段化改造提供事实基线

## 一、能力矩阵

| # | 能力域 | 现状 | 评级 | 关键证据 |
|---|--------|------|------|----------|
| 1 | 任务执行单元（Job） | Job 模型含 status（queued/running/succeeded/failed）、attempts、priority、request_key（幂等键）、input_snapshot、event_seq、error_code/error_msg、result | EXISTING | app/models/production.py:74-104 |
| 2 | 事件流（outbox + SSE） | job_event 表全局递增 seq + GET /api/events SSE 增量轮询（1s 间隔、单连接 300s 上限、断线以 last_seq 重连） | EXISTING | app/models/production.py:182-190、app/api/v1/events.py |
| 3 | Worker 调度与恢复 | run_worker 主循环 + 三类恢复：_recover_interrupted_jobs（启动恢复）、_reconcile_comfy_timeout_jobs（超时对账续跑）、_recover_stalled_llm_jobs（LLM 卡死恢复）+ 并发计算 | EXISTING | app/worker/scheduler.py:79-308 |
| 4 | 剧本解析链路 | tool_analyze_script → Job(parse) → parse_executor：LLM 调用事务外、normalize_parse_output 归一化、_upsert_records 集级 upsert；ParseVersion 口径锁定（total_episodes/script_hash）；EpisodeParseRecord 集×资产类型双粒度状态机 | EXISTING | pipeline_tools.py:178、parse_executor.py、production.py:106-121、narrative.py:190-205 |
| 5 | 解析阶段化进度事件 | 仅有 job.started/job.finished（payload 含 parse_version_id/shots），无分集/分资产类型进度事件 | PARTIAL | parse_executor.py |
| 6 | 图片/视频生成链路 | executor.py：ComfyUI 实例池（acquire_client_by_load 按负载选实例）、comfy_unreachable 故障转移重试、comfy_timeout 对账续跑、质量重试（job_max_attempts + quality_retry 事件 + 旧版本 supersede）、重生成门禁（_apply_regenerate_gates）、链式任务（_release_chained_next/_cancel_chained_after_failure）、末帧提取绑定 shot.last_frame_asset_id | EXISTING | app/worker/executor.py:378-1306 |
| 7 | 资产落库与锚定 | 产物按 {asset_id}_v{version}.{ext} 重命名落盘 + 缩略图；自动绑定 prop.anchor_asset_id / character.portrait_asset_id / scene.baseline_asset_id | EXISTING | executor.py:652-712、1104-1197 |
| 8 | 合成/导出链路 | compose_executor（_collect_shots/_latest_asset/_to_shot_input）+ export_executor，均走 Job + 事件机制 | EXISTING | app/worker/compose_executor.py、export_executor.py |
| 9 | 人工审批流 | request_generate（仅建审批记录，不耗算力）→ run_generate（requires_approval=true）→ execute_approval（agent.py:296）；工具注册表含 permission/risk_level/idempotent/requires_approval 元数据 | EXISTING | agent_tools.py 注册表、agent.py:276-296 |
| 10 | Agent 工具层 | pipeline_tools 8 个工具（create_project/save_script/analyze_script/review_assets/confirm_keyframes/compose_video/export_video/get_pipeline_status）+ agent_tools 注册表（含 run_generate/get_style_config/request_style_update 等） | EXISTING | pipeline_tools.py:114-376、agent_tools.py |
| 11 | 意图识别与会话上下文 | Rule Engine + LLM 分类 + intent_resolver（显式ID>焦点>澄清）+ context_manager（AgentSession.context_state）+ parent_agent（AgentTask 状态机 pending→running→completed/failed/cancelled） | EXISTING | 前序 Phase 2/3 交付 |
| 12 | chat 端点任务回传 | payload 已含 task_id/task_status/intent_resolution/route_action（web_search/video_workflow 等） | PARTIAL | agent.py:843-1126 |
| 13 | 统一 Task Orchestrator | AgentTask（会话级）与 Job（执行级）两套体系并行，无 task/task_stage/task_event/task_result 统一表，无统一事件流编排 | MISSING | parent_agent.py vs worker 体系 |
| 14 | 前端聊天内任务卡片 | DirectorConsoleView.vue 消息渲染仅按 role/content，无消息类型分支（m.type）、无 task_id/task_status 卡片渲染（grep 零命中）；搜索卡片为前序交付的唯一结构化卡片 | MISSING | DirectorConsoleView.vue:597-665 |
| 15 | 前端 SSE 消费 | JobsView.vue（EventSource）、ParseView.vue（llmEventSource）已有消费，但均在独立页面；导演台聊天框内无事件订阅 | PARTIAL | JobsView.vue、ParseView.vue |
| 16 | 测试基线 | 后端 453 passed / 7 failed，7 个失败均为 /tmp/p0.png root 所有权环境问题，与代码无关 | EXISTING（环境噪音） | 全量测试 2026-11-04 |

## 二、关键结论

1. **执行层成熟度高，编排层缺失**：Job/Worker/事件/恢复/审批/幂等（request_key）均已就绪且经过实战，Phase 2 不应重写执行层，而是新建统一 Task 层做编排映射。
2. **事件机制可复用**：job_event outbox + SSE 的 seq 递增与断线重连语义可直接扩展为 task 级事件流，避免另起通道。
3. **前端缺口集中在导演台**：消息渲染无类型分支是最大缺口，需新增任务卡片组件（动态更新、不刷屏）+ 聊天内 SSE 订阅。
4. **解析阶段化有数据基础**：EpisodeParseRecord 的集×资产类型双粒度状态机天然支持阶段化进度事件，只需在 parse_executor 补发分集进度事件。
5. **无 BROKEN 项**：未发现需要先修复才能开工的破损链路。

## 三、处理方案（Phase 2+ 输入）

| 缺口 | 方案 | 对应阶段 |
|------|------|----------|
| 统一 Task 层缺失 | 新增 task/task_stage/task_event/task_result 表；Task Orchestrator 将 AgentTask 映射到 Job，Job 保持底层执行单元不变 | Phase 2 |
| 解析无阶段化进度 | parse_executor 按 EpisodeParseRecord 粒度补发 task 级进度事件（复用 outbox） | Phase 3 |
| 生成无聊天闭环 | 生成类 Job 的 job.started/retry/finished 事件桥接为 task 事件，驱动聊天内任务卡片更新 | Phase 4 |
| 审批与依赖门禁 | 复用 request_generate/run_generate/execute_approval 既有审批流，Task Orchestrator 增加依赖门禁判断 | Phase 5 |
| 前端任务卡片缺失 | DirectorConsoleView 新增消息类型分支 + TaskCard 组件（订阅 SSE 按 task_id 增量更新，不追加新消息） | Phase 2-6 各阶段同步 |
| 恢复/重试/安全 | 复用 scheduler 三类恢复 + request_key 幂等 + comfy 对账续跑；LLM 提示词强制只引用真实事件状态 | Phase 7 |

## 四、约束确认

- 不推倒重写：所有 EXISTING 项原样复用，改造仅发生在编排层与前端展示层。
- 前端不得直调 ComfyUI：现状即如此（均经后端 Job），保持。
- 进度事件不刷屏：任务卡片采用『单卡片动态更新』模式，事件驱动 UI patch 而非追加消息。
