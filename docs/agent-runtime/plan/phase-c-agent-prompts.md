# Director-Lab · Phase C「状态驱动 Planner 与工具层收敛」编码 Agent 任务提示词

> 前置条件：**Phase B 全部合并**，`tests/test_agent_phase_b_e2e.py` 全绿。行号只作参考，请按函数名定位。
> 用法：每次会话先粘贴 **〇、通用上下文**，再粘贴**一个**任务。一个任务对应一个 PR。
> 执行顺序：**C0 → C1 → C2 → C3 → C4 → C5 → C6 → C7 → C8 → C9 → C10**
> 对应最终 Plan 的 Phase C。已确认决策：**计划结构完全由代码生成，LLM 只负责把用户的话解析成目标**。

---

## 〇、通用上下文（每个任务前都要粘贴）

```text
你在 Director-Lab 仓库（FastAPI + SQLAlchemy 2 + SQLite WAL + Alembic，后端在 backend/）中工作。
当前任务属于「Agent Runtime Phase C：状态驱动 Planner 与工具层收敛」。
Phase A 修好了 Runtime 的正确性；Phase B 打通了 /chat 接入（开关控制）、审批 → Job → 自动继续、重启恢复，
但 Planner 只有两个固定模板（批量资产、批量分镜图），且只支持 agent_tools.TOOL_REGISTRY 中的工具。

Phase C 目标：
1. 用户说出目标阶段（例如"做完这个项目的分镜视频"），Agent 根据项目**真实进度**算出剩余步骤，已完成的不重做；
2. 覆盖整条生产流程：剧本 → 解析 → 确认解析 → 资产 → 分镜图 → 分镜视频（含音频）→ 合成 → 导出；
3. 三套工具注册表（agent_tools / pipeline_tools / regen_tools）通过一个统一目录接入 Runtime；
4. 目标级预算（Job 数、LLM 调用、运行时长），超限停下来问用户。

现有可复用的"真实状态"来源（禁止自己重写这些判定）：
- app/services/progress.py::derive_progress(db, project) -> (shot_total, shot_done, stage)
  12 个阶段：estimate / select_range / parse / characters / scenes / keyframes / clips / audio / compose / export / done
- app/services/pipeline_state.py：PIPELINE_NODES（7 个节点）、ACTION_NODE_MAP、assert_action_allowed、
  SCRIPT_REVIEW_GATED_ACTIONS（剧本审核未通过时阻断后续动作）
- app/services/stage_readiness.py：check_stage_readiness(db, project_id, stage) -> StageReadiness
  （ready / total / done / failed / generating / missing[StageBlocker] / blockers）；
  阶段常量 STAGE_ASSETS / STAGE_STORYBOARD_IMAGES / STAGE_STORYBOARD_VIDEOS / STAGE_COMPOSE；
  _STAGE_MIN_DONE：分镜图只要求至少 1 张即可进入下一阶段
- app/services/assets_gate.py::evaluate_assets_gate(db, project_id) -> GateResult（blockers[GateItem]，含 entity_id / asset_type / reason）
- app/services/parse_gate.py::evaluate_parse_gate
- app/services/shot_chain.py：分镜视频按集内顺序**串行**生成，第 N 镜依赖第 N-1 镜的实际尾帧
- app/services/dependency_impact.py：版本变化后的下游失效分析

三套工具注册表（各有自己的执行网关，禁止绕过）：
- agent_tools.TOOL_REGISTRY + execute_tool（29 个：查询类、request_generate / batch_request_generate / run_generate、风格、记忆……）
- pipeline_tools.PIPELINE_TOOL_REGISTRY + execute_pipeline_tool（create_project / save_script / analyze_script /
  confirm_parse / query_parse_status / get_pipeline_status / compose_video(高风险,需审批) / export_video(高风险,需审批)）
- regen_tools.REGEN_TOOL_REGISTRY + execute_regen_tool（regenerate_asset / regenerate_shot（高风险,需审批）/
  replace_asset / adopt_candidate / list_candidates）

硬性约束：
1. 继续遵守 Phase A、B 的全部约束（尤其：工具只走各自网关；Job 创建 ≠ 成功；审批按步骤绑定；
   迁移只增不删；测试只用 tmp 库；/chat 接入保持在 agent_runtime_chat_enabled 开关后面）。
2. Planner 是**纯确定性代码**：同样的项目状态 + 同样的 GoalSpec，必须产生同样的计划（有测试保证）。
   LLM 只允许出现在 C2 的目标解析中，输出必须经 Pydantic 校验，校验失败就向用户澄清，不得猜测。
3. Planner 只读：不得在规划过程中写业务表；判定一律调用上面列出的现有函数。
4. 已有有效产物的单元（current 资产、已有分镜图或视频的镜头）**不得**被计划重新生成，
   除非 GoalSpec.allow_regenerate=True 且该单元被显式点名（用户说"重做第 3 镜"）。
5. 在途的 Job（queued / running / chained_wait / submission_unknown）不得重复提交，只能"跟踪"。
6. 人工审核门禁（资产审核 / 分镜图审核开关）是 Phase D 的范围。本阶段只预留接口：
   review_gate_enabled(db, project_id, stage) 恒返回 False，规划代码要调用它（D 阶段替换实现）。
   但**现有的业务门禁必须遵守**：剧本审核（SCRIPT_REVIEW_GATED_ACTIONS）、解析确认（confirm_parse）。
7. 不改前端（Phase E）。新接口和新字段写进 docs/API.md。

交付要求：
- 代码 + 测试一起提交；`cd backend && pytest -q tests/test_agent_runtime_*.py tests/test_agent_phase_b_*.py tests/test_agent_phase_c_*.py`
  全绿；全量 pytest 的失败数不多于改动前（在 PR 描述中附统计）。
- PR 描述包含：改了什么、为什么、如何验证、遗留问题。
- 完成后在 docs/agent-runtime/implementation-plan.md 中勾选对应条目。
```

