# WORKFLOW —— 工作流技术契约（唯一权威源）

> 本文档定义 ComfyKit 工作流的格式、标记、参数、匹配与 H3 生成链路的全部技术约束（P0/P1）。接口字段见 API.md，生成业务流程见 PRD.md M4/M5，提示词业务规则见 PROMPTS.md。随文交付的标准工作流文件位于 `backend/data/workflows/` 目录（sfx 音效工作流本期不交付，详见第 13 节清单）。

## 1. 格式要求（P0）

仅接受 ComfyUI 的 API 格式（导出时选择 API Format）。顶层为节点 ID 到节点对象的映射，每个节点包含 class_type 与 inputs。画布存档格式（含 nodes 与 links 字段）直接拒绝，并提示用户改从 API Format 导出。

从画布导出 API Format 后，必须剔除画布专用残留键（如 KSampler 中的 control_after_generate、widgets_values 等）：此类键只在画布 UI 状态中存在，不属于 ComfyUI 服务端节点的合法输入，混入会导致与节点标准输入集及既定参数策略（如种子策略）矛盾，且运行时行为未定义。2026-10-23 实证：wf_image_t2i_qwen / wf_image_ref_qwen_edit / wf_bgm_stable_audio 三份工作流曾残留 control_after_generate 并修复，凡新增或更新工作流文件时须复核此条。

## 2. 标记语法（P0）

标记写在节点的 `_meta.title` 中，不是写在 inputs 里。

| 标记 | 含义 |
| --- | --- |
| $param | 基础参数，注入同名输入字段 |
| $param.field | 参数映射到指定字段 |
| $param! | 必填参数，缺失即报错 |
| $~param | 媒体参数，先上传再注入文件名 |
| $~param! | 必填媒体 |
| $output.name | 标记输出变量名 |

补充规则：
- 同一节点可写多个参数，以逗号分隔
- 不以 $ 开头的片段视为展示文本，不参与解析
- 字段映射：`$param` 默认映射到同名输入字段；字段名与参数名不同必须显式写 `$param.field`。例如 `CLIPTextEncode` 用 `$prompt.text!`，`TextEncodeQwenImageEditPlus` 用 `$prompt.prompt!`，`PrimitiveStringMultiline`/`PrimitiveInt` 用 `$prompt.value!` / `$seed.value`
- 媒体参数写成 `$param.~field`，例如 `LoadImage` 节点写 `$ref_identity.~image`。不使用未指定字段的 `$~param`，避免映射到不存在的同名字段
- 标记不得指向带点号的输入键（如动态下拉子项 `model.prompt`）或已被连线占用的输入；此类字段改由 Primitive 节点经连线驱动并在 Primitive 节点上打标记，或由执行器按 `capabilities` 声明的 patch key 直接改写
- 标记指向不存在字段、指向连线输入、同名参数重复绑定、`$~` 用于非媒体节点，均在校验中报错或告警，并定位到节点 ID 与字段名
- 可选媒体槽位未注入时的处理见第 10.3 节槽位剔除
- 参数名规范化：转小写，非字母数字字符统一为下划线
- P0 工作流必须显式声明 `$output.*` 输出标记；缺少输出标记时拒绝保存并返回 `workflow_marker_missing`
- 如未来提供自动推断兼容模式，必须由用户显式开启、在界面标记为兼容模式，并记录在工作流版本中；不得作为默认行为

## 3. 统一参数约定（P0）

所有工作流必须遵守以下参数命名，业务层只使用这套名字：

| 参数名 | 语义 | 标记写法 | 必填 |
| --- | --- | --- | --- |
| prompt | 正向提示词 | $prompt.{字段}!（字段随节点：text / prompt / value） | 是 |
| negative | 负向提示词 | $negative | 否 |
| width | 宽度 | $width（字段同名）；Primitive 节点写 $width.value | 视工作流 |
| height | 高度 | $height（字段同名）；Primitive 节点写 $height.value | 视工作流 |
| seed | 随机种子 | $seed | 否 |
| ref_identity | 人物身份参考图，优先保持面部与五官 | $ref_identity.~image | 否 |
| ref_costume | 服装与全身比例参考图 | $ref_costume.~image | 否 |
| ref_scene | 场景与光线参考图 | $ref_scene.~image | 否 |
| ref_prop | 关键道具参考图，约束道具形制、颜色与持握关系 | $ref_prop.~image | 否 |
| ref_prev_frame | 上一镜头末帧，用于画面连续衔接；首镜或前镜末帧缺失时不注入 | $ref_prev_frame.~image | 否 |
| ref_image | 通用参考图。出图工作流中与 ref_identity/ref_costume/ref_scene 互斥；H3 视频工作流中表示『当前镜头分镜图』，与其余五个槽位并存 | $ref_image.~image | 否 |
| first_frame | 首帧 | $first_frame.~image | 否 |
| end_frame | 尾帧 | $end_frame.~image | 否 |
| frames | 帧数 | $frames | 否 |
| tts_text | 配音文本 | $tts_text! | 是（TTS） |
| length | 视频目标帧数（H3 本地节点；124–362，步长 17，offset 5，fps=24） | $length.value | 否 |

## 4. 输出约定（P0）

输出标记按 `workflow_type` 校验，不能把所有音频输出都设为一个工作流的必需项。

| 产物类型 / 工作流 | 输出标记 | 约束 |
| --- | --- | --- |
| 图像 | `$output.cover` | 图像工作流必需输出 |
| 视频 | `$output.clip` | 视频工作流必需输出；H3 VA 的音频可内嵌于视频文件，也可按能力声明提供独立输出 |
| 配音 TTS 补救 | `$output.voice` | TTS 工作流必需输出；仅在 H3 VA 音频缺失/无效、对白检查失败或人工要求重配时调用 |
| 背景音乐 | `$output.bgm` | BGM 生成工作流必需输出；素材库选取不经过生成工作流 |
| 音效 | `$output.sfx` | SFX 生成工作流必需输出；素材库选取不经过生成工作流 |

## 5. 参数对齐规则（P0）

