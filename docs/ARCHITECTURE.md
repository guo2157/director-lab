# ARCHITECTURE —— 系统架构与状态机

> 本文档是架构与状态机的唯一权威定义。功能需求见 PRD.md，接口字段见 API.md，表结构见 DATABASE.md。

## 1. 架构总览

| 组件 | 形态 | 职责 |
| --- | --- | --- |
| app（FastAPI） | 本机进程，托管前端静态文件，监听内网端口 | HTTP 接口、SSE 事件流、业务编排 |
| worker（单实例） | 与 app 同机 | 任务轮询执行、ComfyUI 提交与轮询、ffmpeg 合成 |
| SQLite（WAL） | 本机文件 | 元数据、任务队列、事件 outbox |
| ComfyUI | 外部独立服务（IP:Port 连接） | 渲染执行，经 ComfyKit 客户端调用 |
| 语言模型 | 外部服务（OpenAI 兼容接口） | 剧本评估与解析，独立于 ComfyUI 工作流 |

选型理由（关键决策，详见 DECISIONS.md）：SQLite + WAL 匹配单机小团队定位；数据库表 + 单 worker 取代外部消息队列，保证任务状态可直接查询、修改与恢复；并发 = 可用 ComfyUI 实例数（D-18 修订 D-03）：单实例串行（ComfyUI 为串行队列、显存易溢出、`/interrupt` 是全局中断），多实例由实例池加权轮询分发并行执行。

## 2. 主流程与阶段

主流程 12 步（2026-10-26 D-22 修订，原评估集数与选择范围两步已合并）：创建项目 → 输入剧本 → script-agent 一体化分析（算法预检 + 软闸口评估 + 自动分批解析，集数由 Agent 决定，用户不再确认集数）→ 人工审核分镜 → 角色定妆图 → 场景基准图 → 逐镜分镜图 → 一致性校验 → 逐镜 H3 VA 音视频 → 检查与按需 TTS 补救 → 合成 → 导出。旧端点（estimate/estimates-confirm/script-evaluate/parse）兼容保留；首批自动解析，has_more 时前端提供『继续分析』入口。

生产阶段枚举：`estimate`、`select_range`、`parse`、`characters`、`scenes`、`assets_review`、`keyframes`、`clips`、`audio`、`compose`、`export`、`done`。合成与导出是两个独立阶段；创建项目/输入剧本属前置准备，人工审核属人工闸口，一致性校验属分镜图阶段内判定，均不产生独立阶段。D-22 后 `estimate`/`select_range` 不再对应独立人工交互步骤，仅作为 progress.py 的派生状态标记（未评估→estimate、已评估未解析→select_range）兼容保留。

资产分层与前端流转（2026-10-25，D-21）：解析产出的角色定妆图/场景基准图/道具锚定图为**项目级公共资产**（对应阶段 characters/scenes 与道具，跨集复用，按实体名称绑定，供 @名称 引用解析与生成六槽位注入）；分镜图/分镜视频/末帧为**集级资产**（对应阶段 keyframes/clips，按集归属）。前端顶级导航为 5 个菜单：剧本（保存→评估→解析子流程）→ 资产库（公共资产生图 + 资产人工审核）→ 分镜图（集级分镜图生成与逐镜人工确认，路由 `/project/:id/storyboard-images`）→ 分镜看板（集级分镜视频生成）→ 合成与导出；页面单一出口流转链：解析完成→去资产库、公共资产齐全→提交人工审核、审核通过→去分镜图、分镜图逐镜确认完毕→去分镜看板、分镜视频就绪→去合成导出，禁止跨级跳转。

资产定义链路（2026-10-28，AssetPlan 第二阶段）：解析落库后由 `build_asset_plans` 一次性定稿五类定义（character/scene/prop/keyframe/clip）的名称、占位路径、提示词与参考依赖（`asset_plan`/`asset_plan_dependency` 表，迁移 f3a7c1d9e2b8）。名称与路径一经生成永不修改，提示词随重新解析刷新。各环节批量生成（资产库批量制作、分镜图批量生成、分镜看板批量出视频）改为按定义触发（`POST /projects/{id}/asset-plans/generate`），无匹配定义时回退镜头级/项目级通道。参考图槽位由定义绑定决定（不依赖提示词书写顺序），仅注入工作流 `capabilities.reference_slots` 声明的槽位；绑定快照（槽位/资产ID/版本/路径/状态）随任务参数持久化，重试复用快照不重新解析（版本漂移防护）。分镜图/视频定义提示词用 `@名称` 语法引用角色/场景；`ref_image`/`ref_prev_frame`/`first_frame`/`end_frame` 为系统保留槽位，由执行器按镜头实际产物回填。闸口：keyframe 须公共资产审核通过（D-23）且跳过已锁定分镜图；clip 须分镜图已确认（D-13）。

