# DATABASE —— 数据模型与存储设计

> 本文档是表结构的唯一权威定义。建表与迁移以此为准；字段在接口层的暴露口径见 API.md。状态机取值含义见 ARCHITECTURE.md。

## 1. 层级划分硬规则（P0）

资产与分镜产物分属两个层级，不得混用：

| 层级 | 内容 | 归属 | 跨集行为 |
| --- | --- | --- | --- |
| 资产层 | 角色定妆图、场景基准图、道具锚定图 | 项目级 | 公用，全项目唯一，跨集复用同一份 |
| 分镜层 | 镜头、分镜提示词、分镜图、分镜视频、配音 | 集级 | 独立，每集各自持有，修改互不影响 |

推导规则：
1. 同一角色或场景在第 1 集与第 5 集出现时，锚定图只有一份；两集的分镜图、分镜视频各自独立生成与存储
2. 修改第 5 集的某个镜头提示词，只影响该镜头自身的分镜图与视频，不波及第 1 集引用同一资产的其它镜头

## 2. 核心表

### script_version（剧本版本）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| project_id | string | 所属项目 |
| version | int | 项目内递增版本号 |
| content_snapshot | text | 不可变剧本文本快照 |
| content_hash | string | 内容哈希 |
| created_at | datetime | 创建时间 |

### script_estimate（剧本评估）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| script_version_id | string | 对应剧本版本 |
| algorithm_version | string | 估算算法/提示词版本 |
| result | json | 时长区间、集数、每集镜头数、估算假设 |
| user_confirmed | bool | 用户是否确认制作范围 |
| created_at | datetime | 创建时间 |

### project（项目）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| name | string | 项目名 |
| creation_type | string | 创作类型（novel / script），默认 novel |
| description | text | 项目描述，≤500 字符，可为空 |
| aspect_ratio | string | 画幅比（9:16 / 16:9 / 1:1） |
| status | string | 项目状态（draft / active / archived） |
| settings | json | 项目级配置（可覆盖一致性阈值等系统默认） |
| episode_plan | json | 分集规划数组，每项含 episode、goal、rhythm_curve、cliffhanger_type、hook_to_next、locked；重新解析时仅覆盖 locked=false 的集 |
| estimated_at | datetime | 最近一次剧本体量评估时间，未评估为空 |
| estimated_text_hash | string | 兼容字段：最近一次评估的剧本文本哈希 |
| current_script_version_id | string | 当前生效的剧本版本 ID |
| current_parse_version_id | string | 当前生效的解析版本 ID |
| deleted_at | datetime | 软删除时间，可为空 |
| created_at / updated_at | datetime | 时间戳 |

### character（角色）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| project_id | string | 外键 |
| name | string | 角色名，项目内唯一，用于 @名称 引用 |
| description | text | 角色简介 |
| traits | text | 性格特点 |
| appearance | text | 外在形象描述，直接用于文生图提示词 |
| voice_id | string | 音色 ID，指向已注册音色，用于 TTS |
| portrait_asset_id | string | 当前生效的角色定妆图资产 ID（单值，不做三视图） |
| created_at / updated_at | datetime | 时间戳 |

### scene（场景）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| project_id | string | 外键 |
| episode | int | 首次出现集数，从 1 开始，默认 1；全部出现集数由 scene_episode 表表达 |
| seq | int | 场景序号，项目内唯一，不按集重置，删除后保留空号 |
| title | string | 场景名 |
| description | text | 场景描述 |
| time_note | string | 时间线说明（如『距上集结尾三天后』），可为空 |
| baseline_asset_id | string | 当前生效的场景基准图资产 ID |
| created_at / updated_at | datetime | 时间戳 |

### scene_episode（场景-集关联）

同一地点在多集出现时 scene 只保留一条记录，通过本表表达出现在哪些集，避免基准图重复生成导致一致性下降。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| scene_id | string | 场景 ID |
| episode | int | 该场景出现的集数 |
| seq_in_episode | int | 该场景在该集内的出场顺序 |

约束：同一 `scene_id` 与 `episode` 组合不可重复。

### prop（道具）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| project_id | string | 外键 |
| name | string | 道具名，项目内唯一，用于 @名称 引用 |
| description | text | 道具用途与剧情意义 |
| appearance | text | 外观描述，直接用于文生图提示词 |
| is_key_prop | bool | 是否为关键道具，默认 false；可由系统建议、用户确认 |
| anchor_required | bool | 是否对引用镜头强制要求锚定图，默认 false；仅关键道具且显式启用时为 true |
| anchor_asset_id | string | 当前生效的道具锚定图资产 ID，可为空 |
| created_at / updated_at | datetime | 时间戳 |

