# STYLE —— 剧本类型与风格配置体系

本文件是项目级剧本类型与风格配置的唯一权威说明，与 `backend/app/services/style_config.py` 同步维护。接口字段以 docs/API.md 为准，表结构以 docs/DATABASE.md 为准，提示词模板以 docs/PROMPTS.md 为准。

## 1. 设计目标

- 把原先分散在提示词里的题材、风格、节奏、情绪等描述收敛为一份结构化配置，落在 `project.settings`。
- 同一份配置贯穿剧本解析、角色/场景/道具资产图、分镜图、分镜视频全流程，避免各环节各自硬编码风格。
- 配置可快照、可追溯、可校验，生成任务留存当时使用的风格快照，便于复现与排障。
- 风格只约束表达方式，不改变剧本事实与因果；任何注入段落都带此声明。

## 2. 配置字段

| 字段 | 类型 | 多选 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| genre_tags | string[] | 是 | 空数组 | 剧本类型标签，第一个为主类型 |
| narrative_styles | string[] | 是 | 空数组 | 叙事风格，第一个为主风格，其余为辅助 |
| visual_style | string | 否 | storybook_realism | 主视觉风格，全局唯一 |
| pacing | string | 否 | balanced | 剧情节奏 |
| emotional_tone | string[] | 是 | 空数组 | 情绪基调，第一个为主情绪 |
| conflict_intensity | string | 否 | moderate | 冲突强度 |
| twist_density | string | 否 | occasional | 反转密度 |
| dialogue_style | string | 否 | natural | 对白风格 |
| narration_mode | string | 否 | mixed | 叙述方式 |
| target_audience | string | 否 | all_ages | 目标受众 |
| custom_style_requirements | string | 否 | 空串 | 自定义风格要求，上限 500 字符 |

字段全集由 `STYLE_FIELDS = tuple(default_style_config().keys())` 派生，新增字段必须同步 `default_style_config()`、`normalize_style_config()`、`options_payload()` 与本文档。

## 3. 取值表

### 3.1 剧本类型（genre_tags）

由 `GENRE_GROUPS` 分组提供，展平为 `GENRE_TAGS`，共 7 组。

| 分组 | 标签 |
| --- | --- |
| 都市与情感 | 都市情感、甜宠恋爱、虐恋、婚姻家庭、家庭伦理、豪门恩怨、职场商战、社会现实 |
| 古装与历史 | 古装权谋、宫廷争斗、历史传奇、武侠江湖、民国传奇、战争历史 |
| 奇幻与幻想 | 穿越、重生、玄幻、仙侠、系统流、异世界、神话传说、魔法冒险、科幻幻想 |
| 悬疑与动作 | 悬疑推理、刑侦犯罪、复仇反转、动作冒险、谍战、惊悚恐怖、灾难求生 |
| 青春与成长 | 校园青春、友情成长、励志奋斗、亲子教育、师生故事、运动竞技 |
| 儿童与动画故事 | 童话故事、寓言故事、儿童冒险、亲子故事、校园儿童剧、动物故事、科普剧情、儿童成长 |
| 喜剧与生活 | 搞笑喜剧、荒诞喜剧、黑色幽默、乡村生活、职场日常、家庭生活、社会寓言 |

### 3.2 叙事风格（narrative_styles）

可多选，首个为主风格。每项带 `constraint` 约束句，注入提示词时逐项展开。

写实细腻型、戏剧冲突型、爽感逆袭型、悬念反转型、轻松幽默型、温情治愈型、寓教于乐型、诗意抒情型、黑暗压抑型、史诗宏大型。

### 3.3 主视觉风格（visual_style）

单选，每项含 label / summary / prompt 三段信息，`prompt` 为注入提示词的美术风格原文。

| 内部值 | 说明 |
| --- | --- |
| storybook_realism | 默认值，童话绘本写实 |
| cinematic_realism | 电影级写实 |
| 3d_cartoon | 三维卡通风 |
| chinese_ink | 国风水墨 |
| anime_2d | 二维日式动画 |
| storybook_watercolor | 绘本水彩 |
| flat_illustration | 扁平插画 |
| comic | 美漫/条漫 |
| clay_stop_motion | 黏土定格 |
| low_poly | 低多边形 |
| retro_film | 复古胶片 |
| paper_cut | 剪纸 |
| pixel_art | 像素风 |