---

## C0 · 统一工具目录（三套注册表的门面）

```text
任务：新增 app/services/agent_tool_catalog.py，作为 Runtime 唯一的工具入口。**不合并、不搬迁**三套注册表的实现。

1. 数据类 CatalogToolSpec（只读视图）：
   name, source("agent"|"pipeline"|"regen"), description, input_model, risk_level, requires_approval,
   permission, idempotent, timeout_seconds, max_retries, preconditions(名称列表),
   以及本任务新增的元数据：
   - execution_mode: "sync" | "job" | "approval_request"
     （approval_request：工具本身只创建审批记录，例如 request_generate / batch_request_generate）
   - produces: 产物类型列表，例如 ["asset:character"]、["keyframe"]、["clip"]、["composition"]、["export"]、["parse_version"]，只读工具为 []
   - verify: Verifier 规则名（C8 使用），例如 "job_succeeded"、"asset_current"、"parse_confirmed"
   - pipeline_action: 对应 pipeline_state.ACTION_NODE_MAP 中的动作键（例如 generate_character），只读工具为 None
   - output_schema: 可选的 Pydantic 模型或 None
   新增元数据放在一个独立的覆盖表 _CATALOG_OVERRIDES: dict[str, dict] 里，不修改三个原注册表的字典
   （原注册表已有的字段直接透传）。没有覆盖项的工具默认 execution_mode="sync"、produces=[]、verify="tool_ok"。
2. 名称冲突：三套注册表如有重名工具，启动时抛错（写测试覆盖）。
3. invoke(db, name, *, approved=False, **kwargs) -> ToolResult：按 source 分派到
   agent_tools.execute_tool / pipeline_tools.execute_pipeline_tool / regen_tools.execute_regen_tool。
   三个网关返回的 ToolResult 是否为同一类型？请先阅读确认；不一致的话在门面里做一次规范化
   （ok / error_code / error_message / data / duration_ms），不修改网关本身。
4. Runtime 改用目录：execution_manager.execute_step、planner.validate_plan（工具存在性检查）、
   Phase B 的服务端参数注入，全部改为通过 catalog 读取 spec 和执行。删除 Phase B 中
   "工具不在 TOOL_REGISTRY 中就拒绝"的限制，改为"工具不在目录中就拒绝"。
5. 补全覆盖表：至少覆盖 request_generate / batch_request_generate / run_generate / analyze_script / confirm_parse /
   compose_video / export_video / regenerate_asset / regenerate_shot，以及所有只读工具（execution_mode=sync, produces=[]）。
   每条覆盖都要在注释里写明依据（例如"run_generate 返回 job_id，见 agent_tool_impls.tool_run_generate"）。
6. 静态守卫：扩展 Phase A 的 ast 守卫测试，断言 execution_manager / agent_runtime / planner 中不直接 import
   execute_tool / execute_pipeline_tool / execute_regen_tool（只能经由 catalog）。

验收测试 tests/test_agent_phase_c_catalog.py：
- test_catalog_contains_all_three_registries（数量 = 三者之和）
- test_name_collision_raises
- test_invoke_dispatches_by_source（monkeypatch 三个网关并计数）
- test_tool_result_normalized
- test_overrides_have_valid_fields（execution_mode 取值合法；produces 中的类型都在允许列表内）
- test_compose_requires_approval_via_catalog：invoke("compose_video") 未批准时 error_code == "approval_required"
```

---

## C1 · 生产 DAG：阶段定义与"是否已满足"判定