规则：道具按项目维度建模、不按集拆分；普通道具仅依赖 appearance 文字描述，缺少可选锚定图不得阻塞生成。

### shot（镜头）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| scene_id | string | 外键 |
| episode | int | 所属集数，从 1 开始；同一场景在不同集各自拥有独立镜头集合 |
| character_id | string | 主说话角色 ID，可选；用于配音取 character.voice_id 及生成时引用定妆图 |
| seq | int | 镜头序号，同一场景同一集内唯一，删除后保留空号不复用；界面镜号即 seq |
| dialogue | text | 台词全文，多说话人时按出现顺序拼接，用于展示与字幕 |
| dialogue_segments | json | 多人对白权威结构，每段含 `speaker_id`（权威）、`speaker`（兼容名称）、`text`、`emotion`、`order`、可选 `start_time/end_time`；单说话人可为空；入库前将可解析的 speaker 名称归一化为 speaker_id |
| shot_kind | string | 必填枚举 dialogue / narration / visual / transition；缺省值仅由解析适配层按规则补齐并写入 warning，不允许数据库无值 |
| image_prompt | text | 分镜图提示词，纯画面描述；不得混入 H3 视频格式的时间码与镜号 |
| video_prompt | text | 视频提示词，按 H3 多模态格式编写；为空时由系统以 image_prompt 加镜头元信息自动拼装 |
| shot_type | string | 景别：远景/全景/中景/中近景/近景/特写/大特写 |
| camera_angle | string | 机位与角度：平视/俯拍/仰拍/侧面/正面/过肩等 |
| camera_movement | string | 运镜：固定/推镜/拉镜/摇镜/移镜/跟拍 |
| transition | string | 剪辑衔接：切入点/反应镜头/动作匹配/悬念停顿 |
| shot_purpose | string | 分镜目的 |
| emotion | string | 情感标签：neutral / happy / sad / angry / fearful，用于 TTS 情感控制 |
| soundscape | text | 环境声与动作音效描述 |
| bgm | text | 背景音乐描述，含情绪与变化节点 |
| duration | int | 预估时长（秒），必须为正整数 |
| status | string | pending / generating / review / done / redo；pending 可附带 blocked_reason 子状态 |
| actual_duration | number | 当前有效对白/旁白音频实际时长（秒）；优先 H3 VA 音频时长，TTS 补救后取补救音频时长；来源失效/替换时同步更新 |
| video_actual_duration | number | 当前有效 H3 VA/视频产物实际时长（秒），由媒体探测写入 |
| audio_source | string | h3_va / tts_fallback / none；当前有效对白/旁白音频来源 |
| audio_check_status | string | passed / review / failed / not_applicable；音轨文件与媒体有效性检查结果 |
| dialogue_check_status | string | passed / review / failed / not_applicable；对白与台词对应性检查结果 |
| av_sync_delta | number | 音频与视频实际时长差（秒），用于音画容差判定；缺少任一时长时为空 |
| time_jump | bool | 是否发生明显时间跳跃，默认 false |
| requires_prev_frame | bool | 是否要求注入前镜末帧，默认 false；为 true 但前镜有效末帧缺失则进入审核/阻止生成 |
| start_state | json | 结构化起始状态（六项枚举，见下表），用于视频续接 |
| end_state | json | 结构化结束状态，与 start_state 同构，供下一镜头衔接 |
| last_frame_asset_id | string | 视频末帧自动抽取生成的资产 ID，视频成功后回写，供下一镜头作 ref_prev_frame |
| beats | json | 动作节拍数组，每项含 action、start_pose、end_pose、duration；单镜头最多 3 个节拍 |
| emotion_curve | json | 情绪变化曲线，含 from、trigger、turn_at、to、expression；`emotion` 取 to 的值 |
| created_at / updated_at | datetime | 时间戳 |