历史值中文名通过 `_LEGACY_VISUAL_STYLE_ALIASES` 做关键字迁移，例如国风水墨映射到 chinese_ink。无法识别时回退默认并产生 `visual_style_unknown`。

### 3.4 节奏（pacing）

| 取值 | 说明 |
| --- | --- |
| slow | 舒缓 |
| gentle | 温和 |
| balanced | 均衡，默认 |
| tight | 紧凑 |
| fast | 快节奏 |

### 3.5 情绪基调（emotional_tone）

可多选，首个为主情绪，注入时主情绪必给出、前两个辅助情绪可选。

欢乐、温馨、轻松、紧张、悲伤、热血、神秘、治愈、浪漫、庄严、黑暗压抑。

### 3.6 其余枚举

| 字段 | 取值 |
| --- | --- |
| conflict_intensity | low / moderate / high |
| twist_density | none / occasional / frequent |
| dialogue_style | natural / concise / humorous / poetic / dramatic / child_friendly |
| narration_mode | voiceover / dialogue / action / mixed |
| target_audience | kindergarten_primary_2 / primary_middle_high / teen / adult / all_ages |

## 4. 版本与默认值

| 常量 | 值 | 说明 |
| --- | --- | --- |
| STYLE_CONFIG_VERSION | style-v1.0 | 写入项目 settings 与任务快照 |
| DEFAULT_VISUAL_STYLE | storybook_realism | 缺省视觉风格 |
| MAX_CUSTOM_LENGTH | 500 | 自定义风格要求上限 |

默认视觉风格的 `prompt` 与 `asset_prompts._DEFAULT_STYLE` 完全一致，保证旧项目在未配置风格时资产图提示词不发生变化。

## 5. 规范化与校验

入口为 `normalize_style_config(config)` 与 `validate_style_config(config)`，后者返回 `(cfg, issues)`。校验不阻断保存，只把问题回传给前端提示。

| 问题码 | 触发条件 | 处理方式 |
| --- | --- | --- |
| visual_style_unknown | 视觉风格取值无法识别 | 回退默认 |
| narrative_style_unknown | 叙事风格不在词表内 | 丢弃该项 |
| emotional_tone_unknown | 情绪不在词表内 | 丢弃该项 |
| custom_style_too_long | 自定义文本超过 500 字符 | 截断，severity=error |
| narrative_style_conflict | 主风格与辅助风格相互矛盾 | 保留配置顺序，不强行改写 |
| genre_conflicts_audience | 题材与低龄受众冲突 | 仅提示 |
| narrative_conflicts_audience | 叙事风格与低龄受众冲突 | 仅提示 |
| conflict_conflicts_audience | 冲突强度与低龄受众冲突 | 仅提示 |
| pacing_unknown | 剧情节奏取值无法识别 | 回退默认 |
| conflict_intensity_unknown | 冲突强度取值无法识别 | 回退默认 |
| twist_density_unknown | 反转密度取值无法识别 | 回退默认 |
| dialogue_style_unknown | 对白风格取值无法识别（含旧自由文本） | 回退默认 |
| narration_mode_unknown | 叙述方式取值无法识别 | 回退默认 |
| target_audience_unknown | 目标受众取值无法识别 | 回退默认 |

低龄受众集合为 `kindergarten_primary_2` 与 `primary_middle_high`，冲突题材集合为 惊悚恐怖、灾难求生、刑侦犯罪。

旧键迁移：`genre` 映射到 `genre_tags`、`audience` 映射到 `target_audience`、`requirements` 映射到 `custom_style_requirements`。

## 6. 提示词注入

统一由 `compile_style_prompt(config, task_type=...)` 生成美术风格片段，按任务类型分级注入。

| task_type | 注入内容 | 设计理由 |
| --- | --- | --- |
| character / scene / prop | 视觉风格 prompt + 自定义要求 | 资产图用于定义身份，不掺入剧情情绪 |
| storyboard | 视觉风格 + 节奏 + 主情绪（附辅助情绪） | 约束分镜构图与光影氛围 |
| shot_video | 同 storyboard | 保持分镜图与视频视觉连续 |

情绪片段格式为「情绪基调以{主}为主，辅以{辅}」。