```text
任务：新增 app/services/production_dag.py，用代码描述整条生产流程，并提供每个阶段的真实状态判定。

1. 阶段定义（顺序即依赖，必要时可写成显式 depends_on）：
   script        剧本已保存                         判定：project.current_script_version_id 非空
   script_review 剧本审核通过                       判定：复用 pipeline_state._assert_script_review_passed 的同一口径
                                                    （请抽出一个返回布尔值和原因的只读函数，不要复制逻辑；原函数改为调用它）
   parse         解析已完成（候选版本已产出）         判定：parse_gate / derive_progress 的同一口径
   parse_confirm 解析结果已确认                     判定：project.current_parse_version_id 指向已确认版本（以 tool_confirm_parse 的写入为准）
   assets        公共资产齐全                       判定：evaluate_assets_gate(...).ready
   keyframes     分镜图                             判定：check_stage_readiness(STAGE_STORYBOARD_IMAGES)
   clips         分镜视频（含音频）                  判定：check_stage_readiness(STAGE_STORYBOARD_VIDEOS)
   compose       合成                               判定：check_stage_readiness(STAGE_COMPOSE) + 存在 current 合成记录
   export        导出                               判定：存在对应合成的导出记录（以 list_exports 工具使用的查询为准）
   注意：keyframes 的"足以进入下一阶段"（_STAGE_MIN_DONE=1）与"作为目标已完成"（全部镜头都有分镜图）是两回事，
   两个判定都要提供：is_satisfied_for_downstream / is_complete_as_target。

2. 每个阶段返回 StageState：
   stage, satisfied_for_downstream, complete_as_target,
   missing_units: list[Unit]   —— 还缺产物的最小单元（见下）
   inflight_units: list[Unit]  —— 已有在途 Job 的单元（带 job_id）
   failed_units: list[Unit]    —— 最近一次 Job 失败的单元（带 job_id、error_code）
   blocked_reason: str | None  —— 例如"剧本审核未通过"、"解析未确认"
   Unit = (stage, kind, target_id, episode, order_key, extra)，例如
   ("assets","character","chr_xxx",None,...)、("keyframes","shot","sht_xxx",1,(1,2,5))、("clips","episode",None,2,...)。
   单元数据一律来自 StageReadiness.missing / blockers、GateResult.blockers 等现有结构，不自己写 SQL 判断产物有效性。

3. 每个阶段登记"产出该阶段单元的工具"：
   assets → request_generate(target=character|scene|prop, entity_id)
   keyframes → request_generate(target=keyframe, shot_id)
   clips → 见下面第 4 条
   parse → analyze_script；parse_confirm → confirm_parse；compose → compose_video；export → export_video
   script / script_review 没有可由 Agent 自动完成的工具（剧本由用户提供，审核由用户裁决），
   规划到这两个阶段时只能产生 waiting_user 步骤。

4. 分镜视频必须按集串行：
   先阅读分镜看板"一键批量出视频"的现有实现（grep chained_wait、shot_chain、storyboard 相关 API），找到它创建串行 Job 链的入口。
   - 如果存在可复用的"按集批量提交 clip"的业务函数：把它包装成目录中的一个新工具（放进 agent_tools，遵守现有注册格式，
     requires_approval=True，execution_mode="job"，返回该集全部 job_id），clips 阶段的单元粒度为"集"。
   - 如果不存在：clips 单元粒度为"镜头"，同一集内的镜头步骤按 order_key 串行依赖（depends_on 前一镜），不同集之间可以并行。
   在 PR 中写明选择了哪种方式，以及依据的代码位置。禁止自己实现尾帧提取或串行链逻辑。

5. 音频：先确认 clips 阶段的 Job 是否已包含配音（README 写的是"含音频"，pipeline_state 的 generate_clips 节点包含 clips 和 audio 两个阶段）。
   如果 TTS / BGM / SFX 需要单独生成，增加 audio 子阶段，单元用 request_generate(target=audio|bgm|sfx, shot_id)；否则在注释中说明原因。

6. review_gate_enabled(db, project_id, stage) -> bool：本阶段恒返回 False（Phase D 替换）。

验收测试 tests/test_agent_phase_c_dag.py（用 conftest 中的 helper 构造各种项目状态，资产文件用 seed_scene_baseline_asset 的方式造真实文件）：
- test_empty_project_first_unsatisfied_is_script
- test_script_review_rejected_blocks（blocked_reason 非空）
- test_parsed_not_confirmed
- test_assets_partial_missing_units（5 个实体中 2 个有资产 → missing_units 为 3 个，entity_id 正确）
- test_inflight_job_not_in_missing（某实体有 queued Job → 出现在 inflight_units，不在 missing_units）
- test_keyframes_downstream_vs_target（1/10 张分镜图 → satisfied_for_downstream=True，complete_as_target=False）
- test_clip_units_ordered_by_episode_and_seq
- test_dag_functions_are_read_only：在 session 上监听 flush 事件，判定函数调用后没有任何新增、修改、删除的对象
```