## 3. 四套状态机（P0，必须分别保存，不得互相替代）

### 3.1 项目生命周期状态机

| 状态 | 含义 | 可进入的下一状态 |
| --- | --- | --- |
| draft | 草稿，可自由编辑剧本与分镜结构 | active |
| active | 生产进行中，允许任意阶段的生成与重做 | archived |
| archived | 已归档，只读，不可生成 | active（取消归档） |

规则：仅允许按上表流转；进入 active 后各阶段可任意顺序推进；任一步骤重做不改变项目状态。

draft → active 自动激活：不提供显式激活接口。首次调用以下任一接口时后端自动置为 active，同一项目仅触发一次：`POST /api/projects/{id}/parse`、`POST /api/characters/{id}/generate`、`POST /api/scenes/{id}/generate-baseline`、`POST /api/props/{id}/generate`、`POST /api/shots/{id}/generate`、`POST /api/scenes/{id}/generate`、`POST /api/projects/{id}/generate`、`POST /api/shots/{id}/regenerate`。自动激活在请求校验通过、任务入队成功之后执行。

归档/取消归档：`POST /api/projects/{id}/archive`（active→archived）、`POST /api/projects/{id}/unarchive`（archived→active），其余状态调用返回 409 `invalid_state_transition`。archived 状态下所有生成类接口返回 409，读取类接口不受限制。

### 3.2 镜头制作状态机

`pending` / `generating` / `review` / `done` / `redo`。

- `pending` 可附带 `blocked_reason` 子状态（如 `blocked_by_dependency`），仍属于 pending 而非独立状态值
- `done` 表示当前必需镜头产物齐全且依赖版本匹配，不代表已合成或已导出
- 音画超容差且待审核时镜头状态为 `review`
- 合成状态、导出状态分别由 composition 与 export 记录表达，不在镜头状态中体现

### 3.3 任务状态机

| 状态 | 含义 | 终态 |
| --- | --- | --- |
| queued | 已入队 | 否 |
| running | 执行中 | 否 |
| submission_unknown | 已发起远端提交但无法确认远端是否接收，等待对账 | 否 |
| succeeded | 成功 | 是 |
| failed | 失败 | 是 |
| lost | 远端任务确认丢失 | 是 |
| cancelled | 已取消 | 是 |

### 3.4 产物有效性状态机

`current` / `stale` / `superseded` / `archived`。依赖输入版本变化后标记 `stale`，不物理删除。

边界约定：任务失败不意味着镜头数据丢失；状态迁移必须记录时间与原因；取消、重试、强制继续、重新解析、删除均需遵守依赖与版本规则。

## 4. 生产进度派生（P0）

项目状态只表达生命周期，生产进度为派生字段，由剧本版本、镜头必需产物、合成与导出任务实时聚合，通过 `GET /api/projects/{id}` 返回 `shot_total`、`shot_done`、`current_stage`。

### 4.1 current_stage 推导规则（按顺序取第一个满足条件的值）

1. `estimated_text_hash` 为空，或与当前剧本文本哈希不一致 → `estimate`
2. 已评估且剧本未变更，但不存在解析任务 → `select_range`
3. 存在解析任务但项目内镜头数为 0 → `parse`
4. 存在角色 `portrait_asset_id` 为空 → `characters`
5. 全部角色已具备定妆图，但存在场景 `baseline_asset_id` 为空 → `scenes`
5.5 公共资产（角色定妆图/场景基准图/道具锚定图）已齐全，但 `project.assets_review_status != approved` → `assets_review`（D-23 资产人工审核闸口）
6. 存在分镜图未完成或未人工确认（keyframe_confirmed=false）的镜头 → `keyframes`（D-13 静图先行闸口）
7. 全部分镜图完成，但存在视频未完成的镜头 → `clips`
8. 全部视频完成，但存在按 `shot_kind` 需要音频且 H3 音频检查未通过、又尚未完成 TTS 补救的镜头 → `audio`
9. 任一有配音镜头音画时长差超过 `compose_tolerance` 且尚未审核放行 → `audio`（镜头 review，阻止合成）
10. 全部必需产物齐全且当前有效合成版本不存在或已过期 → `compose`
11. 当前有效合成任务成功，但尚无匹配该合成版本的成功导出记录 → `export`
12. 导出任务成功且记录可访问、来源合成版本仍有效 → `done`

