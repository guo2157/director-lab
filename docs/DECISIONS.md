# DECISIONS —— 已确认决策与待决事项（TBD）

> 本文档是技术决策记录的唯一权威位置：已确认决策记录『为什么这么定』，权威定义本身仍在各专业文档；TBD 记录首次真实联调必须回填/确认的实测项。使用规则见 AGENTS.md。

## 1. 使用规则

1. **权威定义唯一**：接口以 API.md 为准；表结构以 DATABASE.md 为准；状态机以 ARCHITECTURE.md 为准；工作流契约以 WORKFLOW.md 为准。本文档只记录决策理由与未决事项，不重复定义。
2. **冲突必须上报**：发现文档间冲突时须停下，在本文档第 5 节记录并上报，禁止自行二选一静默实现。
3. **TBD 不得当需求实现**：第 3 节标记 TBD 的事项禁止写成既定代码，实现时须留配置/回填入口；回填后将条目移入第 2 节并注明实测来源。

## 2. 已确认决策

| 编号 | 决策 | 理由 | 权威定义 |
| --- | --- | --- | --- |
| D-01 | SQLite + WAL 作为唯一存储 | 匹配单机/小团队定位，零外部运维依赖 | ARCHITECTURE.md §1、DATABASE.md |
| D-02 | 数据库表 + 单 worker 取代外部消息队列 | 任务状态可直接查询、修改与恢复 | ARCHITECTURE.md §1 |
| D-03 | 渲染并发固定为 1 | 本地 ComfyUI 为串行队列、显存易溢出，且 ComfyUI `/interrupt` 是全局中断 | ARCHITECTURE.md §1 |
| D-04 | 四套状态机分别保存、不得互相替代 | 生命周期/镜头制作/任务执行/产物有效性语义正交，混用会丢失失败与对账信息 | ARCHITECTURE.md §3 |
| D-05 | 合成与导出为两个独立阶段 | 合成产物可复用，多格式导出不重复合成 | ARCHITECTURE.md §2、API.md |
| D-06 | H3 VA 为主链路，TTS 仅作补救 | H3 音画同源避免对齐问题；TTS 段间静音与时长不可控，仅用于音画超容差后的补救 | WORKFLOW.md、PRD.md M7 |
| D-07 | 一致性采用双阈值 0.85 / 0.70 | 单阈值无法区分『自动放行 / 人工审核 / 强制返工』三档处置 | CONSISTENCY.md |
| D-08 | 失效传播手动触发、stale 不物理删除 | 避免级联失效造成不可预期的大面积返工，保留可追溯性 | ARCHITECTURE.md §3.4、CONSISTENCY.md |
| D-09 | draft→active 自动激活，不提供显式激活接口 | 减少状态操作入口，首次生成即激活语义明确，同一项目仅触发一次 | ARCHITECTURE.md §3.1 |
| D-10 | H3 视频生成切换为本地权重原生节点架构（UNETLoader + MiniMaxH3ReferenceToVideo / MiniMaxH3ImageToVideo），弃用云端 Partner Node | 免云端按秒计费、参考图与提示词不出境、无 Comfy 账号依赖；代价为需 ≥24GB 显存实例（验证目标 A10，V100 未验证）与 5 项本地权重部署 | WORKFLOW.md、wf_video_h3_*.json 及 meta |
| D-11 | Mock 渲染模式：`DLAB_RENDER_MOCK=true` 时 worker 执行器跳过 ComfyUI 实例池/上传/提交/轮询/下载，直接生成占位产物（Pillow 占位图 / ffmpeg testsrc 视频 / 正弦波音频），任务照常走完整状态机与产物入库 | 无 ComfyUI 环境可演示全流程与跑端到端测试；拦截点收敛在 executor 内部，SSE/前端/状态机零改动；占位产物含 [MOCK] 标识与 job 尾号，可肉眼区分 | config.py render_mock、app/services/mock_render.py、app/worker/executor.py |
| D-12 | 零配置启动：应用 lifespan 启动时自动执行 alembic upgrade head（幂等）并自检 ffmpeg/ComfyUI，检测结果记日志、不阻断启动 | 降低新环境部署门槛（免手动迁移）；单机单进程定位下自动迁移无并发风险；ComfyUI 不可达仅告警并提示 DLAB_RENDER_MOCK，真实生成时才硬性依赖 | app/main.py _startup_selfcheck |
| D-13 | 静图先行闸口：shot 表新增 keyframe_confirmed 字段，生成 clip 前校验分镜图已人工确认（未确认返回 409 keyframe_not_confirmed）；重做分镜图或分镜图标 stale 时确认自动失效；current_stage 规则 6 同步纳入『未确认』判定 | 把『确认』与『烧算力』分离，避免分镜错误导致本地 GPU 视频白跑；确认失效复用现有依赖版本/stale 规则，不新增平行状态体系 | app/api/v1/shots.py、app/api/v1/generate.py、app/services/progress.py、ARCHITECTURE.md §4.1 规则 6 |
| D-14 | 失败任务批量重试：POST /api/jobs/projects/{id}/retry-failed 按 failed/lost 且未达 job_max_attempts 筛选批量创建新任务（复用单任务重试语义：新任务 ID、继承累计次数），已达上限的单独返回 skipped_capped | 本地 ComfyUI 失败率高（显存溢出/模型缺失），逐个重试成本高；重试即新建任务与既有任务状态机一致，旧记录保留可审计 | app/api/v1/jobs.py |
| D-15 | 提示词外置 + UI 可编辑：LLM 提示词从 workspace/prompts/*.md 加载（文件优先，缺失回退代码内置默认值），settings 路由提供列出/读取/写入/重置端点，写入自动备份（最多 5 份），每次调用动态读取保存即生效 | 适配本地 LLM 时提示词需免改代码调优；回退默认值保证零配置可用；文件级备份避免误写不可恢复 | app/services/prompt_store.py、app/api/v1/settings.py、app/worker/parse_executor.py |
| D-16 | @引用注入：提示词中 @角色名/@场景名 自动映射定妆图/基准图，按工作流 capabilities.reference_slots 槽位注入参考图（ref_identity/ref_scene）；未匹配返回 422、资产缺失返回 409，不静默忽略 | 一致性交给系统而非用户手动挑参考图；槽位过滤避免向不支持的工作流注入无效参数；明确报错避免静默丢参考 | app/services/ref_inject.py、app/api/v1/generate.py |
| D-17 | 产物缩略图：worker 产物落库时生成缩略图填充 asset.thumb_path（图片 Pillow 缩放至 400px webp；视频 ffmpeg 抽取第 1 秒封面帧再缩放；音频不生成）；生成失败仅记日志、不阻断产物落库 | 前端列表/看板可用轻量缩略图替代原图加载，降低本地带宽与解码开销；缩略图属展示增强而非业务数据，失败降级为无缩略图展示，不影响任务状态机与产物有效性 | app/services/thumbnail.py、app/worker/executor.py |
| D-16 修订 | @引用注入补齐与多角色支持：①批量生成（projects/{id}/generate）与 regenerate 端点补齐注入，与单镜生成行为一致；②多角色按出现顺序填入角色槽位（ref_identity → ref_image → ref_costume），槽位不足返回 422 不静默丢参考；③角色与场景创建/改名入口校验名称不得相同（409），resolve_refs 对历史同名数据兜底返回 422；④@正则对普通文本（邮箱等）的误伤明确不处理 | 单镜/批量/重做三条链路行为不一致会导致生成结果漂移；多角色是 H3 ref2va 多图参考（ref_image_0..5）的核心用法；同名引用无法区分对象必须入口拦截；正则误伤属低频场景，收紧语法反而增加使用成本 | app/services/ref_inject.py、app/api/v1/generate.py、app/api/v1/characters.py、app/api/v1/scenes.py |
| D-18 | 并行调度（修订 D-03）：worker 并发 = 可用 ComfyUI 实例数——Mock 模式/单实例保持串行（与旧行为一致），多实例时任务由实例池加权轮询分发并行执行，单实例同一时刻至多执行一个任务；主循环为唯一取任务者（无取任务竞态），执行线程池消费，实例配置运行时生效每轮重算并发度 | 多 ComfyUI 实例场景下单实例串行浪费算力；实例池已有加权负载均衡与故障转移，调度层只需按实例数放开并发；单实例仍串行，规避 ComfyUI 串行队列与显存溢出问题（D-03 原始理由在单实例下依然成立） | app/worker/scheduler.py、app/core/config.py |
| D-19 | 媒体文件服务：GET /api/assets/{id}/file 返回资产原文件，?thumb=true 且缩略图存在时返回缩略图（缺失回退原文件）；资产对象与镜头 current_assets 透出 url/thumb_url 字段，前端列表/看板优先加载 thumb_url | 产物与缩略图存于本地 media_dir，此前无 HTTP 服务路由导致前端无法展示；按资产 ID 走 API 路由（非 StaticFiles 目录挂载）保留存在性校验与 404 语义；缩略图缺失回退原文件保证展示不中断 | app/api/v1/assets.py、app/api/v1/shots.py、frontend（AssetsView/StoryboardView） |
| D-20 | 道具 @引用注入与锚定图回写（补齐 D-16）：①resolve_refs 新增 Prop 查询，@道具名 → 道具 anchor_asset_id 对应文件；新增 _PROP_SLOTS=[ref_prop]（ref2va 工作流），注入逻辑与角色/场景对齐（按出现顺序填槽、槽位不足 422 不静默丢参考）；道具无锚定图 409 reference_asset_missing；同名兜底扩展为角色/场景/道具三方判定（历史数据防御）；②executor 产物落库后新增 7.0 回写：job.params 含 __prop_id__ / __character_id__ / __scene_id__ 时，将本次首个 keyframe 产物资产回写到 Prop.anchor_asset_id / Character.portrait_asset_id / Scene.baseline_asset_id（此前三类生成端点写入的标记参数均无消费方，生成完成后记录不更新，前端状态标签永远停留未生成） | 剧本解析提示词已要求提取 props 且 parse_executor 按名幂等落库、props.py 改名时同步改写 @引用，但 ref_inject 此前仅查 Character/Scene，镜头提示词中 @道具名会 422 报错、道具锚定图生成了也无法注入参考图——前后端道具链路在注入环节断裂；回写缺口经 executor 全文审查确认（仅消费 __media__ 与 seed），与 D-16 模式一致补齐，保持三类引用与三条生成链路行为统一 | app/services/ref_inject.py、app/worker/executor.py、tests/test_prompt_and_ref.py、tests/test_mock_render.py |

## 3. TBD / 待实测项

以下条目须在首次真实联调时回填/确认；回填前禁止写死（来源：WORKFLOW.md §14、DATABASE.md 末节）：

| 编号 | 事项 | 回填要求 |
| --- | --- | --- |
| T-01 | H3 音轨参数（FPS 已实证为 24） | 2026-10-23 实证：本地架构 CreateVideo 节点 fps=24、length 124–362 帧步长 17（约 5.2–15.1 秒）；音轨参数（采样率/声道）待首次本地生成实测回填 |
| T-02 | `ratio=adaptive` 行为 | 确认各分辨率/画幅下的实际生效行为 |
| T-03 | 模型文件名与本机一致性 | 核对工作流 JSON 中的模型文件名与本机文件。2026-10-23 实证：图像模型 8/8 全部就位（AUD3-P0-01 解除）；H3 本地权重 5 项（minimax_h3_ref2va_pruned_int8_convrot / minimax_h3_fl2va_pruned_int8_convrot / qwen3vl_32b_minimax_h3_int8_convrot / minimax_h3_video_vae_fp16 / minimax_h3_audio_vae_fp32）随 D-10 架构切换新增，待部署到 ≥24GB 显存实例后核对 |
| T-04 | 三图参考一致性评分基线 | 实测后校准 0.85/0.70 双阈值是否需要调整 |
| T-05 | Qwen3-TTS 情感指令映射实测 | 节点类名与输入键已确认（Qwen3Loader/Qwen3CustomVoice/Qwen3VoiceClone，speaker/language/instruct 等，见 meta 与校验输出）；`emotion_map` 情感映射为推荐值（neutral 为空、happy→Happy 等），未经真实联调验证，首次联调时确认后回填。2026-10-23 复证实证：远端已安装 DarioFT/ComfyUI-Qwen3-TTS，Qwen3Loader（必填 attention/precision/repo_id/source）、Qwen3CustomVoice（必填 language/model/seed/speaker/text）、Qwen3VoiceClone（必填 model/seed/text）三类节点均已注册，必填输入键与工作流声明完全一致（AUD3-P0-02 解除）；注意上游 qwen-tts 依赖 transformers==4.57.3，安装曾可能降级现有 transformers 版本。情感映射联调前提已满足，`emotion_map` 推荐值仍待首次真实联调验证后回填 |
| T-06 | Stable Audio 最大时长与采样参数 | 实测单次上限（约 47 秒为经验值） |
| T-07 | ~~T2V/FLF2V 的 `model.*` 动态输入键~~（superseded） | 云端架构实证当日 H3 切换为本地原生节点架构（D-10）；新架构为显式输入键（length/width/height/first_frame/last_frame/ref_images.ref_image_N），已与实例节点定义逐一核对一致；本条目标记 superseded，残留风险转为本地生成实测 |
| T-08 | 服务型数据库迁移时机 | 出现多实例或高并发写入需求时评估迁移方案 |
| T-09 | SSE 鉴权演进 | 引入 Bearer 鉴权时，EventSource 无法附加自定义请求头，前端需改用 fetch + ReadableStream 读取流（服务端接口形态不变，见 API.md 第 12 节） |

## 4. 本期未交付

- GPT-SoVITS / CosyVoice / Edge-TTS 标准工作流（当前 TTS 标准实现为 Qwen3-TTS）
- SFX 音效工作流 `wf_sfx_stable_audio.json`（文档层保留 sfx 枚举与链路定义；SFX 为可选轨道，缺失不阻塞合成，可从素材库选取）

## 5. 冲突与变更记录

记录格式：`日期 | 冲突/事项 | 涉及文档 | 结论 | 状态（open/resolved/superseded）`

| 日期 | 冲突/事项 | 涉及文档 | 结论 | 状态 |
| --- | --- | --- | --- | --- |
| 2026-10-23 | PRD.md M4-05 将生成通道降级标为 P0，但 API.md 无三方通道接口、ARCHITECTURE.md 无降级组件定义，需求与实现定义不匹配 | PRD.md、FRONTEND.md、DEVELOPMENT.md、API.md、ARCHITECTURE.md | 经审计确认后决策保留三方通道：API.md 补 GET/PUT /api/settings/channels 与通道测试接口，ARCHITECTURE.md 补 5.8 节降级组件定义（触发条件、尝试顺序、可追溯性、恢复规则） | resolved |
| 2026-10-23 | 3 个工作流（wf_image_t2i_qwen、wf_image_ref_qwen_edit、wf_bgm_stable_audio）KSampler 残留画布专用键 control_after_generate，与 ComfyUI 0.41.0 KSampler 标准 10 输入及既定种子策略矛盾 | WORKFLOW.md、AGENTS.md、DECISIONS.md | 按深度交叉验证结论删除 3 处残留键行（修复后 validate_workflows 8/8 PASS、交叉验证仅剩已知项）；WORKFLOW.md 增补导出防复发规则；DECISIONS.md 回填 T-03/T-05/T-07 实证标注 | resolved |
| 2026-10-23 | 三份 H3 工作流由云端 Partner Node 架构重写为本地权重原生节点架构，与 PRD.md P0 决策（云端按秒计费）、DECISIONS.md 第 4 节（本地权重 H3 本期不交付）、WORKFLOW.md（时长 4–15 秒制、Image N 语法）全面冲突；且当前 T4 16GB 实例不满足本地运行显存要求 | PRD.md、WORKFLOW.md、API.md、DECISIONS.md、wf_video_h3_*.json | 经审计上报后用户确认走本地架构并计划切换 A10/V100 实例（D-10）；文档按帧数制（length 124–362 步长 17）、分辨率约束（32 倍数、短边 384–768）、<Picture N> 语法同步修订；V100 兼容性未验证（nvfp4 原生支持仅 Blackwell），A10 为验证目标 | resolved |
| 2026-10-23 | 阶段4收尾进度登记：seed_workflows.py 落盘与 8 工作流种子导入完成（修复 glob 误匹配 .meta.json 导致 16 条重复的 bug 后清理重跑）；阶段4.1（202256b）与阶段4.2（f0652d1，15 文件 +1538 行、21/21 测试通过）已提交；T-05 复核发现『远端未装 Qwen3-TTS 节点包』记录过期 | DECISIONS.md、WORKFLOW.md、scripts/seed_workflows.py | 复证实测远端已安装 DarioFT/ComfyUI-Qwen3-TTS，Qwen3Loader/Qwen3CustomVoice/Qwen3VoiceClone 三节点均注册且必填输入键与工作流声明完全一致，T-05 已回填 AUD3-P0-02 解除；docs 目录不在 git 管理下，进度以本节变更记录为准；emotion_map 推荐值仍待首次真实联调验证后回填 T-05 | resolved |
| 2026-10-24 | 阶段5完成：前端 7 页面（项目列表/剧本/分镜看板/资产库/任务中心/设置/合成导出）+ API 封装 9 模块 + 类型定义 6 文件 + Pinia stores + 路由 + 全局布局 + SSE 订阅 + 深色主题设计令牌；npm run build 通过（vue-tsc + vite）；联调冒烟 3/5 通过（GET /api/projects ✅、POST /api/projects ✅、GET /api/jobs ✅；GET /api/workflows 与 GET /api/settings 后端路由未实现，前端封装已就绪待后端补齐）；git 提交完成 | FRONTEND.md、API.md、frontend/src/ | 阶段5 前端页面与真实接口联调已提交；workflows/settings 路由属阶段 3 后端范围但未实现，需后续补齐后端接口再联调 | resolved |
| 2026-10-24 | 工作流资产迁入 backend/data/workflows 后代码审核：修复 validate_workflows.py 四处轻微问题（PASS/FAIL 判定移至 meta 校验之后、必需输出改由 meta trigger.target 推断且文件名推断仅作回退、meta 路径改用 splitext、with open 管理文件句柄）；修复 seed_workflows.py 版本虚增（内容指纹比对，未变化跳过）；遗留两项设计现状：find_output_nodes 同名 $output 静默覆盖无告警、prune_unused_slots.py 仅以 re.search 识别标题首个可选媒体标记 | scripts/validate_workflows.py、scripts/seed_workflows.py、app/services/workflow_inject.py、scripts/prune_unused_slots.py | 修复后 9/9 工作流校验 PASS、种子脚本二次运行全部未变化（版本号不再增长）；遗留项确认为可接受设计现状（当前工作流无同名输出，注入阶段会对缺失必填参数先抛错），记录备查 | resolved |
| 2026-10-24 | docs 全局逻辑检查发现进度跟踪与实际脱节：AGENTS.md 仍称无既有源代码，IMPLEMENTATION_PLAN.md 阶段 1-5 状态停留在开发中/待开始，AUD3-P0-02 与 AUD3-P2-01 仍列为阻塞但 DECISIONS.md 已记录解除，且存在 /home/ubuntu/docs 路径笔误与工作流 8/9 份口径不一致 | AGENTS.md、IMPLEMENTATION_PLAN.md、DECISIONS.md | 按 git 提交证据回填：阶段 1-3 已验证、阶段 4 待验证、阶段 5 开发中；修正 docs 权威目录为项目内 docs/；阶段 4 范围与进度表改为 9 份工作流；AUD3-P0-02/P2-01 标记已解除；保留基线节 8 份表述作为 2026-10-23 历史快照并补 2026-10-24 更新说明 | resolved |
| 2026-10-24 | 阶段5+6完成：阶段5 前端7页面+API封装+SSE+布局构建通过联调5/5通过；后端补齐 workflows/settings 路由；阶段6 合成导出后端代码已在阶段4.2实现（compose服务+执行器+API），前端合成导出页已在阶段5实现，21/21测试通过 | FRONTEND.md、API.md、backend/app/ | 阶段5前端页面与真实接口联调已提交；后端workflows/settings路由补齐已提交；阶段6合成导出与字幕配音后端代码复用阶段4.2实现，无需新增开发；health测试修复适配新端点 | resolved |
| 2026-10-24 | 落实代码审核遗留两项设计现状的实际修复：workflow_inject.py 的 parse_workflow 与 find_output_nodes 增加同名 $output 冲突检测（解析期报错、执行期抛 ValueError，覆盖 worker 执行器产物提取链路）；validate_workflows.py 同步增加重复输出校验；prune_unused_slots.py 由 re.search 首标记改为 re.finditer 全标记识别（所有媒体标记均未注入才删除节点，避免多标记 Load 节点误删） | app/services/workflow_inject.py、scripts/validate_workflows.py、scripts/prune_unused_slots.py | 回归：21/21 测试通过、9/9 工作流校验 PASS、种子脚本二次运行幂等（版本不再虚增）；针对性验证：重复输出在解析与执行两条链路均拦截、prune 部分提供保留/全部未提供删除行为正确 | resolved |