---

## C2 · GoalSpec：把用户的话解析成结构化目标

```text
任务：新增 app/services/goal_spec.py。这是 Phase C 中唯一允许使用 LLM 的地方。

1. Pydantic 模型 GoalSpec：
   target_stage: Literal["parse","parse_confirm","assets","keyframes","clips","compose","export"]
   scope: GoalScope = { episodes: list[int] | None, entity_types: list[Literal["character","scene","prop"]] | None,
                        entity_ids: list[str] | None, shot_ids: list[str] | None }
   allow_regenerate: bool = False      —— 只有用户明确说"重做 / 重新生成"时为 True
   stop_before: list[stage] = []       —— 例如"先做到分镜图，视频先别生成"
   budget: GoalBudgetSpec | None       —— 见 C7
   source: Literal["rules","llm"]
2. parse_goal_spec(db, *, session, content, intent: IntentResult, resolved) -> GoalSpec | Clarification：
   a. 规则优先：基于 intent.intent / subcategory / entities（generate_request 的 asset.generate / shot.generate / video.generate / audio.generate，
      pipeline_action 的 script.parse / video.compose / video.export 等）以及中文关键词，构造 GoalSpec。
      Phase B 的 should_use_runtime 判定迁移到这里，统一维护。
   b. 规则无法确定 target_stage 时，调用 LLM（复用 app/integrations/llm 现有客户端的 chat_json），
      输入：用户原话 + 项目当前阶段（derive_progress 的结果）+ 可选阶段枚举的中文说明；输出 JSON 并用 GoalSpec 校验。
      Prompt 注册到现有的提示词注册机制中（参考 intent_router 的 _register_default），不要写死在代码里。
   c. 校验失败、LLM 超时、置信度不足、scope 中的 id 不属于当前项目 → 返回 Clarification(question)，由 /chat 直接回复澄清问题。
   d. scope 中的 entity_ids / shot_ids 必须在服务端校验归属当前项目（复用 intent_resolver 已有的实体校验）。
3. 目标指纹：compute_fingerprint 改为基于 GoalSpec（project_id + target_stage + 规范化后的 scope + allow_regenerate + stop_before），
   替换 Phase A 中 classify_description 的临时实现（删除 classify_description 及其调用）。
4. GoalSpec 持久化：写入 agent_goal.constraints["goal_spec"]（JSON）；success_criteria 由 GoalSpec 推导
   （例如 target_stage=clips → ["clips.complete_as_target"]），不再由描述文本推导。
5. 测试环境：conftest 中已禁止真实 LLM 调用的模式（参考 _block_real_scene_plate_llm），为 goal_spec 增加同样的自动夹具，
   需要 LLM 的测试用 monkeypatch 返回固定 JSON。

验收测试 tests/test_agent_phase_c_goal_spec.py：
- 参数化规则用例（至少 15 条中文表述），例如：
  "把这个项目的分镜视频都做完" → clips；"先生成角色和场景" → assets + entity_types=[character,scene]；
  "做到分镜图就行，视频先别动" → keyframes；"重做第二集的分镜图" → keyframes + episodes=[2] + allow_regenerate=True；
  "合成第一集" → compose + episodes=[1]
- test_llm_fallback_validated：LLM 返回合法 JSON → source="llm"
- test_llm_invalid_returns_clarification：LLM 返回非法 stage → Clarification
- test_foreign_entity_id_rejected
- test_fingerprint_stable_under_scope_order（scope 列表顺序不同，指纹相同）
```

---

## C3 · 状态驱动 Planner：滚动展开的计划

