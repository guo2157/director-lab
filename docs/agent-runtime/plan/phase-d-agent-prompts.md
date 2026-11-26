# Director-Lab · Phase D「人工审核门禁与版本绑定」编码 Agent 任务提示词

> 前置条件：**Phase C 全部合并**，`tests/test_agent_phase_c_e2e.py` 全绿。行号只作参考，请按函数名定位。
> 用法：每次会话先粘贴 **〇、通用上下文**，再粘贴**一个**任务。一个任务对应一个 PR。
> 执行顺序：**D0 → D1 → D2 → D3 → D4 → D5 → D6 → D7 → D8 → D9 → D10**
> 已确认决策：**项目级审核开关**。新建项目默认开启，存量项目默认关闭；开关关闭时，系统行为与现在完全一致。

---

## 〇、通用上下文（每个任务前都要粘贴）

```text
你在 Director-Lab 仓库（FastAPI + SQLAlchemy 2 + SQLite WAL + Alembic；后端 backend/，前端 frontend/ 为 Vue3 + Vite）中工作。
当前任务属于「Agent Runtime Phase D：人工审核门禁与版本绑定」。

背景（务必先理解，不要重新发明）：
1. 2026-11 曾经**有意移除**资产和分镜图的人工审核闸口：
   - assets_gate.py 的 docstring 写明"人工审核闸口已取消：不再读 assets_review_status"；
   - Project.assets_review_status / assets_reviewed_at（项目级、不绑定版本）是遗留字段，目前没有任何判定读取它；
   - Shot.keyframe_confirmed（逐镜确认）也已取消，但仍有多处代码在重置它。
   本阶段把审核做成**可按项目开关的选项**，不是恢复旧设计。**不要复用这两个遗留字段**，也不要删除它们。
2. 资产版本模型：每个版本是 asset 表中的**一行**（同 name、version 递增、旧版本 validity 变为 superseded）。
   因此"审核绑定版本"天然可以落在资产行上——一行就是一个版本。
   注意：资产级重新生成（regen_tools.tool_regenerate_asset → api/v1/assets.py::regenerate_asset）的结果是**候选版本**，
   需要 adopt_candidate 才会成为 current。
3. 公共资产的生成 Job 类型同样是 generate_keyframe，区别在于 shot_id 为空（见 agent_tool_impls.tool_batch_request_generate 中的查询）；
   镜头分镜图 Job 是 generate_keyframe 且 shot_id 非空；分镜视频 Job 是 generate_clip。
4. 现有的就绪判定入口：
   - 资产：assets_gate.evaluate_assets_gate / assert_assets_gate（被 api/v1/generate.py::_check_assets_ready、
     stage_readiness.check_assets_readiness、agent_tool_impls.tool_run_generate 等调用）
   - 分镜图（生成视频前）：api/v1/generate.py::_check_keyframe_ready（**目前只在 API 层**），被 generate.py 多处与 asset_plans.py 调用
   - stage_readiness.check_stage_readiness（Phase C 的 production_dag 以它为准）
5. Job 的创建点分散在约 20 处（api/v1/*.py、jobs.py 的重试、services/pipeline_tools.py、agent_tool_impls.py……），
   所以只在入口处检查是不够的，需要在 Worker 执行前再兜底检查一次（D5）。
6. 已有的审核组件：review_confirmation.py（kind=artifact_review，目前只用于剧本审核，带 24 小时待审过期机制），
   以及 parse_confirmation_response（把"可以 / 不行 / 改一下"这类中文答复确定性地映射为 approve / reject / revise / defer）。
7. Phase C 中预留了 production_dag.review_gate_enabled(db, project_id, stage)，目前恒返回 False，D6 负责替换。

硬性约束：
1. 继续遵守 Phase A–C 的全部约束（工具只走网关；迁移只增不删；测试只用 tmp 库；Planner 确定性；Job 创建 ≠ 成功……）。
2. 开关关闭时**零行为变化**：每个修改过的门禁、接口、Worker 路径都要有"开关关闭时与改动前一致"的回归测试。
3. 审核结论只能由人做出：Agent、LLM、Planner 都不得把任何资产标记为 approved。
   通过对话审核时，只能把用户明确的答复（经确定性解析）应用到**当时展示给用户的那些具体版本**上。
4. 门禁在服务端执行：前端按钮置灰只是体验优化，不能作为门禁。
5. 不自动重新生成已审核通过的资产；上游变化导致下游失效时，只标记并提示，是否重做由用户决定
   （与 dependency_mark.py 中"仅写依赖失效标记，重做仍由用户显式触发"的既有策略保持一致）。
6. 审核相关事件写入 workspace_event（先登记白名单），不新建事件表。

交付要求：
- 代码 + 测试一起提交；`cd backend && pytest -q tests/test_agent_runtime_*.py tests/test_agent_phase_*.py` 全绿；
  全量 pytest 的失败数不多于改动前（在 PR 描述中附统计）。前端改动需通过 .github/workflows/frontend-checks.yml 中的检查。
- PR 描述包含：改了什么、为什么、如何验证、遗留问题。
- 完成后在 docs/agent-runtime/implementation-plan.md 中勾选对应条目。
```