#### start_state / end_state 六项取值域（P0，只接受枚举，不接受自由文本）

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| position | 画面左 / 画面中 / 画面右 / 前景 / 背景 / 画外 | 人物在构图中的位置，多人时按主说话人记 |
| pose | 站立 / 坐下 / 躺卧 / 行走 / 奔跑 / 弯腰 / 蹲下 / 转身 / 手持物 / 其他 | 该时刻的静态姿态，不描述过程 |
| gaze | 正前 / 左侧 / 右侧 / 上方 / 下方 / 对视 / 背对 / 闭眼 | 视线朝向 |
| prop_state | 无道具 / 手持 / 放置 / 传递 / 使用中 / 掉落 | 关键道具在该时刻的状态 |
| emotion | neutral / happy / sad / angry / fearful | 与 shot.emotion 取值域完全一致；映射不了时落 neutral 并写入 parse_warnings，本项不使用『其他』 |
| camera | 平视 / 俯拍 / 仰拍 / 侧面 / 正面 / 过肩 / 其他 | 与 camera_angle 保持一致，不得矛盾 |

映射与校验：
1. 解析阶段由语言模型把自然语言映射到上述取值，映射不了的一律落『其他』，不得自造新值；emotion 项除外——其为封闭枚举，映射不了时落 neutral 并写入 parse_warnings
2. 出现『其他』的镜头在分镜审核界面统一标黄，提示人工确认或改写后重新映射
3. 相邻镜头结构校验按枚举值检查并给出提示；硬切、时间跳跃、视角切换需结合 transition、shot_kind、time_jump 与 requires_prev_frame 判断；『其他』与任何值比对均记为待人工确认，不计为冲突
4. `emotion` 必须与 `shot.emotion` 一致，两者同为封闭枚举，不一致视为数据错误，校验直接拒绝；`camera` 与 `camera_angle` 的一致性仅作提示：`camera_angle` 为自由文本，当其恰为上表枚举值之一且与本项不一致时记 warning 提示人工确认，不拒绝

### asset（资产）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| project_id | string | 外键 |
| asset_type | string | character / scene / prop / keyframe / clip / audio / last_frame |
| name | string | 资产名 |
| version | int | 版本号 |
| parent_id | string | 上一版本 ID |
| file_path | string | 存储路径 |
| thumb_path | string | 缩略图路径 |
| source_job_id | string | 来源任务 |
| ref_count | int | 引用计数（仅展示/加速，非删除依据） |
| locked | bool | 是否锁定 |
| validity | string | current / stale / superseded / archived |
| input_snapshot | json | 产物输入版本与参数快照 |

### asset_ref（镜头-资产绑定）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| shot_id | string | 镜头 ID |
| asset_id | string | 资产 ID |
| role | string | identity / costume / scene / prop / reference / first_frame / end_frame / prev_frame / dialogue / sfx / bgm |
| source_version | int | 引用资产版本 |
| created_at | datetime | 绑定时间 |

说明：本表只表达引用关系，不用于判断产物是否具备（判定规则见 ARCHITECTURE.md 4.2 节）。

### workflow（工作流）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| name | string | 名称 |
| workflow_type | string | image / video / tts / bgm / sfx；video 可声明同时输出音频 |
| description | text | 用途描述，用于前端选择时展示 |
| trigger | json | 触发条件（target / required_params / priority，见 WORKFLOW.md） |
| capabilities | json | 当前版本支持的首尾帧、参考图槽位、时长/分辨率/FPS、音频和文件格式等能力声明 |
| version | int | 版本号 |
| definition | json | 工作流 JSON |
| parsed_schema | json | 解析出的参数与输出 |
| is_default | bool | 是否默认 |
| created_at / updated_at | datetime | 时间戳 |

### job（任务）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| project_id | string | 所属项目（必填） |
| shot_id | string | 关联镜头（可选，项目级任务如剧本解析为空） |
| job_type | string | parse / generate_keyframe / generate_clip / generate_audio / generate_bgm / generate_sfx / compose / export |
| workflow_id / workflow_version | string / int | 所用工作流及版本 |
| params | json | 实际参数 |
| status | string | 任务状态（见 ARCHITECTURE.md 3.3） |
| prompt_id | string | 远端任务 ID |
| attempts | int | 重试次数 |
| error_code | string | 失败码 |
| error_msg | text | 失败详情 |
| result | json | 产物信息 |
| request_key | string | 唯一业务请求 ID，用于提交对账/去重 |
| submission_state | string | not_submitted / submitting / submitted / submission_unknown |
| input_snapshot | json | 输入资产版本、提示词版本、工作流版本、模型参数、前镜尾帧版本等快照 |
| event_seq | int | 事件序号，用于断线补拉 |
| started_at / finished_at | datetime | 时间戳 |