```text
背景：后续阶段的单元在前置阶段完成之前根本不存在（例如解析完成前没有镜头）。
所以计划不能一次性列出所有单元，而是"当前前沿阶段展开为具体单元，后续阶段用占位步骤表示，前置满足后再展开"。

任务：重写 planner.build_plan（保留函数名作为入口，删除 Phase A 的关键词匹配和 Phase B 的两个模板，模板测试改为调用新实现）。

1. plan_for_goal(db, goal, spec: GoalSpec) -> Plan：
   a. 计算阶段链：从 DAG 起点到 spec.target_stage（去掉 stop_before 之后的阶段）。
   b. 对每个阶段调用 C1 的判定，找到第一个"未满足下游要求"的阶段 = 前沿阶段。
   c. 前沿之前、已满足的阶段：不产生步骤。
   d. 前沿阶段：
      - blocked_reason 非空，或该阶段没有可自动执行的工具（script / script_review）→ 一个 waiting_user 步骤
        （step_type="clarification"，result 中写明原因和建议操作），计划到此为止；
      - 否则 missing_units 每个单元一个步骤（遵守 scope 过滤，以及 Phase B 的 agent_max_batch_steps 上限）；
        inflight_units 每个单元一个"跟踪"步骤（step_type="track_job"，job_id 直接写入，初始状态为 waiting_job，不调用任何工具）；
        failed_units：allow_regenerate=False 时也按 missing 处理（重新提交生成申请，需要审批），
        但在步骤 result 中标记 previous_failure，回复中告诉用户这是重试。
   e. 前沿之后的阶段：每个阶段一个占位步骤（step_type="stage_expand"，step_key=f"expand:{stage}"），
      depends_on 为前一阶段的全部步骤（或前一个占位步骤）。
   f. 目标阶段为 keyframes 时使用 complete_as_target 判定；中间阶段使用 satisfied_for_downstream 判定。
   g. 步骤 step_key 规则：f"{stage}:{kind}:{target_id or episode}"，保证同一单元在不同 revision 中 key 相同（C4 依赖它来沿用已完成的步骤）。
2. 占位步骤的展开：Runtime 在 tick 中遇到依赖已满足的 stage_expand 步骤时，**不调用工具**，而是：
   用当前状态重新调用 plan_for_goal → 生成新的 revision（carry_over 已完成的步骤）→ 旧计划 superseded → 继续 tick。
   这个"展开"不计入 Replan 次数（与 Phase A 的 MAX_REPLAN_PER_GOAL 分开统计：expansion_count，上限 20，超过则 failed，防止死循环）。
3. 依赖关系：
   - assets 单元之间相互独立（可并行）；keyframes 单元依赖 assets 阶段整体满足；
   - clips 按 C1 第 4 条处理（集内串行或按集批量）；
   - compose 每集一个步骤，依赖该集 clips 完成；export 依赖对应的 compose。
4. 计划必须通过 validate_plan；新增校验项：
   - 同一单元不得出现两次；
   - 有效产物的单元不得出现在生成步骤中（除非 allow_regenerate 且被点名）；
   - track_job 步骤的 job_id 必须存在且处于在途状态；
   - stage_expand 只能依赖已存在的步骤。
5. 确定性：对同一 db 状态和 GoalSpec 调用两次，plan_json 中除 plan_id / 时间戳外完全一致（测试覆盖）。

验收测试 tests/test_agent_phase_c_planner.py：
- test_acceptance_remaining_only（**Phase C 核心验收**）：已解析并确认、5 个实体中 2 个有资产、没有分镜图的项目，
  GoalSpec(target_stage=clips) → 计划 = 3 个资产步骤 + expand:keyframes + expand:clips；不包含 parse / confirm / 已有资产的实体。
- test_inflight_tracked_not_resubmitted
- test_stop_before_respected
- test_blocked_by_script_review_waits_user
- test_no_project_shots_before_parse：未解析项目、目标 keyframes → 计划 = analyze_script + expand:parse_confirm + ...，不包含任何镜头单元
- test_expand_creates_new_revision_and_carries_over
- test_expansion_limit
- test_deterministic_plan
- test_scope_episode_filter
```

---

## C4 · 基于状态的 Replan 与版本变化检测

```text
任务：
1. Replanner 改造：Planner 已经是状态驱动的，因此 REPLAN 动作统一为"用当前真实状态重新调用 plan_for_goal，生成新 revision，
   沿用 step_key 相同且仍然有效的已完成步骤"。删除 Phase A 中"计划完全相同就快速失败"的临时逻辑，改为：
   新计划中包含与失败步骤相同 step_key、相同工具、相同参数的步骤，且失败原因属于确定性错误
   （tool_invalid_args / precondition_failed / 业务门禁错误码）→ 不重试，该步骤转为 waiting_user，其余步骤继续。
2. 已完成步骤"仍然有效"的判定：不只看步骤状态，还要用 C1 的判定确认该单元当前确实有有效产物
   （例如资产生成后被用户在页面上删除或替换，单元重新变为 missing，就不能沿用）。
3. 上下文快照与版本检测：
   - ContextSnapshot 增加 versions 字段：script_version_id、current_parse_version_id，
     以及计划涉及的实体资产 id 和版本、镜头分镜图 asset_id。
     计划保存时把这份快照写入 plan_json["snapshot_versions"]。
   - 执行 produces 非空、或 risk_level=high 的步骤之前，重新采集相关版本并与快照比较：
     剧本或解析版本变化 → 整个计划失效，触发 Replan（reason="upstream_version_changed"）；
     该步骤依赖的资产版本变化 → 只对受影响的单元 Replan（用 dependency_impact.preview_dependency_impact 计算受影响范围，不要自己遍历依赖）。
   - 禁止在版本已变化的情况下继续执行旧计划中的生成步骤。
4. Replan 事件：沿用 Phase B 的 agent.plan.created，payload 增加 parent_plan_id、reason（failure / expansion / upstream_version_changed / user_goal_changed）。
5. 用户在进行中修改目标（指纹变化）：沿用 Phase A 的 supersede 逻辑；新 Goal 的计划会自然沿用状态（因为 Planner 读取真实状态），
   旧 Goal 中仍在途的 Job 由新计划的 track_job 步骤接管，不取消。写测试覆盖这一点。

验收测试 tests/test_agent_phase_c_replan.py：
- test_replan_skips_still_valid_steps
- test_completed_step_invalidated_if_asset_removed
- test_deterministic_failure_goes_waiting_user_not_retry
- test_script_version_change_invalidates_plan
- test_asset_version_change_replans_affected_only
- test_goal_change_adopts_inflight_jobs（旧 Goal 的在途 Job 被新计划 track，Job 表中没有新增重复 Job）
```