| 参数 | 规则 |
| --- | --- |
| 宽高 | 必须同时绑定才可改写；只绑一侧时该侧以工作流内置值为准，并在 validate 返回 warning 级 issue，不静默丢弃 |
| 帧数 | 按步长 × n + offset 对齐，反算时长后需正向校验一致；H3 本地节点绑定 `length`（124–362、步长 17、offset 5、fps=24，约 5.2–15.1 秒，见第 8 节） |
| 帧率 | 只读，不允许改写 |
| 种子 | random 策略下同一次 job 内所有种子项共用一个值，不同 job 之间互不共享；fixed 策略下由 params.seed 指定 |
| 负向提示词 | 追加到节点原有字面值后，未绑定则丢弃 |

### 5.1 多参考图规则

出图工作流最多可绑定三个职责明确的参考图槽位，视频工作流最多可绑定六个图像参考槽位（五个职责槽位含关键道具与上一镜头末帧，外加当前镜头分镜图 `ref_image`），每个槽位承担一项约束，互不替代。出图三槽位：

| 槽位 | 负责约束 | 缺失时的处理 |
| --- | --- | --- |
| ref_identity | 人物身份、脸型、五官、发型 | 该槽位不注入，仅按其余槽位与提示词生成 |
| ref_costume | 服装颜色、配饰、全身比例 | 同上 |
| ref_scene | 场景与光线 | 同上 |

### 5.2 分辨率来源

项目不直接存储宽高，按 `project.aspect_ratio` 映射为基准分辨率，再按工作流绑定情况决定是否注入：

| aspect_ratio | 基准宽 × 高 |
| --- | --- |
| 9:16 | 720 × 1280 |
| 16:9 | 1280 × 720 |
| 1:1 | 1024 × 1024 |

注入规则：工作流同时绑定 width 与 height 时按上表注入；仅绑定一侧或均未绑定时不注入，以工作流内置尺寸为准。用户可在生成请求的 `params.width` / `params.height` 中显式覆盖，覆盖值同样需两侧齐全才生效。

H3 本地节点（MiniMaxH3ReferenceToVideo / MiniMaxH3ImageToVideo）绑定 width 与 height，分辨率须满足 `capabilities.resolution_constraints`（32 的倍数、短边 384–768、长边 ≤1344），执行器按下表注入：

| project.aspect_ratio | H3 width × height |
| --- | --- |
| 9:16 | 736 × 1312 |
| 16:9 | 1312 × 736 |
| 1:1 | 768 × 768 |

不满足分辨率约束（32 的倍数、短边 384–768、长边 ≤1344）的请求提交前校验失败。

## 6. 修改边界（P0）

运行时参数（prompt、seed、尺寸、参考图）可注入；结构参数（checkpoint、LoRA、采样器、步数）属于工作流本身，必须回 ComfyUI 修改后重新导出并重新导入。

## 7. 工作流匹配规则（P0）

系统根据生成请求自动匹配最合适的工作流，同时支持手动覆盖。

### 7.1 触发条件（trigger）

每个工作流上传时需填写 `trigger` 字段，JSON 格式：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| target | string | 是 | 对应生成接口的 target 值：keyframe / clip / audio / bgm / sfx |
| required_params | string[] | 否 | 该工作流必需的参数名列表，如 ["first_frame", "end_frame"] |
| priority | int | 否 | 优先级，数值越大越优先，默认 0 |

### 7.2 target 与 workflow_type 的固定映射

`target` 表示业务动作，`workflow_type` 表示工作流的能力类别，两者为固定的一对一映射，不允许交叉：

| target | workflow_type | 对应业务 |
| --- | --- | --- |
| keyframe | image | 生成角色定妆图、场景基准图、分镜图 |
| clip | video | 逐镜生成视频片段 |
| audio | tts | TTS 补救配音；H3 VA 主音频由 `target=clip` 的 video 工作流产出 |
| bgm | bgm | 背景音乐生成 |
| sfx | sfx | 环境声与动作音效生成 |

按 `target` 过滤工作流，等价于同时按上表映射出的 `workflow_type` 过滤；不在对应关系内的工作流不得被选中。

### 7.3 枚举值域隔离（P0）

`asset_type`、`workflow_type`、`job_type` 三套枚举虽然存在同名取值，但语义完全独立，禁止交叉赋值或在同一字典中混用：

| 枚举 | 取值 | 语义 |
| --- | --- | --- |
| asset_type | character / scene / prop / keyframe / clip / audio / last_frame | 资产产物的种类 |
| workflow_type | image / video / tts / bgm / sfx | 工作流的能力类别；`video` 可通过 `capabilities` 声明 H3 VA 音频输出能力 |
| job_type | parse / generate_keyframe / generate_clip / generate_audio / generate_bgm / generate_sfx / compose / export | 任务的业务动作 |

特别注意：`asset_type=keyframe` 指一张已生成的分镜图资产，`workflow_type=image` 指一个出图工作流，二者含义不同，不可互换。

### 7.4 匹配逻辑

1. 按 `target` 与 `workflow_type` 固定映射过滤工作流
2. 对 `required_params` 逐项硬校验：参数必须存在、类型正确、可绑定且在当前生成模式下有有效值；任一必需参数不满足，该工作流即不具备候选资格
3. 仅在全部必需参数通过的候选中，按 `priority` 降序排列
4. 优先级相同时，按 `is_default`（默认优先）排列；`version` 为工作流自身版本（编辑定义时自增），不参与跨工作流优先级排序
5. 无合格候选时返回 `no_matching_workflow`，列出不匹配参数及原因；不得以匹配数量代替必需参数校验
6. 工作流每次修改生成不可变版本；任务启动时固定引用工作流版本，后续修改不得影响已排队/运行任务

### 7.5 手动覆盖

生成请求中传 `workflow_id` 时，跳过自动匹配，直接使用指定工作流。前端在生成确认弹窗中展示自动匹配结果，并提供「切换工作流」入口供手动选择。

### 7.6 匹配示例