### parse_version（解析版本）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| project_id | string | 所属项目 |
| script_version_id | string | 本次解析使用的剧本版本 |
| version | int | 项目内递增版本号 |
| status | string | candidate / current / superseded / failed |
| diff_summary | json | 新增、删除、修改的场景/镜头与受影响任务/资产摘要 |
| total_episodes | int | 可空。script-agent 口径锁定：首批解析成功后锁定的总集数，后续增量解析不可超出 |
| script_hash | string(64) | 可空。口径对应的剧本文本哈希；剧本变化则口径失效，需重新全量分析 |
| created_at / confirmed_at | datetime | 创建/确认时间 |

### dependency_ref（依赖关系，权威来源）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| source_type / source_id / source_version | string / string / int | 上游依赖对象及版本 |
| target_type / target_id | string / string | 下游对象 |
| dependency_role | string | identity / costume / scene / prop / prompt / workflow / prev_frame / audio / compose 等 |
| validity | string | current / stale |

### job_event（事件 outbox）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| event_seq | int | 单调递增序号，主键或唯一键 |
| job_id | string | 关联任务 |
| event_type | string | 状态变化/进度/产物事件类型 |
| payload | json | 事件数据，不含密钥 |
| created_at | datetime | 事件创建时间 |
| published_at | datetime | 推送完成时间，可为空 |

### upload_record（上传记录）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| comfy_filename | string | 上传后文件名 |
| local_path | string | 本地路径 |
| created_at | datetime | 时间 |

### composition（合成产物）

每次调用 compose 接口创建一条记录。合成必须引用当前有效的镜头视频与配音版本，并保存 source_snapshot 用于依赖失效判定。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| project_id | string | 所属项目 |
| episode | int | 合成的集数，null 表示全部集 |
| status | string | composing / succeeded / failed |
| stale | bool | 依赖是否失效；任一镜头视频或音频产物失效时置 true，不可用于导出 |
| file_path | string | 合成产物路径（内部预览，不直接作为导出下载地址） |
| duration | number | 合成总时长（秒） |
| srt_path | string | 字幕文件路径，sidecar 模式时存在 |
| source_snapshot | json | 依赖快照：多集合成时按 episode 分组，每组含该集每个镜头的 video asset_id/version、audio asset_id/version、实际时长、时间轴起始时间 |
| warnings | json | 合成警告数组，如字幕估算时间标注、BGM 循环/淡出记录 |
| created_at | datetime | 创建时间 |

### export（导出记录）

每次调用 export 接口创建一条记录。导出只负责交付封装，不改变 composition 本身的状态。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 主键 |
| project_id | string | 所属项目 |
| composition_id | string | 来源合成版本 ID |
| format | string | 导出格式，枚举 mp4 |
| delivery_profile | string | standard / high_quality |
| file_name | string | 导出文件名 |
| size | int | 文件大小（字节） |
| status | string | exporting / succeeded / failed |
| created_at | datetime | 创建时间 |

## 3. 存储设计

数据库使用 SQLite 并开启 WAL 模式，同时设置 `busy_timeout=5000`（毫秒）、`synchronous=NORMAL`。事务仅做短时读写，所有网络与媒体处理在事务外执行。写事务最多等待 5 秒，单次请求内重试不超过 3 次，仍失败返回 503 `database_busy`。

媒体文件落本地目录，按项目与类型分桶，静态文件由后端直接托管。

建议索引：
- job 表：`(project_id, status)`、`(prompt_id)`、`(status, job_type)`、`(created_at)`
- asset 表：`(project_id, asset_type, validity)`
- asset_ref 表：`(shot_id)`
- dependency_ref 表：`(target_type, target_id)`
- shot 表：`(scene_id, episode)`，支撑分镜看板按场景按集分批加载镜头列表（FRONTEND.md 第 7 节）与场景内镜头排序查询
- composition 表：`(project_id, episode, stale)`，支撑合成记录列表按集筛选与有效合成版本判定（current_stage 第 10/11 条）；有效性语义由 stale 字段承担（依赖失效置 true，不可导出），本表无 validity 字段

必须落位的组合唯一约束（解析与排序正确性依赖，首个迁移脚本即须建立）：
- scene_episode 表：`UNIQUE(scene_id, episode)`，同一场景同一集仅一条出场记录
- shot 表：`UNIQUE(scene_id, episode, seq)`，同一场景同一集内镜头序号唯一（删除保留空号不复用，故允许空号对应位置永久空缺）
- script_version 表：`UNIQUE(project_id, version)`
- parse_version 表：`UNIQUE(project_id, version)`

本期定位为单机/小团队规模；如需多实例或高并发写入，应迁移到服务型数据库（见 DECISIONS.md TBD）。