---

## C5 · 通用的"审批支撑步骤"

```text
背景：Phase B 的 B4 对 request_generate / run_generate 做了专门处理。Phase C 要让 compose_video、export_video、
regenerate_asset、regenerate_shot 等需要审批的工具也能进入 Runtime。

任务：
1. 按 catalog 的 execution_mode 和 requires_approval 统一处理（替换 B4 中按工具名判断的分支）：
   - execution_mode="approval_request"（request_generate 类）：执行一次得到 confirmation_id → waiting_approval；
     审批通过后执行"配对的执行工具"。配对关系写在 catalog 覆盖表中：request_generate → run_generate（参数 confirmation_id）。
   - requires_approval=True 的普通工具（compose_video / export_video / regenerate_*）：首次执行得到 approval_required →
     用 approvals.create_confirmation 建审批（tool_name 写真实工具名、args 写注入后的参数）→ waiting_approval；
     审批通过后用 catalog.invoke(name, approved=True, **args) 执行。
2. 与页面上的"执行"按钮保持一致：api/v1/agent.py::execute_approval 已按 PIPELINE_TOOL_REGISTRY / REGEN_TOOL_REGISTRY / 其他 分派执行。
   用户可能先在页面上点了执行，Runtime 再恢复。请确认 compose_video / export_video / regenerate_* 是否幂等
   （重复执行是否会创建第二个合成或生成 Job）：
   - 幂等的：Runtime 直接调用，得到同一个结果；
   - 不幂等的：Runtime 执行前先检查该审批是否已被执行过（复用 Phase A 中"审批已消费"的判定，必要时在 execute_approval 中补记录），
     已执行就从记录中取 job_id 或结果，不再调用工具。
   在 PR 中列出每个工具的幂等性结论和依据。
3. execute_approval 接口在执行成功后，如果审批关联了 Runtime 步骤，同样给步骤打恢复标记（与 Phase B 的 resolve 打标记一致）。
4. 删除 Phase B B4 第 4 条"流程工具和返工工具不接入 Runtime"的 Validator 限制。

验收测试 tests/test_agent_phase_c_approval_steps.py：
- test_compose_step_waits_approval_then_runs
- test_export_step_after_compose
- test_regenerate_shot_requires_explicit_scope（没有 allow_regenerate + 点名时，Planner 不会生成 regenerate 步骤）
- test_page_execute_then_resume_no_duplicate（对每个需要审批的工具参数化）
- test_execute_approval_marks_step
```

---

## C6 · 主对话的动态工具暴露

```text
背景：/chat 的 LLM 主对话目前按意图从 INTENT_TOOL_MAPPING 取工具（只读工具为主）。原 Plan 要求"不一次性暴露所有工具，按阶段筛选"。

任务：
1. agent_tools.get_allowed_tools(intent, *, project_stage: str | None = None, has_active_goal: bool = False) -> list[str]：
   - 保留原有的意图映射作为基础集合；
   - 按 project_stage 去掉与当前阶段无关的只读工具（例如未解析时去掉 list_project_shots、get_clip_generation_progress 等），
     映射表写成数据（STAGE_TOOL_FILTER），不要写成分支判断；
   - has_active_goal=True 时额外加入 Goal 查询能力（如果 Phase B 没有对应的只读工具，新增一个 get_agent_goal_status 只读工具，从 B8 的接口复用实现）；
   - 无论什么情况，requires_approval=True 或 risk_level=high 的工具都**不得**出现在 LLM 工具列表中（加断言测试）。
2. /chat 调用处传入 derive_progress 得到的阶段和 active Goal 状态。原签名保持兼容（新参数都有默认值），现有调用不传参时行为不变。
3. 在助手消息 payload 的审计字段中记录本轮暴露的工具名列表（如果已有类似字段，就复用）。

验收测试 tests/test_agent_phase_c_tool_exposure.py：
- test_default_signature_unchanged
- test_stage_filter_removes_irrelevant_tools（参数化几个阶段）
- test_high_risk_never_exposed（遍历所有意图 × 阶段组合）
- test_active_goal_adds_status_tool
```