| 场景 | target | 请求 params | 匹配到的工作流 trigger |
| --- | --- | --- | --- |
| 文生图 | keyframe | {prompt, width, height} | target=keyframe, required_params=[] |
| 图生图（参考图） | keyframe | {prompt, ref_identity} | target=keyframe, required_params=[ref_identity] |
| 图生视频（H3 R2V，分镜图作参考） | clip | {prompt, ref_image} | target=clip, required_params=[ref_image] |
| 首尾帧生视频（H3 FLF2V） | clip | {prompt, first_frame, end_frame} | target=clip, required_params=[first_frame]（end_frame 可选） |
| 多图参考生视频 | clip | {prompt, ref_image, ref_identity} | target=clip, required_params=[ref_image, ref_identity] |
| 语音合成 | audio | {tts_text} | target=audio, required_params=[tts_text] |

### 7.7 固定映射（不参与自动匹配）

| 流程节点 | target | workflow_type | 说明 |
| --- | --- | --- | --- |
| 剧本解析 | — | — | 由独立语言模型适配层处理，使用系统设置中的语言模型配置，不经过 ComfyUI 工作流 |
| 生成角色定妆图 | keyframe | image | 文生图，无参考图输入 |
| 生成场景基准图 | keyframe | image | 文生图，无参考图输入 |
| 生成分镜图 | keyframe | image | 图生图，携带已锁定的角色定妆图和场景基准图作为参考图；前置约束：公共资产须通过人工审核（否则 409 `assets_review_pending`），当前分镜图已锁定时禁止重绘覆盖（否则 409 `asset_locked`，需先解锁） |
| 生成视频 | clip | video | 多图参考生视频，携带角色/场景/道具资产图作为参考，使用 MinimaxH3 提示词技能 |
| 检查音频/对白 | — | — | 检查 H3 VA 同次生成音轨；仅检查失败时条件触发 TTS 补救 |
| TTS 补救配音 | audio | tts | 仅对缺失、无效或审核不通过的对白/旁白音频生成补救音轨 |

## 8. H3 VA 音视频一体生成与校验契约（P0）

本系统默认通过 ComfyUI 中配置的 H3 VA 工作流逐镜生成音视频。H3 VA 是主生产链路，独立 TTS 是条件式补救能力，不是每个镜头的固定后置步骤。

### 8.1 生成前时长规划

- 单次 H3 VA 生成按帧数制规划：fps=24，`length` 取 124–362、步长 17、offset 5（约 5.2–15.1 秒）；实际时长必须以当前工作流的帧数规则和帧率为准
- 本期标准 H3 工作流为帧数型：`capabilities` 声明 `fps`（24）、`frame_count_param`（`length`）、`frame_count_step`（17）、`frame_count_offset`（5）与 `frame_count_range`（124–362）；根据 `shot.duration` 计算候选帧数（length = 17 × n + 5），反算预计时长并做正向校验后写入 `$length`
- 低于帧数下限（约 5.2 秒）时编译期硬阻断，接口返回 `422 prompt_validation_failed`，阻断项 code 为 `duration_below_workflow_min`（带 duration / frames / min_frames 字段），提示拆镜或延长时长
- 高于帧数上限（约 15.1 秒）时截断到最大帧数，记 `duration_clamped_to_max` 告警后继续提交，不阻断
- 实际 fps 与时长以生成后媒体探测为准
- `shot.duration` 是生成目标/参考时长，不代表模型一定生成该精确时长；实际生成结果以媒体探测为准

### 8.2 生成后检查顺序

1. 检查视频文件存在、可解码、画幅/FPS/实际时长有效，并保存视频资产
2. 按工作流 `capabilities.audio` 检查音轨：若音频内嵌于 MP4，使用媒体探测确认音轨并可按需提取为独立 `audio` 资产；若工作流声明独立音频输出，则验证该输出文件。未声明音频能力的工作流不得假定一定有音轨
3. 对 `dialogue` / `narration` 镜头，检查音频是否有效、对白是否与当前 `dialogue` / `dialogue_segments` 对应、音频时长及音画同步情况。自动检查无法可靠判断语义或口型时，必须标记为待人工确认，不得把『存在音轨』等同于『对白合格』
4. 若 H3 音频缺失、文件无效、对白缺失/明显不匹配，或人工审核不通过，且该镜头按 `shot_kind` 需要配音，则创建独立 TTS 补救任务。补救产物需记录 `audio_source=tts_fallback`、补救原因与来源 job；正常 H3 音频记录 `audio_source=h3_va`
5. 对 `visual` / `transition` 镜头，按策略允许无对白音频；不得仅因没有对白而创建 TTS。环境声或模型生成的其他声音可作为可选轨道保留
6. 所有路径均记录 `video_duration`、`audio_duration`（如有）、`audio_source`、`audio_check_status`、`dialogue_check_status`、`av_sync_delta` 与检查警告。音画差值仍按合成容差与审核闸口处理（PRD.md M7 节）

### 8.3 TTS 补救规则

- TTS 仅补救对白/旁白音频，不自动替换 H3 视频；若问题是人物口型、动作、画面或音画表演不匹配，应提示用户重生成 H3 VA 镜头或人工审核
- 用户可显式选择『重新生成 H3 音视频』或『仅重新生成 TTS 补救音频』。重新生成 H3 产物后，旧的 TTS 补救音频默认标记为待复核/过期。重生成 H3 VA 后若音轨恢复有效，actual_duration 更新为 H3 音频时长，旧 TTS 产物标记 superseded，引用旧 actual_duration 的 composition 标记 stale
- TTS 补救音频按 `dialogue_segments[].speaker_id` 分段生成；多人对白映射失败时进入审核，不允许静默使用错误音色

### 8.4 工作流能力声明（H3）

视频工作流 `capabilities` 至少需声明 `audio`（是否预期产出音频）、`audio_output_mode`（`embedded` / `separate` / `none`）、`fps`、`frame_count_param` / `frame_count_step` / `frame_count_offset` / `frame_count_range` 或等价帧数约束，以及 `resolution_constraints` 分辨率约束。老工作流未声明这些字段时，可允许视频生成，但音频能力视为未知，界面必须提示并由真实工作流测试确认，不得自动认定其为 H3 VA。