review 放行转换条件（镜头制作状态机补充）：镜头因音画时长差超 `compose_tolerance` 进入 `review` 后，仅人工放行可转回 `done`。放行操作须记录审核人、审核时间与放行时的产物快照（视频/音频 asset_id 与版本、时长差值）；放行后镜头恢复 `done`，合成校验按放行记录放行该镜头。重生成视频/音频、或上游变更使相关产物失效（标 stale/redo）时，放行记录自动失效，镜头回到对应待生成/待重做状态，须重新生成并重新审核；失效的放行记录保留存档不删除。

补充规则：
- 阶段内部分完成是正常状态；失败与重做使对应产物重新进入未完成集合；依赖失效时合成与导出阶段回退到最靠前的未完成阶段
- 第 4、5、5.5 条为项目级判定，不受 episode 参数影响：只要项目内仍有角色或场景未锁定，或公共资产未通过人工审核，按集统计时 current_stage 同样停在 characters/scenes/assets_review

### 4.2 阶段判定依据（不得以 shot.status 作为分段依据）

| 阶段 | 判定依据 |
| --- | --- |
| 分镜图是否具备 | 存在 `asset_type=keyframe` 且由 succeeded 任务产出的产物 |
| 视频是否具备 | 存在 `asset_type=clip` 且由 succeeded 任务产出的产物 |
| 配音是否具备 | 存在 `asset_type=audio` 的产物；`shot_kind=visual/transition` 且配置允许无配音时不要求 |

约定：不使用 `asset_ref` 判断产物是否具备（该表只表达引用关系）；重做后旧产物保留，判定以依赖版本匹配、未标记失效且由成功任务产出的当前版本为准，不得只按版本号或完成时间判定；「未完成」同时包含从未生成与已生成但被标记 redo 两种情况。

音频中间态（P0）：重生成 H3 视频后，旧 TTS 补救音频按失效传播矩阵标记 superseded；在新 H3 音频完成生成后检查（音轨存在性、有效性、对白检查）之前，镜头处于音频中间态，此时 `audio_source` 记为 `none`、镜头状态回到 `review`，配音判定按『不具备』处理（`actual_duration` 同步清空，合成排轴不得引用旧值）；检查通过后按结果回写 `h3_va` 或重新触发 TTS 补救（回写 `tts_fallback`）。

### 4.3 集数统计口径

`shot_total`、`shot_done`、`current_stage` 均支持可选 `episode` 参数：传入则仅统计该集；未传则统计全部集。未传 episode 且存在多集时，任一集未完成即视为对应阶段未完成。

## 5. 任务系统

### 5.1 队列设计

数据库表 + 单 worker 进程轮询，不引入外部消息队列。所有远端生成调用必须在短数据库事务之外执行，禁止持有事务等待上传、生成、下载、ffprobe 或 FFmpeg。

### 5.2 并发控制

并发 = 可用 ComfyUI 实例数（D-18 修订 D-03）：Mock 模式/单实例保持串行（与旧行为一致）；多实例时任务由实例池加权轮询分发并行执行，单实例同一时刻至多执行一个任务。主循环为唯一取任务者（无取任务竞态），执行线程池消费，实例配置运行时生效每轮重算并发度。`POST /api/jobs/{id}/cancel` 在任务 running 时调用 ComfyUI `/interrupt`，该接口中断目标 ComfyUI 上所有正在执行的任务：若 ComfyUI 与其他使用者共用，取消可能中断他人生成，界面需二次确认并明确提示；建议为 ComfyUI 配置独立实例。

### 5.3 持久化差异