---

## C7 · 目标级预算

```text
任务：
1. GoalBudgetSpec（放在 goal_spec.py）：max_jobs（默认 settings.agent_goal_max_jobs=200）、max_llm_calls（默认 50）、
   max_wall_seconds（默认 24 小时）、max_steps（默认 500）。用户在目标中说"最多生成 20 张"等时由 C2 解析覆盖。
2. 预算账本：在 agent_goal.constraints["budget_usage"] 中记录 jobs_created、llm_calls、steps_executed、started_at。
   - jobs_created：每次步骤进入 waiting_job 时按新的 job_id 数量累加（track_job 步骤不计入）；
   - llm_calls：C2 的目标解析每调用一次 LLM 就记一次。先查看 app/integrations/llm 客户端能否拿到 token 用量：能拿到就同时记录 tokens，拿不到就只计次数，在 PR 中说明；
   - 检查点：创建 Job 的步骤（execution_mode=job，或审批后会产生 Job 的步骤）执行之前，以及每次 tick 开始时。
3. 超限处理：不执行该步骤，Goal 的 lifecycle_state → waiting_user，error_code="budget_exceeded"，回复中说明哪项超限、当前用量和上限，
   并给出两个选择："提高上限后继续"或"到此为止"。
4. 接口：POST /api/agent/goals/{goal_id}/budget（调整上限，只允许提高或降低到不低于当前用量）；
   /chat 中"继续，上限提高到 N"之类的表述由 C2 识别为预算调整。
5. 删除 Phase A 中 execution_manager.execute_step 的单轮 ToolBudget 参数（它是对话单轮预算，不适用于跨天的 Goal），改由 Goal 预算统一处理；
   /chat 主对话中原有的 ToolBudget 保持不变。

验收测试 tests/test_agent_phase_c_budget.py：
- test_job_budget_blocks_before_creation（max_jobs=2，3 个资产步骤 → 第 3 个审批通过后不执行，waiting_user）
- test_track_job_not_counted
- test_raise_budget_resumes
- test_cannot_lower_below_usage
- test_budget_usage_persisted_across_restart
```

---

## C8 · 阶段级验证与目标完成判定

```text
任务：
1. Verifier 增加按 catalog.verify 规则名分派的验证器（registry 形式，不写长 if-else）：
   - job_succeeded：沿用 Phase A（Job 终态 succeeded 且归属正确）；
   - asset_current：单元对应的实体存在 validity=current 的资产，且文件存在（复用 assets_gate.media_file_exists）；
   - keyframe_current / clip_current：对应镜头存在当前有效的分镜图 / 视频（复用 stage_readiness 的同一口径）；
   - parse_confirmed：C1 的 parse_confirm 判定为真；
   - composition_current / export_exists：以 list_compositions / list_exports 工具的查询为准；
   - tool_ok：同步工具 ToolResult.ok。
   步骤只有在"工具 / Job 层面成功"且"该单元的产物验证通过"时才是 succeeded。
   Job 成功但产物验证失败（例如文件缺失）→ step failed，error_code="artifact_verification_failed"，进入 Replan 流程（按 C4 的确定性错误规则处理）。
2. 阶段级验证：一个阶段的所有步骤都终态之后，再调用一次 C1 的阶段判定确认该阶段真的满足；
   不满足（例如有单元在执行期间被别处失效）→ 触发一次展开（重新规划该阶段的缺失单元）。
3. 目标完成：只有 spec.target_stage 的 complete_as_target 为真，且没有失败或等待中的步骤时，Goal 才 completed。
   check_goal_completion 改为调用这个判定（不再只看计划步骤）。
4. 回复中的完成信息要列出实际产物数量（例如"角色 3/3、场景 5/5、分镜图 26/26"），数据来自阶段判定，而不是步骤计数。

验收测试 tests/test_agent_phase_c_verify.py：
- test_job_success_but_file_missing_fails_step
- test_stage_revalidation_triggers_expand
- test_goal_complete_only_when_target_satisfied
- test_reply_counts_from_stage_state
```

---

## C9 · 端到端场景测试（Phase C 验收门禁）