## 9. MinimaxH3 提示词技能

视频生成工作流使用 MinimaxH3 能力，支持三种生成方式。提示词中通过 `@资产名称` 语法引用已上传的资产图片。

### 9.1 三种生成方式

| 方式 | 缩写 | 适用场景 | 提示词重点 |
| --- | --- | --- | --- |
| 纯文字生成视频 | T2VA | 无参考图片，直接根据文字生成 | 写清人物外观、初始构图、动作顺序、镜头运动和声音 |
| 首帧图片生成视频 | I2VA | 已有角色图或分镜图 | 描述图片之后发生什么，保持原图的人物身份、服装、构图和场景 |
| 多素材参考生成视频 | Ref2VA | 需要参考角色图、场景图、动作视频或声音素材 | 说明每个参考素材的用途，明确哪些特征必须保留、哪些动作或风格需要迁移 |

本系统主要使用 Ref2VA 方式，将角色/场景/道具/分镜图作为参考素材传入。

### 9.2 资产命名要求

角色、场景、道具等资产在上传时必须填写明确的 `name` 字段，该名称将作为提示词中的引用标识：
- 仅允许中文、英文、数字、下划线，禁止空格和特殊字符
- 同一项目内资产名称不可重复
- 单镜头手动绑定资产上限为 6 条：`POST /api/shots/{id}/assets` 在 `asset_ref` 已有 6 条时返回 `400 invalid_request`，重复绑定同一资产返回 `409 asset_in_use`
- 该 6 条上限针对手动绑定表，与工作流参考图槽位能力（`capabilities.max_reference_images`，H3 R2V 为 9）是两个独立概念，不要混用
- 示例：`@小明`（角色）、`@雨夜街头`（场景）、`@魔法书`（道具）

### 9.3 系统注入规则

生成视频时，系统自动将当前镜头关联的资产注入提示词上下文。此行为由后端在 `POST /api/shots/{id}/generate` 内部完成，前端无需额外调用绑定接口：
1. 查询该镜头所属场景关联的角色/场景资产，加上该镜头已有的分镜图资产
2. 自动创建 `asset_ref` 记录（role 为 reference），将资产绑定到镜头
3. 提示词内部使用 `@名称` 引用资产；提交前由提示词编译器按实际连线的参考槽位顺序把 `@名称` 及『参考图1/2』统一编译为 H3 节点识别的 `<Picture N>`（N 为剩余连线顺序，见第 10.3 节），`@名称` 不直接发送给 H3 本地节点
4. 用户可在镜头编辑器中预览和手动调整注入后的完整提示词

### 9.4 工作流标记要求

视频工作流必须存在 `$prompt` 必填标记（H3 本地节点的 `prompt` 输入键标注 `$prompt.prompt!`）；编译后的 `<Picture N>` 引用与用户提示词一并注入该参数。

## 10. 结构化提示词模板

### 10.1 多镜头提示词格式

每个镜头使用统一的 `integrated_multimodal_description` 格式编写 H3 提示词，确保画面描述、对白、声音和音乐信息完整且结构化。

单镜头提示词结构：`[Shot N] 时间范围。景别，机位与运镜。场景与人物描述，人物动作与表情。对白内容。声音与环境音。`

声音设计字段：`overall_soundscape`（环境声、动作音效，与画面同步）、`non_diegetic_music`（背景音乐，描述情绪与变化节点）。

重要约束：
- 多镜头提示词不代表 H3 每次都会精确按照时间点切镜；需要精确控制时建议逐镜头生成再剪辑
- H3 VA 单次生成按帧数制规划（fps=24、`length` 124–362，约 5.2–15.1 秒）；实际可生成时长受当前工作流的帧数、帧率及模型约束
- 生成前根据镜头目标时长与工作流能力反算帧数并对齐；反算后再次计算实际时长，无法满足目标时长则提示用户拆镜、合并节拍或调整目标时长，不得静默提交不合法参数
- 生成后必须探测实际视频时长、音轨是否存在及音频实际时长；对白镜头还需检查对白内容/可辨识性与音画同步。模型成功返回文件不等于质量检查通过

### 10.2 结构化分镜图提示词模板

分镜图采用固定分段的结构化提示词（任务 / 参考图职责 / 目标画面 / 必须保持 / 允许变化 / 禁止 / 输出），替代自由文本描述。完整模板结构与示例见 PROMPTS.md 第 4 节。

槽位自动填充规则（后端按镜头关联关系自动填充，前端无需手动指定）：

| 槽位 | 取值来源 |
| --- | --- |
| ref_identity | 该镜头主说话角色的 portrait_asset_id（定妆图） |
| ref_costume | 同上，暂由角色定妆图兼任 |
| ref_scene | 该镜头所属场景的 baseline_asset_id（场景基准图） |

填充约束：
- 锚定资产未锁定时返回 409 `invalid_state_transition`，不自动降级
- 非锚定参考资产未锁定时不填充该槽位，并在任务结果中记 warning
- 三个槽位全部无法填充时降级为文生图（text_only），但不得静默降级：须用户在生成确认弹窗中显式确认
- 填充结果可在生成确认弹窗中预览，用户可手动取消某个槽位
- 道具锚定图不占用上述三个槽位，通过 @名称 语法在提示词中引用；关键道具锚定图为可选资产，仅当 `anchor_required=true` 时作为硬依赖

### 10.3 结构化视频提示词模板与槽位剔除

视频提示词对应 `shot.video_prompt`，与分镜图模板并列存放，互不覆盖。完整模板结构（开场状态六项 / 剧情与动作 / 目标状态六项 / 必须保持 / 禁止 / 对白与声音）见 PROMPTS.md 第 5 节。

#### 视频六槽位与取值来源