| 维度 | 图像任务 | 视频任务 |
| --- | --- | --- |
| 任务 ID 持久化 | 是 | 是 |
| 写入时机 | 提交后立即落库；落库失败记录 `prompt_id` 待补并在下次轮询前重试，不得静默丢弃 | 同左 |
| 服务重启后 | 继续轮询，超时后按 `comfy_job_lost` 处理 | 继续轮询 |
| 失败处理 | 优先续跑，确认远端无该任务后再重跑 | 优先续跑，避免浪费显存与时间 |
| 标记 lost 条件 | 仅当远端任务 ID 缺失且无法补查时 | 同左 |

### 5.4 恢复机制

worker 启动时扫描 `running`、`submitting`、`submission_unknown` 及有远端任务 ID 的记录，优先查询远端任务/历史，再决定续轮询或重试。提交前持久化 `request_key` 与提交意图，提交后立即保存远端 ID。若远端已接收但本地未保存 ID 且无法对账，标记 `submission_unknown`，不得无条件再次提交，由对账任务或人工确认处理。远端不支持幂等键时不承诺 exactly-once，只提供去重与对账。视频轮询超时下限 60 秒，可配置更长。

进程管理：裸机部署下 app 与 worker 均由 systemd 管理，`Restart=always`、`RestartSec=5`；迁移 Docker Compose 后由容器 restart policy 接管。

### 5.5 失败分类（界面应对的唯一权威分派表）

| 类型 | 场景 | 错误码 | 界面应对 |
| --- | --- | --- | --- |
| 环境类（可重试） | 上传失败 | comfy_upload_failed | 展示重试按钮 |
| 环境类（可重试） | 任务丢失 | comfy_job_lost | 展示重试按钮 |
| 环境类（可重试） | 被中断 | comfy_interrupted | 展示重试按钮 |
| 环境类（可重试） | 超时 | comfy_timeout | 展示重试按钮 |
| 环境类（可重试） | ComfyUI 不可达 | comfy_unreachable | 展示重试按钮 |
| 环境类（可重试） | 数据库忙 | database_busy | 展示重试按钮 |
| 环境类（可重试） | 提交结果未知 | job_submission_unknown | 展示重试按钮，提示系统正在对账 |
| 环境类（可重试） | 连通性测试失败 | comfyui_test_failed | 展示重试按钮 |
| 配置类（需修改） | 节点报错 | comfy_node_errors | 展示去修改工作流指引 |
| 配置类（需修改） | 执行错误 | comfy_execution_error | 展示去修改工作流指引 |
| 配置类（需修改） | 产物缺失 | output_missing | 展示去修改工作流指引 |
| 配置类（需修改） | 产物类型不符 | output_type_mismatch | 展示去修改工作流指引 |
| 配置类（需修改） | 容器不符 | output_container_mismatch | 展示去修改工作流指引 |
| 配置类（需修改） | 绑定无效 | workflow_marker_missing / workflow_media_missing | 展示去修改工作流指引 |
| 配置类（需修改） | H3 参考或时长不合规 | h3_reference_required / h3_duration_unrepresentable | 展示调整参考图或镜头时长指引 |
| 配置类（需修改） | Partner Node 登录/额度/网络 | comfy_partner_auth_failed | 展示去 ComfyUI 登录或充值指引 |
| 配置类（需修改） | 无匹配工作流 | no_matching_workflow | 展示去上传工作流或手动指定指引 |
| 配置类（需修改） | 工作流能力不匹配 | workflow_capability_mismatch | 展示去更换工作流指引 |
| 配置类（需修改） | 文件超出上限 | file_too_large | 展示压缩或裁剪后重新上传指引 |

### 5.6 产物校验

- 白名单：图像仅收 png / jpg / jpeg / webp；视频仅收 mp4 / mov / m4v
- 文件头校验：读取文件头验证容器格式，视频再以 ffprobe 验证编码与时长
- 源头约束：工作流视频输出统一使用 h264-mp4 编码的保存节点，避免下游合成失败
- 产物落盘：worker 收到产物后按 `{asset_id}_{version}.{ext}` 重命名再落盘（如 `ast_abc123_v3.png`），避免重名冲突

### 5.7 资源消耗保护

| 项 | 约定 |
| --- | --- |
| 重试上限 | 同一任务累计重试（自动+手动）不超过设置项 `job_max_attempts`（默认 3，范围 1–10）；达上限后 `retryable=false`，仅展示处理指引，防止无限重试消耗算力与云端计费额度 |
| 队列积压 | 单项目 queued 任务超过 200 个时，新生成类接口返回 429 `too_many_jobs`，提示先等待或取消部分任务；解析/合成/导出类不受此限 |
| 超大任务 | 批量生成按场景或项目分批入队，单批不超过 `max_episodes_per_batch` 对应的镜头规模，避免单次提交数百任务导致队列长时间占满 |