---

## D0 · 迁移：资产行上的审核字段

```text
任务：新增 Alembic 迁移（down_revision = 当前 head），在 asset 表上增加审核字段，并同步 app/models/production.py::Asset。

新增列（全部可空，带 downgrade）：
- review_status    String(20) nullable, index   —— NULL=未审核 / approved / rejected / revision_required
- reviewed_at      DateTime nullable
- reviewer         String(64) nullable          —— 单用户系统先写 "user"；通过对话审核时写 "user:chat"
- review_comment   Text nullable
- review_round     Integer nullable             —— 同一实体（同 name 序列）第几轮审核，用于限制反复驳回的次数
- review_refs      JSON nullable                —— 仅分镜图使用：审核通过时该镜头绑定的参考资产 id 列表（AssetRef.asset_id），
                                                    D4 用它判断"审核之后参考资产有没有被替换"

设计说明（写进模型注释）：
- 审核字段挂在资产行上：一行就是一个版本，新版本行的 review_status 天然为 NULL（未审核），旧审核不会被继承。
- "未审核"就是 NULL，不需要在任何资产创建路径中写入 pending，因此不需要修改分散的资产创建代码。
- 只有角色 / 场景 / 道具 / 分镜图（asset_type in character, scene, prop, keyframe）参与审核；其他类型的这些字段始终为 NULL。

另外：在 workspace_event.EVENT_TYPES 白名单中新增：
- review.asset_updated（payload: project_id, asset_id, asset_type, entity_id?, shot_id?, version, review_status, reviewer）
- review.policy_updated（payload: project_id, asset_review, keyframe_review）
注意 event_type 列是 String(48)。

验收：tests/test_agent_phase_d_migration.py——upgrade 后新列存在、downgrade 后消失；
新建 Asset 行时 review_status 为 NULL；写入新事件类型成功。
```

---

## D1 · 审核开关（review_policy）