```text
任务：新增 tests/test_agent_phase_c_e2e.py。环境与 Phase B 的 B10 相同：tmp 库、render mock、LLM 用 monkeypatch，
"Worker"由测试代码直接调用单轮 Job 执行和 agent_resume.process_marked。审批通过由测试调用 resolve 接口完成。

场景（对应原始 Plan 第 21.3 节的场景编号）：
1. test_s2_no_script：已有项目、没有剧本，"帮我生成分镜视频" → waiting_user，回复提示上传或粘贴剧本，没有创建任何 Job 或审批。
2. test_s3_script_not_parsed：有剧本未解析，目标 keyframes → analyze_script（Job）→ 完成 → confirm_parse 步骤需用户确认 →
   确认后展开 assets → … 一路到 keyframes 完成；回复中能看到角色、场景、道具、镜头的实际数量。
3. test_acceptance_partial_project：Phase C 核心验收（见 C3），一路推进到 clips 完成，已有的 2 个资产没有产生新的 Job。
4. test_s9_job_timeout_or_lost：一个 Job 变为 lost 或长时间 running → 兜底扫描后按真实 Job 状态处理，没有重复提交。
5. test_s11_user_changes_goal：做到一半把目标从 keyframes 改为 assets（缩小范围）→ 新 Goal 立即 completed（资产已满足），
   旧 Goal superseded，在途的分镜图 Job 不被取消、被 track。
6. test_s12_switch_project：会话切换项目 → 新消息创建新项目的 Goal，旧项目 Goal 不受影响，两个项目的 Memory、计划互不引用对方的 id。
7. test_s14_duplicate_request：同一句话连续发送 3 次 → 1 个 Goal。
8. test_s17_dependency_version_changed：推进过程中用户在页面上重新生成了一个角色资产（模拟版本变化）→
   依赖该角色、尚未生成的分镜图步骤基于新版本重新规划；已生成的分镜图在回复中被标记为"可能需要复核"（具体失效处理属于 Phase D，这里只要求被识别出来）。
9. test_s19_budget：max_jobs=5 → 停在 waiting_user，提高上限后继续完成。
10. test_s20_partial_success：一个分镜图 Job 失败，其余成功 → lifecycle_state=waiting_user（或 partially_completed），
    回复中列出成功和失败的镜头，用户说"重试失败的"后只重新提交那一个。
11. test_compose_and_export：目标 export → 合成和导出各需一次审批，审批后完成，Goal completed。

完成标准：以上全部通过；Phase B 的 E2E 测试仍然全部通过（Phase B 的两个模板场景现在由新 Planner 实现）。
```

---

## C10 · 收尾：文档、清理、开关策略

```text
任务：
1. docs/agent-runtime/target-architecture.md：补充 production_dag、GoalSpec、滚动展开计划、统一工具目录的说明和示意图。
2. docs/agent-runtime/data-model-design.md：记录 plan_json 的新字段（snapshot_versions、goal_spec、expansion 信息）和步骤类型
   （tool_call / track_job / stage_expand / clarification）。
3. docs/API.md：预算接口、Goal 状态接口新增字段、助手消息 payload 的新字段。
4. 删除已被替代的代码：planner 中的关键词匹配、Phase B 的模板函数、classify_description、
   execution_manager 的单轮 ToolBudget 参数；确认没有残留调用（ruff + grep）。
5. 在 implementation-plan.md 中记录开关策略：Phase C 合并后，用示例项目《游乐场》从"只有剧本"跑到导出完成（使用真实 ComfyUI），
   通过后可以把 DLAB_AGENT_RUNTIME_CHAT_ENABLED 的默认值改为 true；同时在设置页文案中说明可以关闭。
6. 遗留问题汇总（作为 Phase D、E、F 的输入），至少包括：审核开关和审核绑定版本（D）、拒绝审核后按原因重新生成（D）、
   前端展示目标和计划（E）、Memory 接入 Context Resolver 和指标统计（F）、approvals.py 内部提交事务的问题。

完成标准：全量 pytest 失败数不多于 Phase B 结束时；ruff 无新增告警；文档中的代码路径都真实存在。
```

---

## 附：Phase C 完成后的状态（供 Phase D 衔接）

| 已具备 | 仍缺（后续阶段） |
|---|---|
| 用户说出目标阶段，Agent 按真实进度只做剩下的部分 | 资产 / 分镜图审核开关与审核绑定版本（D） |
| 整条流程：剧本检查 → 解析 → 确认 → 资产 → 分镜图 → 视频 → 合成 → 导出 | 审核拒绝后按意见重新生成（D） |
| 三套工具通过统一目录接入，所有需要审批的工具都能进入 Runtime | 已生成产物在上游变化后的失效处理与复核（D） |
| 版本变化检测、在途 Job 跟踪、不重复提交 | 前端目标卡片、计划步骤、等待入口（E） |
| 目标级预算、产物级验证、目标完成判定 | Memory 检索、指标统计、场景评测进 CI（F） |