### 5.8 多 ComfyUI 实例负载均衡（三层调度 + 故障转移，D-19/D-21/D-22/D-23）

生成通道可扩展：支持配置多个 ComfyUI 实例，采用三层负载均衡策略分发任务。实例配置经设置页 ComfyUI 区管理，接口见 API.md（GET/PUT /api/settings/comfyui-instances、POST /api/settings/comfyui-instances/test、GET /api/settings/comfyui-instances/status）。

#### 5.8.1 实例配置

| 项 | 约定 |
| --- | --- |
| 实例配置 | 每个实例含 `base_url`、`weight`（正整数）、`enabled`；未配置实例时回退单实例 `comfyui_base_url` |
| 可追溯性 | 实际使用的实例写入 job 的 `input_snapshot.comfyui_instance`；任务取消时按该实例发送中断 |
| 测试接口 | 逐实例最小探测（GET /system_stats），不提交生成任务 |
| 监控面板 | GET /api/settings/comfyui-instances/status 返回各实例在线状态、显存（vram_total/free）、队列深度（pending/running）、熔断状态 |

#### 5.8.2 第一层：实例健康筛选（熔断机制，D-19）

后台心跳线程（daemon，随 worker 启动）定期探测所有启用实例的可达性，维护进程内熔断状态：

| 项 | 约定 |
| --- | --- |
| 心跳间隔 | 30 秒一轮，遍历所有启用实例调用 GET /system_stats |
| 熔断阈值 | 连续 3 次探测失败 → 熔断摘除 60 秒 |
| 半开恢复 | 熔断到期后进入半开状态（10 秒探测间隔），探测成功即恢复，再失败则重新熔断 |
| 调度过滤 | pick_instance / pick_instance_by_load 优先从健康实例中选择；全部熔断时降级为直接尝试（不静默丢弃） |
| 健康摘要 | get_health_summary() 返回各实例 fail_count / circuit_open / last_check，供监控面板展示 |

#### 5.8.3 第二层：实时负载调度（least-connections，D-19）

任务分发从静态加权轮询升级为按实时负载选择最空闲实例：

| 项 | 约定 |
| --- | --- |
| 负载查询 | 对每个健康实例调用 GET /queue，load = queue_pending 数量 + queue_running 数量 |
| 评分公式 | score = load / weight（权重越高可承担越多负载）；选评分最低的实例 |
| 同分处理 | 同分时取权重高的实例 |
| 降级策略 | 全部实例无法查询负载时（如 /queue 不可用），降级为加权轮询 pick_instance() |
| exclude 参数 | 支持排除指定 base_url 列表（任务级故障转移重试时跳过已失败实例） |

#### 5.8.4 第三层：任务级故障转移重试（D-21）

提交前阶段失败时，在未尝试过的实例间自动换实例重试：

| 阶段 | 失败行为 |
| --- | --- |
| 获取客户端 | acquire_client_by_load 内部故障转移：探测失败 → 记录健康状态 → 按权重尝试其余实例 |
| 上传媒体 | 有剩余未尝试实例 → 写 job.retry 事件 → 返回 retriable=True → 外层循环换实例重试 |
| 提交 prompt | 有剩余未尝试实例 → 写 job.retry 事件 → 返回 retriable=True → 外层循环换实例重试 |
| 等待完成 | 不换实例，保持原失败语义（超时/执行错误直接失败） |
| 下载产物 | 不换实例，直接失败 |

重试时 exclude 列表累加失败实例 base_url，直到全部实例尝试完毕才最终 _fail_job。重试时 seed 从 job.params 复用（首次生成后持久化），保证重试产出一致。

#### 5.8.5 同镜头任务亲和性（D-22）

| 项 | 约定 |
| --- | --- |
| 亲和查询 | _preferred_instance_for_shot：查询同镜头最近成功任务（status=succeeded）的 input_snapshot.comfyui_instance |
| 复用条件 | 该实例仍在启用列表且未被 exclude |
| 传入方式 | 作为 preferred 参数传入 acquire_client_by_load，优先使用；不可用时降级为 least-connections |
| 作用 | 同镜头 keyframe→clip 存在输入依赖（clip 用 keyframe 做首帧），复用同实例可复用已上传媒体文件 |