| 槽位 | 负责约束 | 自动取值来源 |
| --- | --- | --- |
| ref_identity | 人物身份、脸型、五官、发型 | 主说话角色的 `portrait_asset_id` |
| ref_costume | 服装颜色、配饰、全身比例 | 暂由角色定妆图兼任 |
| ref_scene | 场景、光线与空间陈设 | 场景 `baseline_asset_id` |
| ref_prop | 关键道具的形制、颜色与持握关系 | 提示词中 `@道具名` 引用道具的 `anchor_asset_id`，多道具时按出现顺序取用 |
| ref_prev_frame | 与上一镜头的画面连续性 | 同场景同集内 `seq - 1` 镜头的末帧资产 |
| ref_image | 当前镜头分镜图（目标构图与终态） | 该镜头的 keyframe 资产 |

补充约定：
- 视频工作流中 `ref_image` 固定表示当前镜头分镜图，与其余五个槽位并存；出图工作流中 `ref_image` 为通用单图参考，与身份/服装/场景三槽位互斥
- 同一镜头绑定的参考素材总数不超过 6 个，含分镜图在内
- 任一来源资产未锁定时不填充该槽位，记 warning，不阻断生成
- `ref_prev_frame` 在首镜（seq=1）或上一镜头末帧缺失时不注入，此时仅靠 `start_state` 枚举值衔接
- 分组注入：`ref_prev_frame` 单独作为参考图1；`ref_image` 作为参考图2；`ref_identity`、`ref_costume`、`ref_scene`、`ref_prop` 作为参考图2 的细化来源追加注入，不单独成段；提交 H3 时提示词编译器按实际连线顺序把『参考图1/2』与 `@名称` 统一编译为 `<Picture N>`（槽位映射键 `ref_images.ref_image_N`，见第 13.1 节）
- 六个槽位全部无法填充时需降级为 T2VA；H3 参考生视频要求至少一张参考图或参考视频，降级须改用 T2V 工作流（`wf_video_h3_t2v`），否则返回 422 `h3_reference_required`

#### 约束冲突处理

参考图1 与参考图2 是同一段动作的两端，存在差异是正常现象。真正的冲突指同一要素在两张图中互斥且无法通过动作调和：

| 情形 | 处理 |
| --- | --- |
| 位置、姿态、视线、道具持握不同 | 不算冲突。开场严格承接参考图1，经动作与运镜逐步过渡到参考图2，禁止开场即跳变 |
| 身份、发型、服装、体型不一致 | 属于身份冲突，以参考图2 为准，并提示检查角色定妆图是否被替换 |
| 场景布局、光线、屏幕方向不一致 | 以参考图1 为准，保证与上一镜头处于同一空间；参考图2 仅取构图与景别 |
| 道具数量或持有者互斥且无法调和 | 不静默取舍，在生成确认弹窗列出冲突项，由用户改分镜图或改动作后重试 |

首镜无参考图1 时，全部约束以参考图2 为准，开场状态由 `start_state` 枚举值决定。

#### 可选槽位剔除与提示词编译（P0）

所有参考槽位均为可选；提交前删除未注入的 LoadImage 节点及指向它的输入键；提示词按剩余连线重编 `<Picture N>` 编号（不沿用满槽编号）；剔除结果写入任务 `input_snapshot` 并显示于确认弹窗。`backend/scripts/prune_unused_slots.py` 为参考实现。

#### 末帧自动抽取

视频生成成功后，系统自动从产物中抽取最后一帧存为独立资产：

| 项目 | 规则 |
| --- | --- |
| 资产类型 | `asset_type = last_frame` |
| 触发时机 | 视频任务状态置为成功之后，由同一任务的后处理步骤完成 |
| 命名 | 取所属镜头 `seq`，格式如 `第12镜末帧`，项目内唯一 |
| 关联 | 自动创建 `asset_ref`，`role = end_frame`，绑定到产出该视频的镜头 |
| 版本 | 同一镜头重新生成视频时不覆盖旧资产，按 `version` 递增并更新 `parent_id` |
| 失败处理 | 抽帧失败不改变视频任务的成功状态，记 warning 并允许手动重试抽取 |
| 锁定 | 与其它资产一致，锁定后被引用则不可删除 |

抽帧实现口径：FFmpeg 取视频最后一帧；中间格式 PNG 无损（后续工作流需要可在注入前转 JPG，原 PNG 保留）；分辨率与源视频一致不做缩放；按项目与类型分目录存储；保持源视频色彩空间不做色调映射。

#### 批量生成的依赖调度约束

视频任务按依赖关系调度，worker 并发 = 可用 ComfyUI 实例数（D-18 修订 D-03：单实例串行，多实例由实例池加权轮询分发并行执行）；只有明确依赖前镜尾帧的镜头才必须等待前镜完成：

1. 同场景同集且 `requires_prev_frame=true` 的镜头按 `seq` 升序处理，前镜成功并抽出末帧后才开始后镜
2. 镜头失败时，仅暂停依赖该镜头视频/末帧的后续镜头；独立场景或集继续执行
3. 批量结果逐镜记录 `blocked_by_dependency`（作为 pending 的子状态 blocked_reason）、失败镜头 ID 与 seq；不再将所有后续镜头统一标记 prev_shot_failed
4. 用户可重试失败项、从失败点继续或显式忽略失败强制继续；强制继续时提示连续性可能受影响
5. 已完成且依赖版本仍匹配的镜头跳过；产物已过期的镜头不得仅凭 `done` 状态跳过

#### 骨架完整性检查项（warning 级，不阻断）

生成前对 `shot.video_prompt` 做静态检查，命中问题写入任务 `warnings` 数组：

| 检查项 | 判定 | 提示语 |
| --- | --- | --- |
| 镜号标记 | 含 `[Shot N]` 形式标记 | 缺少镜号标记，多镜头混排时可能无法区分段落 |
| 时间码 | 含 `00:00–00:03` 形式的起止时间 | 缺少时间码，模型将自行决定节奏 |
| 段落完整性 | 至少含画面描述与动作两部分 | 画面或动作缺一，生成结果易偏离预期 |
| 台词同步 | `dialogue` 非空时提示词含对白或以对白描述 | 该镜头有台词但提示词未描述口型或对白 |
| 资产引用 | 提示词中的 `@名称` 均能在项目内找到对应资产 | 存在无法解析的资产引用 |
| 时长一致 | 时间码跨度与 `shot.duration` 偏差不超过 20% | 时间码与预估时长偏差较大 |