剧本分析侧由 `parse_context_block(config)` 生成注入段，包含 `describe_for_llm` 的中文配置描述、主视觉风格 prompt 原文要求，以及要求模型在 `project_analysis` 中额外输出 `detected_genre_tags` 与 `detected_narrative_styles`（无法判断时输出空数组，不得臆测）。

注入点：

| 环节 | 文件 | 注入方式 |
| --- | --- | --- |
| 剧本解析（Agent） | `app/services/script_agent.py` | `parse_context_block` 前置到用户消息，结果写入 `style_config_snapshot` 与 `style_config_diffs` |
| 剧本解析（执行器） | `app/worker/parse_executor.py` | 同上 |
| 资产图/分镜图/分镜视频 | `app/services/prompt_compiler.py` | 编译结果 `generation_constraints[style_config]` 与 `canonical_snapshot[style]` |

`describe_for_llm` 末尾固定声明：风格只约束表达方式、构图、光影、节奏与情绪，不得新增人物、道具、事件，不得改变故事因果与镜头绑定关系。

## 7. 快照与追溯

`style_snapshot(config, project_id=...)` 生成不可变快照，字段包含 version、project_id、genre_tags、narrative_styles、visual_style、visual_style_label、visual_style_prompt、pacing、emotional_tone、conflict_intensity、twist_density、dialogue_style、narration_mode、target_audience、custom_style_requirements。

快照与源配置解耦：写入后修改项目风格不影响已有生成任务快照，保证可复现。

## 8. 差异比对

`compute_style_diff(config, project_analysis)` 比对模型识别结果与用户配置，仅在模型输出了 `detected_*` 时才报差异，缺失不报，避免把未识别误判成冲突。返回项含 field、message、detected、configured。

两条防误报规则：

- 配置侧为空时跳过：用户未配置 `genre_tags` 或 `narrative_styles` 时，模型识别到的任何标签都不构成『不一致』，不产生差异项，避免把全量识别结果当成噪声。
- 比对前归一化：通过 `_normalize_tag` 去掉空白与常见中英文标点后比较，容忍模型输出的格式漂移（如多余空格、加书名号），避免因格式差异误报。

## 9. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/projects | 可选携带 `style_config`，以默认值为基线合并规范化后落库；不传则纯默认 |
| GET | /api/projects/style-options | 下发全部可选项与默认值，供前端渲染，避免前端硬编码 |
| GET | /api/projects/{project_id}/style-config | 读取规范化后的配置与校验问题 |
| PUT | /api/projects/{project_id}/style-config | 保存配置，返回规范化结果、问题列表与 saved 标记 |
| PATCH | /api/projects/{project_id} | settings 中含风格字段时自动规范化并写入 style_config_version |

响应字段含 config、version、issues、visual_style_label、raw、saved。保存后 `project.settings` 同时含 `visual_style` 与 `style_config_version`，非风格键（如 episode_duration_target）原样保留。

**PUT 为部分更新语义**：请求体只需携带要修改的字段，未提供的风格字段沿用项目已保存值，不会被默认值覆盖。实现上先把请求体合并进 `project.settings`，再做规范化校验，与 PATCH 行为保持一致。

## 10. 测试

| 测试文件 | 覆盖内容 |
| --- | --- |
| tests/test_style_config.py | 默认值、规范化、旧键迁移、校验问题码、片段编译、快照解耦、差异比对；枚举非法值提示、空配置不报差异、标签格式漂移容忍 |
| tests/test_style_config_api.py | 新建项目默认配置、选项下发、保存与规范化、非法视觉风格、PATCH 迁移、非风格键保留；PUT 部分更新保留既有字段、非法枚举值返回问题码 |
| tests/test_prompt_compiler.py | 资产图使用项目视觉风格、旧项目回退默认、自定义要求注入、分镜图节奏与情绪、视频风格与快照 |
| tests/test_script_parse.py | 解析结果写入风格快照与差异、默认回退、无 detected 字段时不报差异 |

运行方式：`cd backend && .venv/bin/python -m pytest tests/ -q`。

## 11. 已知限制

- 生图工作流侧尚无负向提示词在 CFG=1 条件下的对照实测数据，负向词实际生效强度待验证。
- 前端配置面板尚未实现，当前仅提供接口与选项下发。
- 差异比对依赖模型主动输出 `detected_*`，模型未输出时差异为空，属预期行为而非缺陷。