#### 5.8.6 优先级队列（D-22）

| 项 | 约定 |
| --- | --- |
| 字段 | Job.priority（整数，默认 0），alembic 迁移 d8f3a2b6c9e1 已执行 |
| 排序 | _next_job 按 priority DESC, created_at ASC 取任务 |
| 重试提权 | 重试任务（attempts>0）调度时 priority + 100，保障失败任务尽快重跑 |
| 索引 | ix_job_status_priority（status + priority 联合索引） |

#### 5.8.7 WebSocket 进度推送（D-19）

| 项 | 约定 |
| --- | --- |
| WS 等待 | wait_for_job_ws：订阅 /ws?clientId=... 事件流，监听 progress / executing / execution_error / execution_success |
| 进度回调 | on_progress 回调将 value/max/node 写入 job.progress 事件（事件与状态同事务），前端可展示进度条 |
| 降级策略 | WS 断连/不可用时自动降级为 HTTP 轮询 get_history，任务不因 WS 故障丢失 |
| 预览帧 | 跳过 WS 二进制预览帧数据 |
| 等待窗口 | 每 15 秒为一个 WS 等待窗口，窗口间检查取消状态；总截止为 comfyui_job_timeout_seconds |

#### 5.8.8 submission_unknown 对账自动化（D-23）

worker 启动时自动对账带 prompt_id 的 submission_unknown 任务：

| 远端状态 | 处理 |
| --- | --- |
| history 有结果且成功 | 保持 submission_unknown，executor 续跑分支直接下载产物（跳过上传/提交/等待），写 job.reconciled 事件 |
| history 有结果但失败 | _fail_job 记录错误 |
| 无结果且不在远端队列 | 确认未提交成功，重置 queued + 清除 prompt_id，安全重提 |
| 无结果但仍在远端队列 | 保持 submission_unknown，下轮启动再对账 |
| 实例不可达 | 跳过，保持现状，下轮再对账 |

#### 5.8.9 单实例 / Mock 模式退化

- 单实例：least-connections 只有一个候选，亲和性总是命中同一实例，exclude 无剩余实例不触发重试 → 行为与改造前一致
- Mock 模式：跳过所有实例池逻辑，mock_outputs 生成占位产物 → 行为与改造前一致

#### 5.8.10 切换范围与纪律

实例选择在 worker 提交前执行，属于短事务外的远端调用；故障转移尝试本身不得阻塞任务队列超过单次提交超时。已在执行的任务不迁移实例，按原实例完成或失败。

## 6. 事件持久化与断线补拉（P0）

任务状态与待发布事件必须在同一数据库事务中写入事件表/outbox；事件发布器提交后异步推送 SSE。每个事件包含单调递增 `event_seq`。客户端断线后按最后确认的序号补拉，重复事件必须幂等处理，不允许状态倒退。禁止在事务提交前发布事件。

## 7. 非功能需求

| 类别 | 要求 |
| --- | --- |
| 部署 | 裸机部署，app 与 worker 同机，ComfyUI 为外部独立服务；后续可迁移至 Docker Compose |
| 资源 | 主应用常驻内存不超过 1GB |
| 并发 | 单用户，生成任务并发 = 可用 ComfyUI 实例数（单实例串行） |
| 可用性 | 服务重启后进行中的图像与视频任务均可按远端任务 ID 恢复，不重复消耗算力 |
| 数据安全 | 素材不对外网暴露，默认仅监听内网；H3 Partner Node 参考图与提示词经 ComfyUI 发送至 Comfy/MiniMax 云端，须在设置页明示并可停用对应工作流 |
| 可维护性 | 日志可定位到任务 ID 与工作流版本 |
| 浏览器 | 支持最新版 Chrome 与 Edge |

安全与资源限制：媒体上传、工作流解析、外部 URL 请求和 FFmpeg 执行均设定大小、时长、路径与资源限制；凭据不得明文写入日志或任务错误详情；删除采用软删除与延迟物理清理，运行中任务引用的文件必须保留到任务终态且超过保留期；对外访问不得沿用『仅内网无鉴权』的默认配置。

## 8. 技术栈与版本