检查只做结构比对，不评估画面质量，也不改写用户原文。`video_prompt` 为空时跳过全部检查，由系统按模板与镜头元信息自动拼装。

## 11. 音频生成工作流（audio）

音频工作流负责按需生成 TTS 补救配音及可选的 BGM/SFX 素材。对白/旁白音频的默认来源是 H3 VA 视频工作流同次生成的音轨；系统完成视频生成后先检测内嵌音轨、音频有效性、对白内容与音画同步，只有检查失败或人工要求重配时才调用 TTS。**生成素材与时间线编排是两个职责**：H3 音频或 TTS 补救产物绑定镜头/对白段；SFX 可绑定镜头或场景；BGM 可绑定场景或集，最终由合成时间线安排位置、音量与淡入淡出。合成 API 不得仅靠单个项目级 `bgm.asset_id` 表达完整多轨需求。

### 11.1 音频工作流参数（必需性按 workflow_type 判定）

| 参数名 | 类型 | 标记示例 | 说明 |
| --- | --- | --- | --- |
| `tts_text` | string | `$tts_text` | `tts` 必需；台词文本由系统从 shot.dialogue 或 dialogue_segments 注入 |
| `tts_voice` | string | `$tts_voice` | `tts` 必需；按 dialogue_segments[].speaker_id 解析角色 voice_id |
| `tts_speed` | float | `$tts_speed` | `tts` 可选，默认 1.0，范围 0.5-2.0 |
| `tts_emotion` | string | `$tts_emotion` | `tts` 可选，默认读取 shot.emotion，取值 neutral/happy/sad/angry/fearful |
| `bgm_style` | string | `$bgm_style` | 仅 `bgm` 生成工作流按其 schema 要求；从 shot.bgm 或用户配置读取 |
| `soundscape` | string | `$soundscape` | 仅 `sfx` 生成工作流按其 schema 要求；从 shot.soundscape 或用户配置读取 |
| `duration_hint` | float | `$duration_hint` | `tts` 可选；目标时长（秒），由系统根据台词长度估算 |

### 11.2 各类型工作流输出契约

| 工作流类型 | 必需输出 | 可选输出 | 说明 |
| --- | --- | --- | --- |
| `video`（H3 VA） | `$output.clip`；若音频内嵌于视频，则须可探测到有效音轨 | 独立音频输出、词/句级时间戳、分段元数据 | `capabilities.audio=true` 时应检查生成结果中的音轨；音频可内嵌在 MP4，无需强制 `$output.voice`；音频不可用时才进入 TTS 补救 |
| `tts`（补救） | `$output.voice`（wav/mp3）及可由文件探测得到的 `duration` 元数据 | 词/句级时间戳、分段元数据 | 仅对 H3 VA 音频检查失败且按 `shot_kind` 需要配音的镜头调用；`duration` 由系统探测写入，不要求工作流输出 `$output.duration` 节点 |
| `bgm` | `$output.bgm`（wav/mp3） | — | 可由生成工作流产出，也可从素材库选择，不是合成前的必需产物 |
| `sfx` | `$output.sfx`（wav/mp3） | — | 可由生成工作流产出，也可从素材库选择，不是合成前的必需产物 |

### 11.3 TTS 模型选型

| 引擎 | 适用场景 | 音色数量 | 情感支持 | 备注 |
| --- | --- | --- | --- | --- |
| GPT-SoVITS | 少样本音色克隆 | 用户上传 | 支持 | 推荐，适合角色音色定制（本期未交付标准工作流） |
| CosyVoice | 零样本克隆 | 用户上传 | 支持 | 备选，生成速度快（本期未交付标准工作流） |
| Edge-TTS | 预设音色 | 300+ | 有限 | 快速原型验证用（本期未交付标准工作流） |
| Qwen3-TTS | 9 个预设音色 + 参考音频克隆 | 9 预设 / 用户上传 | 预设模式支持自由文本情感指令 | 标准工作流选用（见第 13 节）；无语速与目标时长控制，时长以探测为准 |

音色管理：
- 每个角色在 `characters` 表中存储 `voice_id` 字段，指向已注册的音色
- 音色注册流程：用户上传参考音频 → 系统调用 TTS 工作流的音色注册节点 → 返回 voice_id
- 同一角色在不同项目中复用同一 voice_id
- `voice_id` 的含义由所选 TTS 工作流的 `capabilities.voice_mode` 决定：`preset` 模式取预设音色名（如 Qwen3-TTS 的 Vivian），注入 `tts_voice`；`clone_reference_audio` 模式指向项目级参考音频资产（`asset_type=audio`），由执行器上传并注入 `tts_voice_ref`。同一角色须固定一种模式，切换模式视为修改 `voice_id`，按失效传播矩阵使该角色配音失效
- `tts_speed`、`duration_hint` 对未声明支持的工作流直接忽略并在任务结果记 warning；`tts_emotion` 按工作流 `emotion_map` 转为指令文本，工作流不支持情感则忽略并记 warning

音色取值规则（仅当 H3 VA 音频需要 TTS 补救时按镜头发起）：

| 情形 | 音色来源 |
| --- | --- |
| 镜头有 dialogue_segments 且非空 | 逐段取该段 speaker 对应角色的 voice_id |
| 镜头无分段，但 speaker 非空 | 取 speaker 对应角色的 voice_id |
| 镜头仅有 character_id | 取该角色的 voice_id |
| 以上均无 | 使用系统设置的默认音色，并在任务结果中记 warning |

多人对话合成方式：
1. 按分段顺序逐段调用 TTS，每段使用该段 speaker 的音色与 `tts_emotion`
2. 各段音频之间插入 200 毫秒静音间隔，避免语句粘连
3. 拼接为单条配音，实际总时长写入 `shot.actual_duration`
4. 任一段合成失败时整个镜头配音任务标记 failed，不保留部分结果