```text
任务：新增 app/services/review_policy.py，并接入项目创建路径与接口。

1. 存储：放在 project.settings["review_policy"] = {"asset_review": bool, "keyframe_review": bool, "updated_at": iso}。
   **不新增列**（project.settings 已用于 style_config、script_review、default_workflows 等）。
   - 读取函数 get_review_policy(project) -> ReviewPolicy：settings 中**没有这个键时视为两项都关闭**。
     这样所有存量项目自动保持现状，不需要数据迁移。
2. 新项目默认开启：settings 配置项 review_policy_new_project_default: bool = True（DLAB_REVIEW_POLICY_NEW_PROJECT_DEFAULT）。
   在以下创建路径中写入 {"asset_review": default, "keyframe_review": default}：
   - api/v1/projects.py::create_project
   - services/pipeline_tools.py::tool_create_project
   - api/v1/agent.py 中随手生成用的草稿项目（约 L709 的 Project(...)）→ **固定写两项都关闭**（草稿项目没有生产流程）
   先 grep 全仓库 `Project(` 确认没有其他创建路径；如有（例如系列、复制项目），在 PR 中列出并按"正式项目开、草稿关"处理。
3. 接口：
   - GET  /api/projects/{project_id}/review-policy
   - PUT  /api/projects/{project_id}/review-policy  body: {asset_review?: bool, keyframe_review?: bool, confirm: bool=false}
     · 关闭：随时允许。
     · 从关闭改为开启：先计算影响（当前 current 的必需资产 / 已有分镜图中有多少条 review_status 不是 approved），
       confirm=false 时只返回 {"requires_confirm": true, "impact": {...}}，不修改；confirm=true 时才写入。
       说明：开启后这些资产会立即成为"待审核"，阻断后续生成。
     · 写入时发 review.policy_updated 事件。
   - 项目详情接口（ProjectOut）增加 review_policy 字段（只读）。
4. 不修改 Project.assets_review_status 的任何读写逻辑。

验收测试 tests/test_agent_phase_d_policy.py：
- test_missing_key_means_off（存量项目）
- test_new_project_default_on（两条正式创建路径）
- test_draft_project_off
- test_enable_requires_confirm_and_reports_impact
- test_disable_anytime
- test_policy_event_written
```

---

## D2 · 资产审核服务与接口

```text
任务：新增 app/services/asset_review.py。所有审核写操作（页面、对话、批量）都必须经过这里。

1. review_asset(db, *, project_id, asset_id, action, comment=None, reviewer="user", expected_version=None) -> Asset
   - action ∈ {"approve", "reject", "revise"} → review_status = approved / rejected / revision_required。
   - 前置校验（不满足就抛业务错误，错误码要明确）：
     · 资产属于该项目，asset_type 属于参与审核的四种类型；
     · validity == "current"，或者是**候选版本**（先阅读 api/v1/assets.py 的 regenerate_asset 与 regen_tools.tool_adopt_candidate，
       确认候选版本的 validity 取值，并写进代码注释）；superseded / stale 的版本不能审核（review_target_not_current）；
     · 文件存在（复用 assets_gate.media_file_exists）；
     · expected_version 不为空时必须等于该行的 version（防止用户在页面上看的是旧版本、提交时已经变了）。
   - 审核候选版本时：approve = 先经 regen_tools 的 adopt_candidate 采用该候选（不要自己改 validity），再把**采用后成为 current 的那一行**标记为 approved；
     reject / revise = 候选保持不被采用，只记录审核结论。
   - 审核分镜图（asset_type=keyframe）通过时，把该镜头当前的 AssetRef.asset_id 列表写入 review_refs。
   - review_round：同一实体（同 project + name 序列）每次产生新的审核结论时 +1（取该序列已有的最大值）。
   - 幂等：同一行重复提交同一 action，不改写 reviewed_at，不重复发事件。
   - 写 review.asset_updated 事件；与业务写入同一事务（本服务不自行 commit，由接口层提交）。
   - 写完后调用 D6 中的 mark_review_steps(db, project_id)（D6 合并前先留一个空实现）。
2. 批量：review_assets(db, *, project_id, items=[{asset_id, action, comment, expected_version}]) —— 逐条校验，
   任意一条失败则整体回滚（与 tool_batch_request_generate 的"不产生部分结果"保持一致）。
3. 查询：list_review_queue(db, project_id, stage: "assets" | "keyframes") —— 返回需要审核的当前版本（review_status 不是 approved），
   带实体名 / 镜头名、版本号、缩略图路径、上一轮的审核意见。仅在对应开关开启时返回内容，否则返回空列表和 policy_off 标记。
4. 原地覆盖检查：grep 所有在**已有资产行**上修改 file_path 的代码（例如 replace_asset、各种 overwrite），
   对四种审核类型的资产，原地覆盖文件时必须同时把审核字段清空（review_status=NULL 等）。
   如果某个路径确实原地覆盖了已审核资产，写一个回归测试。不涉及审核类型的（例如 last_frame 尾帧）不要动。
5. 接口（新文件 app/api/v1/reviews.py，在 main.py 注册）：
   - POST /api/projects/{project_id}/assets/{asset_id}/review   body: {action, comment?, expected_version?}
   - POST /api/projects/{project_id}/reviews/batch              body: {items: [...]}
   - GET  /api/projects/{project_id}/reviews/queue?stage=assets|keyframes
   - 资产列表 / 详情接口的输出中增加 review_status、reviewed_at、review_comment、review_round（只读）。

验收测试 tests/test_agent_phase_d_review.py：
- test_approve_current_asset
- test_cannot_review_superseded_version
- test_expected_version_mismatch_rejected
- test_new_version_starts_unreviewed（重新生成并采用后，新行 review_status 为 NULL，旧行保留 approved 但已是 superseded）
- test_approve_candidate_adopts_then_approves
- test_batch_all_or_nothing
- test_idempotent_no_duplicate_event
- test_in_place_overwrite_clears_review（按第 4 条的排查结果编写）
- test_queue_empty_when_policy_off
```

---

## D3 · 资产门禁接入审核

```text
任务：在 assets_gate.evaluate_assets_gate 中加入审核判定。这是资产门禁的唯一权威入口，改这里即可覆盖所有调用方。

1. 开关 asset_review 开启时，对每个必需资产（沿用现有的必需性规则：角色定妆图、场景基准图、anchor_required 的道具锚定图），
   在现有检查（存在、有效、文件存在、不在生成中）都通过后，再检查 review_status：
   - approved → 通过
   - NULL → 阻断，reason="asset_pending_review"
   - rejected / revision_required → 阻断，reason="asset_review_rejected"，detail 带上审核意见
2. 新增状态常量 GATE_PENDING_REVIEW = "ASSETS_PENDING_REVIEW"、reason_code "assets_pending_review"，
   以及 _SUGGESTIONS 中对应的建议文案（例如"请在资产库审核该资产"、"该资产审核未通过，请重新生成或替换"）。
   状态优先级：生成中 > 不完整 > 待审核（资产没生成完时先提示生成，而不是提示审核）。
3. summary 中每类增加 reviewed 计数（角色 total / done / reviewed），供前端显示"角色 5/5，已审 3/5"。
4. 更新 assets_gate.py 的模块 docstring：把"人工审核闸口已取消"改为"人工审核按项目开关启用（review_policy.asset_review）；
   开关关闭时不读取任何审核字段"，并注明 Project.assets_review_status 仍为遗留字段、不参与判定。
5. 检查调用方的错误展示：api/v1/generate.py::_check_assets_ready 通过 assert_assets_gate 抛出结构化错误，
   确认新的 reason 能原样传到前端（blockers 中带 suggestion）。agent_tool_impls 中的调用如有自行拼接文案，同步补上新 reason。
6. derive_progress 不改：项目阶段仍可显示为 keyframes，但门禁会阻断分镜图生成——在 PR 中说明这一点，
   并检查 pipeline_state.get_pipeline_status 的 next_hint 是否需要在待审核时给出"请先审核资产"（如需要，只改提示文本）。

验收测试 tests/test_agent_phase_d_assets_gate.py：
- test_policy_off_identical：参数化若干项目状态，比较开关关闭时的 GateResult.to_dict() 与改动前（以保存的 fixture 为准）
- test_pending_review_blocks
- test_rejected_blocks_with_comment
- test_all_approved_ready
- test_generating_has_priority_over_pending_review
- test_optional_prop_not_required_for_review
- test_api_keyframe_generate_blocked_by_review（直接调用 POST /shots/{id}/generate，返回 409 及新 reason）
```

---

## D4 · 分镜图门禁：迁到服务层并接入审核

```text
任务：
1. 把 api/v1/generate.py::_check_keyframe_ready 的逻辑**原样**移到新文件 app/services/keyframe_gate.py：
   evaluate_keyframe_gate(db, project_id, shot_ids) -> KeyframeGateResult（ready + 每个镜头的阻断明细）
   和 assert_keyframe_gate(db, project_id, shot_ids)（抛出与原来相同的 AppError("keyframe_not_ready", ..., 409)）。
   generate.py 中保留 _check_keyframe_ready 作为一行转调的薄封装，asset_plans.py 的调用同样改为转调。
   **先只做搬迁并跑通全部现有测试，再做第 2 步**（可以在同一个 PR 中分成两个提交）。
2. 开关 keyframe_review 开启时，每个镜头在"存在有效分镜图"之外还要满足：
   - 该分镜图资产行 review_status == approved；
   - 审核没有过期：审核时记录的 review_refs 中，若有任何一个资产已不是 current，
     则视为"分镜图审核过期"（reason="keyframe_review_stale"，detail 列出哪个参考资产已被替换、替换后的版本）。
     说明：这表示分镜图是用旧版本的角色 / 场景 / 道具画的；处理方式由用户决定（重新审核通过，或重新生成）。
     用户在参考资产更新后重新审核通过，review_refs 会被刷新为新的参考列表，过期状态随之消除。
   reason 取值：keyframe_pending_review / keyframe_review_rejected / keyframe_review_stale。
3. 补齐缺失的门禁调用：
   - agent_tool_impls.tool_request_generate / tool_run_generate 在 target=clip 时调用 assert_keyframe_gate（先确认现有代码是否已有等价检查，没有则补上）；
   - regen_tools.tool_regenerate_shot 在 target=clip 时同样补上；
   - stage_readiness 对 STAGE_STORYBOARD_VIDEOS / STAGE_COMPOSE：开关开启时，把"分镜图未审核 / 审核过期"的镜头作为
     新的 blocker reason 加入（shot_keyframe_pending_review / shot_keyframe_review_stale），不计入 done。
     实现方式：在 check_stage_readiness 中调用 keyframe_gate 的逐镜判定，不要复制审核判断逻辑。
4. 分镜视频按集串行（shot_chain）：某一镜分镜图未审核时，按现有"逐镜闸口"语义处理——批量提交时跳过并提示，
   不要改变 shot_chain 的尾帧逻辑。请阅读批量出视频的现有实现，确认跳过行为在开关开启时依然正确，并写测试。

验收测试 tests/test_agent_phase_d_keyframe_gate.py：
- test_moved_gate_identical_when_policy_off（覆盖原 _check_keyframe_ready 的所有分支）
- test_clip_blocked_until_keyframe_approved（直接调用 POST /shots/{id}/regenerate-clip 或对应的生成接口）
- test_stale_when_reference_asset_replaced
- test_reapprove_clears_stale（参考资产更新后重新审核通过 → review_refs 刷新 → 不再过期）
- test_request_generate_clip_checks_gate
- test_stage_readiness_videos_counts_review
- test_batch_clip_skips_unreviewed_shots
```

---

## D5 · Worker 兜底检查

```text
背景：Job 的创建点分散在约 20 处，另有 jobs.py 的"重试"会复制旧 Job 重新入队，入口检查无法保证全覆盖。

任务：
1. 新增 app/services/review_gate.py::check_job_against_review_gate(db, job) -> str | None（返回阻断原因码，或 None）：
   - 先读项目的 review_policy；两项都关闭 → 直接返回 None（不做任何查询）。
   - generate_keyframe 且 shot_id 非空（镜头分镜图）且 asset_review 开启 → 资产门禁中的**审核部分**未通过则阻断
     （只检查审核部分：资产缺失、生成中等其他情况，保持现有入口的行为，不在 Worker 中新增拦截）。
   - generate_clip 且 keyframe_review 开启 → 该镜头的分镜图审核未通过或已过期则阻断。
   - generate_keyframe 且 shot_id 为空（公共资产本身的生成）→ 不检查。
   - 其他 Job 类型 → 不检查（合成、导出依赖视频齐全，视频已受分镜图门禁约束；在注释中说明）。
2. 接入点：worker/executor.py::execute_generate_job 中，在 Job 认领为 running 之后、提交 ComfyUI 之前调用。
   阻断时：job.status=failed，error_code="review_gate_blocked"，error_msg 写明原因；**不提交 ComfyUI**；
   按现有 Job 失败的标准路径写 job_event 并调用 bridge_job_event，确保 Phase B 的恢复标记和任务中心都能正常更新。
   不要加入自动重试（这个失败不是瞬态错误）。请确认现有的失败重试或熔断逻辑不会把 review_gate_blocked 当作可重试错误，必要时加入不可重试列表。
3. Runtime：在 Replanner 的错误码映射表中加入 job_failed:review_gate_blocked → WAIT_USER。

验收测试 tests/test_agent_phase_d_worker_gate.py：
- test_policy_off_no_query（用 SQLAlchemy 事件计数确认开关关闭时没有额外查询）
- test_clip_job_blocked_without_approved_keyframe（手工插入一条 queued 的 generate_clip Job，调用执行器）→
  failed / review_gate_blocked，ComfyUI 客户端 mock 没有被调用
- test_retried_job_also_blocked（通过 jobs.py 的重试接口重新入队）
- test_entity_asset_job_not_checked
- test_blocked_job_bridges_and_marks_agent_step
```

---

## D6 · Runtime 接入审核步骤

```text
任务：
1. 替换 production_dag.review_gate_enabled(db, project_id, stage)：assets 阶段读 asset_review，keyframes 阶段读 keyframe_review。
2. 阶段判定：production_dag 中 assets / keyframes 的 satisfied_for_downstream 与 complete_as_target 已经以 assets_gate / stage_readiness 为准
   （D3、D4 后自动包含审核）。新增 review_pending_units / review_rejected_units / review_stale_units 三个列表，
   让 Planner 区分"还没生成"和"已生成、等待审核"。
3. Planner：开关开启时，在 assets（以及 keyframes）阶段的生成步骤之后插入一个审核步骤：
   step_type="review_gate"，step_key=f"review:{stage}:{scope_key}"，不调用任何工具；
   depends_on = 该阶段所有生成步骤；步骤 result 中记录 review_targets = [{asset_id, version, entity_id / shot_id, name}]。
   - 执行到该步骤时：targets 全部 approved → succeeded；有未审核的 → waiting_approval（Phase B 的等待态），
     回复中列出待审核项和审核入口；
   - 审核步骤的 targets 在进入等待时**固定下来**（快照）；之后如果某个实体产生了新版本，原快照中的那一项视为过期，
     由 Replan 生成新的审核步骤，而不是悄悄替换快照中的 asset_id。
4. 恢复：实现 D2 中预留的 asset_review.mark_review_steps(db, project_id)：
   给该项目下 status=waiting_approval 且 step_type=review_gate 的步骤设置 resume_requested_at（resume_reason="review_changed"）。
   tick 中处理这类步骤时，重新读取每个 target 的审核状态。
5. 驳回后的处理（rejected / revision_required）：
   - 对每个被驳回的实体，生成一个重新生成步骤：公共资产用 regenerate_asset，分镜图用 regenerate_shot(target=keyframe)
     （两者都需要审批，走 Phase C 的通用审批步骤）。
   - 提示词：先阅读 api/v1/assets.py::regenerate_asset，确认 payload.prompt 是"替换整段提示词"还是"追加"：
     · 如果是追加或有专门的修改意见参数 → 把审核意见作为该参数传入；
     · 如果是整段替换 → **不要**把审核意见当作提示词传入（会丢掉原有提示词），改为 prompt=None，
       并在审批说明（create_confirmation 的 goal）中写明审核意见，提示用户可在资产库中修改提示词后再批准。
       在 PR 中写明结论和依据。
   - 重新生成的结果是候选版本：该步骤完成后插入一个新的审核步骤，targets 为候选版本（D2 规定了审核候选版本 = 采用 + 通过）。
   - 同一实体驳回次数达到 settings.review_max_rounds（默认 3，依据 asset.review_round）→ 不再自动生成重做步骤，改为 waiting_user，
     提示用户在资产库手动处理（替换图片或修改提示词）。
6. 分镜图审核过期（keyframe_review_stale）：不自动重新生成。生成一个 waiting_user 步骤，列出受影响的镜头和被替换的参考资产，
   给出两个选择：
   a. "保留现有分镜图"：用户在页面或对话中重新审核通过；
   b. "重新生成这些分镜图"：生成 regenerate_shot 步骤（需要审批）。
   受影响范围用 dependency_impact.preview_dependency_impact 计算，不要自己遍历依赖图。

验收测试 tests/test_agent_phase_d_runtime.py：
- test_review_step_inserted_only_when_policy_on
- test_waiting_review_then_approve_resumes（页面审核接口 → 标记 → tick → 进入分镜图阶段）
- test_reject_generates_regenerate_step_with_approval
- test_review_targets_snapshot_not_silently_replaced
- test_max_rounds_goes_waiting_user
- test_keyframe_stale_waits_user_with_options
- test_policy_off_plan_identical_to_phase_c（开关关闭时计划与 Phase C 完全一致）
```

---

## D7 · 在对话中审核

```text
任务：Goal 停在 review_gate 等待时，允许用户直接在 /chat 中答复审核。

1. 识别：/chat 中当前会话存在 active Goal，且其 review_gate 步骤处于 waiting_approval 时，先尝试按审核答复解析：
   - 复用 review_confirmation.parse_confirmation_response 把整体意图映射为 approve / reject / revise / defer / 不确定；
   - 识别点名的对象：与该步骤快照 review_targets 中的名称（角色名、场景名、"第 3 镜"）做确定性匹配
     （复用 intent_resolver 已有的实体匹配；不调用 LLM 做最终判定）；
   - 支持的表述示例："都通过"、"角色都可以，场景重做"、"苏婉不行，脸太老了"、"第 3 镜和第 5 镜重画"。
2. 应用规则：
   - "全部通过"类答复只作用于**本步骤快照中、并且就是上一条助手消息中展示给用户的那些版本**
     （助手消息 payload 中记录 review_targets 的 asset_id + version；应用时逐条传 expected_version，版本不一致的跳过并提示）。
   - 驳回 / 修改时，用户原话中的修改意见作为 review_comment。
   - 有任何不确定（意图不确定、点名的对象匹配不到或匹配到多个）→ 不应用任何审核结论，回复澄清问题。
   - 通过 asset_review.review_assets 批量写入，reviewer="user:chat"；之后在同一请求中调用一次 tick，并按结果回复。
3. 不在等待审核时，这些表述按原有链路处理（不要误把"可以"当成审核通过）。

验收测试 tests/test_agent_phase_d_chat_review.py：
- test_approve_all_applies_only_shown_versions（展示后某实体又产生了新版本 → 该实体不被通过，回复中说明）
- test_partial_reject_with_comment
- test_ambiguous_name_asks_clarification_and_writes_nothing
- test_not_waiting_review_ignored
- test_reviewer_recorded_as_chat
```

---

## D8 · 最小前端支持

```text
背景：完整的 Agent 工作台是 Phase E 的范围；但开关默认对新项目开启，如果页面上没有审核入口，新项目会被卡住。
本任务只做"能审核、能看到为什么被拦"的最小改动，沿用现有组件和样式（参考 docs/uiux/design-system.md）。

任务：
1. 资产库（frontend/src/views/project/AssetsView.vue、components/assets/AssetCard.vue、AssetDetailModal.vue）：
   - 开关开启时，资产卡片显示审核状态标记（未审核 / 已通过 / 未通过），详情弹窗中提供"通过 / 驳回（需填写意见）"按钮，
     提交时带上 expected_version；
   - AssetBatchToolbar 增加"批量通过"；
   - AssetStatusBar 显示"已审 x/y"。
2. 分镜图页（StoryboardImagesView.vue）：同样的状态标记与审核按钮；显示 keyframe_review_stale 的提示（"参考资产已更新"）。
3. 项目设置：在项目设置入口（先找到现有的项目设置或风格配置界面所在位置）增加两个开关，
   开启时按 D1 的接口先展示影响数量并二次确认。
4. 门禁错误展示：生成接口返回的新 reason（asset_pending_review、keyframe_pending_review 等）在现有的错误提示组件中显示 suggestion，
   并提供跳转到对应审核位置的链接。
5. 开关关闭时，以上所有新 UI 都不显示（以 project.review_policy 为准）。
6. API 封装放在 frontend/src/api/ 下的对应文件中（新建 reviews.ts），类型定义与后端字段一致。

验收：
- 通过 frontend-checks 工作流中的检查（类型检查、lint、已有单测）；
- 如果项目中已有组件测试框架，为审核按钮的显示条件写测试；没有的话，在 PR 中附开关开启 / 关闭两种状态下的截图。
```

---

## D9 · 端到端场景测试（Phase D 验收门禁）

```text
任务：新增 tests/test_agent_phase_d_e2e.py。环境与 Phase B / C 的端到端测试相同（tmp 库、render mock、LLM monkeypatch、
测试代码直接驱动 Worker 单轮执行和 agent_resume.process_marked）。

场景（对应原始 Plan 第 21.3 节的场景编号）：
1. test_s4_assets_unreviewed：新项目（开关开启），目标 keyframes → 资产生成完成后停在 waiting_approval（审核），没有创建任何分镜图 Job。
2. test_s5_assets_approved：页面审核接口全部通过 → 自动进入分镜图生成 → 分镜图完成后停在分镜图审核。
3. test_s6_keyframes_unreviewed：分镜图未审核时，目标 clips 不产生任何视频 Job。
4. test_s7_keyframes_approved：审核通过 → 视频生成 → 完成。
5. test_s16_reject：驳回一个角色并附意见 → 出现 regenerate_asset 审批 → 批准 → 候选版本 → 再次审核通过 → 继续。
6. test_s17_version_changed：分镜图审核通过后，用户在资产库重新生成并采用了某个角色 → 依赖该角色的分镜图变为 stale →
   视频生成被阻断 → Agent 给出"保留 / 重新生成"两个选项 → 选择保留并重新审核 → 继续。
7. test_bypass_api_blocked：开关开启时，绕过 Agent 直接调用分镜图生成接口、视频生成接口、jobs 重试接口，全部被拒绝（入口 409 或 Worker 阶段失败）。
8. test_legacy_project_unchanged：存量项目（settings 中没有 review_policy）走完 Phase C 的 test_acceptance_partial_project，
   结果与 Phase C 完全一致。
9. test_enable_policy_mid_project：项目进行到一半时开启开关（confirm=true）→ 已有的 current 资产变为待审核 → 下一步被阻断，回复中列出待审项。
10. test_chat_review_flow：全程只通过对话完成审核（"都通过"、"第 2 镜重画"）。

完成标准：以上全部通过；Phase A–C 的全部测试仍然通过。
```

---

## D10 · 收尾：文档与决策记录

```text
任务：
1. docs/DECISIONS.md：新增一条决策记录——"人工审核由 2026-11 的取消，改为按项目开关启用"，写明：
   背景（全自动执行时坏资产会传导到分镜和视频）、方案（资产行级审核字段、项目开关、存量默认关闭）、
   与旧设计的区别（不复用 assets_review_status / keyframe_confirmed，审核绑定版本行）、影响范围。
2. docs/agent-runtime/target-architecture.md / data-model-design.md：补充审核字段、review_gate 步骤、Worker 兜底检查。
3. docs/API.md：D1、D2 的新接口，以及生成接口新增的错误 reason。
4. docs/WORKFLOW.md / FRONTEND.md 中描述"审核已取消"的段落，更新为"可按项目开启"。
5. 遗留字段：在 Project.assets_review_status 和 Shot.keyframe_confirmed 的模型注释中注明"遗留字段，不参与任何判定，
   审核见 asset.review_status"。不删除字段，不删除现有的重置代码（避免影响旧数据）。
6. implementation-plan.md 勾选 D0–D10，并列出 Phase E 需要的前端能力（目标卡片、计划步骤、审核队列与 Agent 等待状态联动）。

完成标准：文档中的代码路径都真实存在；全量 pytest 失败数不多于 Phase C 结束时；ruff 无新增告警。
```

---

## 附：Phase D 完成后的状态（供 Phase E 衔接）

| 已具备 | 仍缺（后续阶段） |
|---|---|
| 项目级审核开关：新项目默认开、存量默认关、开关关闭零变化 | Agent 工作台：目标卡片、计划步骤、等待状态的统一入口（E） |
| 审核绑定到资产版本行，新版本必须重新审核 | 审核队列与 Agent 进度的联动展示（E） |
| 资产门禁、分镜图门禁在服务端执行，Worker 兜底拦截所有绕过路径 | Memory、指标、场景评测进 CI（F） |
| Runtime 会等待审核，驳回后申请重新生成，超过轮数交给用户 | 视频审核（原始 Plan 提到，本期未做） |
| 上游资产变化后，下游分镜图审核自动过期，由用户决定保留或重做 | |
| 对话中审核，只作用于展示给用户的具体版本 | |