后端：FastAPI + uvicorn 单进程；SQLite + WAL；SQLAlchemy 2.0 + Alembic；数据库表 + 单 worker 队列；ffmpeg/ffprobe 子进程；pydantic-settings；ComfyKit 客户端。

前端：Vue 3 + Vite + TypeScript；Naive UI；Vue Router；Pinia；TanStack Query；vue-draggable-plus；ArtPlayer；SSE。

版本下限：Python 3.11、Node 20、ffmpeg 6.0。

## 9. 备份与恢复

### 9.1 备份范围

| 对象 | 是否备份 | 说明 |
| --- | --- | --- |
| SQLite 数据库 | 是 | 全部元数据 |
| 媒体文件目录 | 是 | 按项目与类型分桶的图像、视频、音频产物 |
| 系统设置 | 是 | 含 ComfyUI 地址与语言模型密钥，需加密存放 |
| ComfyUI 本体与模型权重 | 否 | 外部独立服务，由其自身维护 |
| 上传临时文件 | 否 | 可重新上传生成 |

### 9.2 一致性要求（P0）

数据库开启 WAL 且 worker 持续写入，禁止在服务运行时直接拷贝主库文件与 `-wal`、`-shm` 文件。必须使用以下任一方式产生一致性快照：

- `sqlite3 data/app.db ".backup /backup/app-{ts}.db"`
- `VACUUM INTO '/backup/app-{ts}.db'`

媒体文件备份时同步生成文件清单，记录相对路径、文件大小与 sha256 校验和，供恢复后比对。

### 9.3 备份策略

| 项 | 约定 |
| --- | --- |
| 触发方式 | 每日定时（systemd timer 或 cron）加手动触发入口 |
| 保留策略 | 最近 7 日每日一份，最近 4 周每周一份 |
| 存放位置 | 与应用数据不同磁盘；异地或对象存储作为可选增强 |
| 加密 | 备份包必须加密后传输与存放 |
| 完整性校验 | 备份完成后执行 `PRAGMA integrity_check`，并核对媒体文件数量与清单一致 |
| 失败处理 | 备份失败必须告警，不允许静默失败 |

### 9.4 恢复流程

1. 停止 app 与 worker 进程
2. 备份当前残留数据，防止误覆盖造成二次丢失
3. 恢复数据库文件到 `data/app.db`，删除残留的 `-wal` 与 `-shm` 文件
4. 恢复媒体文件目录
5. 校验：`PRAGMA integrity_check`，比对文件清单与校验和，抽样打开若干产物确认可读
6. 先启动 worker，观察日志无异常后再启动 app
7. 验证：项目列表可正常返回，随机打开一个项目详情，播放一张分镜图与一段视频

### 9.5 数据库迁移

| 项 | 约定 |
| --- | --- |
| 迁移工具 | Alembic 管理 schema 版本 |
| 升级前置 | 强制执行一次完整备份才允许升级 |
| 脚本要求 | 每个迁移脚本必须提供 downgrade 实现 |
| 启动校验 | app 启动时比对 `alembic_version` 与代码期望版本，不一致拒绝启动并明确提示当前与期望版本 |
| 回滚 | 迁移失败时按 downgrade 回退，并从备份恢复数据 |

### 9.6 目标与边界

| 指标 | 目标值 | 说明 |
| --- | --- | --- |
| RPO | 24 小时 | 按日备计算，重要操作前建议手动备份 |
| RTO | 1 小时 | 单机手动恢复，不含硬件更换 |
| 高可用 | 不承诺 | 单机单点部署，定位为个人或小团队工作台 |

## 10. 版本与依赖总规则（P0）

- 剧本、解析结果、提示词、角色/场景/道具资产、工作流和生成产物均需可追溯版本
- 每个生成产物记录输入快照或版本引用（提示词、参考资产、工作流、模型参数、前镜尾帧）
- 新版本不得静默覆盖旧版本；只有依赖版本匹配的产物才能标记为 `current`
- 「任务成功」不等于「产物当前有效」，也不等于「审核通过」
- 显式记录镜头到提示词版本、参考、前镜尾帧、工作流版本、产物及合成结果的依赖关系
- 上游变更先逻辑失效，再由用户确认是否重做；自动返工仅在用户明确开启时执行
- 引用计数用于展示，依赖关系表为权威来源；软删除与物理清理分离