单说话人镜头不触发分段合成，直接按整段 dialogue 一次性生成。

speaker 解析容错：speaker_id 是权威字段。若输入仅有 speaker 名称，则按项目内角色名称精确匹配并在入库前补写 speaker_id；无法匹配时保留原始名称并置为待审核，不允许静默改用默认音色。仅用户明确确认『使用默认音色』后才可继续，并记录审核人、时间与选择。审核阶段应提前提示所有未匹配 speaker。

### 11.4 BGM 与音效策略

| 来源 | 说明 | 授权要求 |
| --- | --- | --- |
| 本地素材库 | 用户预先导入的 BGM/SFX 文件 | 用户自行确保授权 |
| 生成式模型 | 通过 ComfyUI 音频生成节点（如 AudioLDM、Stable Audio）生成 | 模型许可证 |
| 素材匹配 | 系统根据 bgm_style 标签从素材库自动匹配 | 用户自行确保授权 |

BGM 时长匹配：系统根据镜头 duration 字段预判所需 BGM 时长；素材/生成 BGM 短于镜头时长时 compose 负责循环或淡出处理；长于镜头时长时 compose 负责截断或淡出。

### 11.5 字段到工作流参数的映射

```
shot.soundscape  → $soundscape   （环境音效描述）
shot.bgm         → $bgm_style    （BGM 风格）
shot.dialogue    → $tts_text     （台词文本）
dialogue_segments[].speaker_id → $tts_voice（逐段通过 speaker_id 关联 character.voice_id；仅单说话人兼容路径可回退到明确的 speaker_id/speaker 映射；不得以 shot.character_id 覆盖多人分段）
shot.emotion     → $tts_emotion  （情感标签）
shot.duration    → $duration_hint（目标时长）
```

## 12. 工作流能力声明与媒体安全（P0）

### 12.1 能力声明提交方式

`capabilities` / `trigger` 必须经独立字段或 sidecar `*.meta.json` 提交，写入工作流 JSON 顶层会破坏「节点 ID→节点对象」格式校验。capabilities 还须含 `reference_slots`、`frame_count_param/frame_count_step/frame_count_offset/frame_count_range`（或等价 `duration_param/duration_range/duration_step`）、`resolution_constraints`、`requires`；未知的 `fps`、`frame_count_step` 置 `null` 待首次联调回填，禁止推测；不得按工作流名称推测能力。

### 12.2 媒体与工作流安全

- 节点类型允许列表/风险分级
- ComfyUI 地址校验协议与目标范围
- 上传媒体查文件头+实际解码
- FFmpeg 超时限制
- 密钥加密、日志脱敏
- 云端 Partner Node 工作流已随 D-10 弃用；如重新引入须标注「云端、计费、素材外发」，默认需用户确认才可设为默认。本地权重 H3 工作流标注 `local_only`，需 ≥24GB 显存（验证目标 A10，V100 未验证）
- 本期单用户内网可，对外须鉴权

### 12.3 按定义生成的参考槽位注入与绑定快照（P0）

按定义批量生成（`POST /projects/{id}/asset-plans/generate`）的参考图注入规则：

1. **槽位由定义绑定决定**：`asset_plan_dependency` 按 (plan_id, slot) 记录依赖（ref_identity/ref_costume/ref_scene/ref_prop/ref_image），注入不依赖提示词文本书写顺序，从根本上避免槽位错位。
2. **仅注入工作流声明的槽位**：以 `capabilities.reference_slots` 为准（字符串数组或 `{param, input_key, optional}` 对象数组），未声明的槽位不注入，避免无效上传与工作流不支持的输入。
3. **版本锁定**：解析优先取定义绑定的当前有效资产（`validity == 'current'`）；绑定资产被替换时回退到归属实体/镜头的当前有效资产，不引用过期版本。
4. **绑定快照**：每个槽位固化 `slot/ref_key/asset_id/version/file_path/status` 随任务参数 `__binding_snapshot__` 持久化；重试复用快照，不重新解析（版本漂移防护）。
5. **系统保留槽位**：`ref_prev_frame`/`first_frame`/`end_frame` 由执行器按镜头实际产物回填（上一镜尾帧、跨集首帧），提示词中不写 @ 引用，@引用解析也不得占用。
6. **文件缺失处理**：参考图文件不存在时跳过该槽位并记录 `file_missing`，不阻断生成。
7. **提示词 @名称 语法**：分镜图/视频定义提示词用 `@角色名`/`@场景名` 引用（@引用解析可携带参考图）；`ref_image`（本镜分镜图）与 `ref_prev_frame`（上一镜末帧）为系统保留槽，提示词中不写 @ 引用。

## 13. 标准工作流清单（随文交付，9 份）

标准工作流文件位于 `backend/data/workflows/` 目录，均为 ComfyKit API 格式；大部分附带 `.meta.json` 能力声明，例外情况见清单内标注：

| 文件 | target / workflow_type | 说明 | required_params | priority |
| --- | --- | --- | --- | --- |
| `wf_image_t2i_qwen.json` | keyframe / image | Qwen-Image + Lightning 4 步 LoRA，`$output.cover`（定妆图/场景基准图） | [] | 0 |
| `wf_image_ref_qwen_edit.json` | keyframe / image | Qwen-Image-Edit-2511 + TextEncodeQwenImageEditPlus（3 图槽位），`$output.cover`（分镜图） | [ref_identity] | 10 |
| `wf_video_h3_ref2va.json` | clip / video | UNETLoader + MiniMaxH3ReferenceToVideo + SaveVideo(mp4/h264)，`$output.clip`（主力 R2V，本地权重 R2VA） | [ref_image] | 10 |
| ~~`wf_video_h3_ref2va_turbo.json`~~ | clip / video | **未交付**：`backend/data/workflows/` 与 workflow 表均无此工作流（实际仅 8 个）。turbo LoRA 权重虽在模型清单中，但未配成可导入工作流；启用需先补 meta 与 capabilities 再导入 | [ref_image] | 10 |
| `wf_video_h3_t2v.json` | clip / video | UNETLoader + MiniMaxH3ImageToVideo（纯文字降级方案，FL2VA 权重） | [] | 0 |
| `wf_video_h3_flf2v.json` | clip / video | UNETLoader + MiniMaxH3ImageToVideo + 首尾帧 ImageScale(crop=center) 裁切；宽高由 Primitive 驱动 | [first_frame] | 5 |
| `wf_tts_qwen3_custom_voice.json` | audio / tts | Qwen3Loader + Qwen3CustomVoice + SaveAudioMP3，`$output.voice`；预设音色 9 个（Vivian/Serena 等），不支持语速与目标时长控制，情感经 emotion_map 映射（推荐值未经验证） | [tts_text] | 0 |
| `wf_tts_qwen3_voice_clone.json` | audio / tts | Qwen3Loader(Base) + Qwen3VoiceClone + LoadAudio；克隆模式建议 1–10 秒干净人声参考并提供 ref_text，上限 30 秒，不支持情感控制 | [tts_text, tts_voice_ref] | 10 |
| `wf_bgm_stable_audio.json` | bgm / bgm | Stable Audio Open 1.0，`$output.bgm`；时长由单个 PrimitiveFloat 同时驱动 EmptyLatentAudio.seconds 与 ConditioningStableAudio.seconds_total | [bgm_style] | 0 |
| ~~`wf_sfx_stable_audio.json`~~ | sfx / sfx | **本期不交付**：文件未随文档提供。文档层保留 sfx 枚举与链路定义；SFX 为可选轨道，缺失不阻塞合成，可从素材库选取 | [soundscape] | 0 |

### 13.1 H3 六参考槽位映射（`ref_images.ref_image_N`）

0=ref_prev_frame（上镜末帧）、1=ref_image（当前分镜图）、2=ref_identity、3=ref_costume、4=ref_scene、5=ref_prop（官方 autogrow 命名从 0 起）；`prompt`/`length`/`seed` 由 Primitive 节点连线驱动；`width`/`height` 由执行器按第 5.2 节分辨率约束注入；提示词以 `<Picture N>` 按连线顺序引用，剔除槽位后重新编号。

### 13.2 H3 约束

- 帧数制：fps=24、`length` 124–362、步长 17、offset 5（约 5.2–15.1 秒）；超范围返回 422 `h3_duration_unrepresentable`
- 分辨率：32 的倍数、短边 384–768、长边 ≤1344；映射 9:16→736×1312、16:9→1312×736、1:1→768×768
- 参考图 ≤9 张、短边 ≥256px、宽高比 0.4–2.5；R2V 至少 1 张参考图，否则 422 `h3_reference_required`
- R2V 不支持首尾帧；FLF2V 首尾帧经 ImageScale(crop=center) 裁切到画布
- 同生成立体声音轨内嵌 mp4（`audio=true`、`audio_output_mode=embedded`）
- 本地权重 5 项（见 13.3）；需 ≥24GB 显存（验证目标 A10，V100 未验证，NVFP4 不支持已排除）；ComfyUI 须含官方 MiniMax H3 原生节点（2026-08 合入），勿装 ComfyUI-MiniMaxH3-Cache；权重受 MiniMax H3 Community License 约束（美/欧/英/韩为排除地区）

### 13.3 模型默认文件名

按链路分组，与各工作流 JSON 实际引用一致（2026-10-24 核对）：

- 图像链路 6 项：`qwen_image_fp8_e4m3fn.safetensors`、`qwen_image_edit_2511_bf16.safetensors`、`qwen_2.5_vl_7b_fp8_scaled.safetensors`、`qwen_image_vae.safetensors`、Lightning LoRA `Qwen-Image-Lightning-4steps-V1.0.safetensors` 与 `Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors`
- H3 视频链路 6 项：本地权重 5 项 `minimax_h3_ref2va_pruned_int8_convrot.safetensors`、`minimax_h3_fl2va_pruned_int8_convrot.safetensors`、`qwen3vl_32b_minimax_h3_int8_convrot.safetensors`、`minimax_h3_video_vae_fp16.safetensors`、`minimax_h3_audio_vae_fp32.safetensors`；turbo 加速 LoRA `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`（仅 ref2va_turbo 工作流使用）
- 音频链路 4 项：BGM `stable-audio-open-1.0.safetensors` + `t5-base.safetensors`；TTS 经 Qwen3Loader 按 HuggingFace 仓库加载，无本地 safetensors 文件：`Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`（预设音色）、`Qwen/Qwen3-TTS-12Hz-1.7B-Base`（声音克隆）

### 13.4 依赖与限制

- Qwen3-TTS 依赖 `DarioFT/ComfyUI-Qwen3-TTS`，要求 `transformers==4.57.3`（建议独立实例/venv）；无语速/目标时长/词级时间戳，字幕走句级对齐（PRD.md M7 节）
- Stable Audio Open 1.0 单次 ≤约 47 秒，商用须核许可证，不得生成对白

### 13.5 校验脚本

`backend/scripts/validate_workflows.py` 供 M5-02/03/04 校验：API 格式、连线完整、标记非连线、同名参数不重复、宽高同时绑定、`$output.*` 存在。

## 14. 本期未交付与待实测项（TBD，详见 DECISIONS.md）

首次真实联调须回填/确认（详见 DECISIONS.md 第 3 节）：H3 本地权重 5 项部署核对、A10 实测生成耗时与显存峰值（原版 ref2va 已验证：768×1344 124帧约 46 分 13 秒、峰值约 22.1GB；turbo 4 步变体待验证）、模型文件名与本机一致性、三图参考一致性评分基线；Qwen3-TTS 情感指令映射；Stable Audio 最大时长与采样参数。

本期未交付：GPT-SoVITS/CosyVoice/Edge-TTS 标准工作流（当前 TTS 标准实现为 Qwen3-TTS）；SFX 标准工作流（见第 13 节清单）。
