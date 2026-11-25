# 02. 提示词质量审计

审计日期：2026-11-25
审计对象：27 个运行时注册模板（prompt_registry.PROMPT_REGISTRY）
数据来源：template_meta() 与 prompt_store.get_prompt() 运行时直读，非人工摘录
关联提交：0b61dc5（01 清单）、203f53a（注册表与指纹）

---

## 一、评估口径

五项客观指标，全部可复算：

1. 结构规模：行数、字符数
2. 接口面：占位符数量（模板真实 format 变量，由 template_input_vars 解析）
3. 输出契约：output_format（json / text）
4. 版本：registry version
5. 指纹：sha256 前 8 位（template_content_hash）

主观判断只用于『可维护性』一类无法量化的维度，且必须标注依据行或依据语句。

---

## 二、全量指标表

| 模板 ID | task_type | 版本 | 输出 | 行数 | 字符 | 占位符 | 指纹 |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| agent_intent_system | agent | 1.0 | json | 24 | 1737 | 1 | 848ea15a |
| agent_session_summary | agent | 1.0 | text | 5 | 180 | 0 | dc7f1c7e |
| asset_character | asset_prompt | 2.1 | text | 13 | 351 | 3 | 71dfee7a |
| asset_scene | asset_prompt | 2.1 | text | 19 | 462 | 3 | 35b2f2aa |
| asset_prop | asset_prompt | 2.1 | text | 13 | 279 | 3 | 253caa54 |
| compile_character_asset | asset_image | 2 | text | 24 | 797 | 11 | f75b890c |
| compile_scene_asset | asset_image | 2 | text | 20 | 840 | 15 | 1c341378 |
| compile_prop_asset | asset_image | 2 | text | 23 | 661 | 8 | 08c0eaf1 |
| compile_storyboard_image | storyboard_image | 3 | text | 24 | 865 | 20 | 9e1d93c8 |
| image_prompt | storyboard_image | 2.0 | text | 17 | 551 | 11 | 6205bc91 |
| compile_shot_video | shot_video | 4 | text | 11 | 811 | 24 | 655df301 |
| video_prompt_first | shot_video | 1.1 | text | 28 | 484 | 9 | d4c45855 |
| video_prompt_body | shot_video | 1.0 | text | 30 | 694 | 9 | 688a870e |
| scene_plate_compiler | scene_plate | 1.0 | json | 45 | 1847 | 6 | fe90fa7c |
| parse_system | script_parse | 1.2 | json | 78 | 4618 | 0 | 4b77c330 |
| script_outline_system | script_outline | 1.0 | json | 8 | 413 | 0 | 8e1c8327 |
| script_create_system | script_writing | 1.0 | text | 8 | 296 | 0 | 7c1c6295 |
| script_expand_system | script_writing | 1.0 | text | 8 | 266 | 0 | 7cef1d78 |
| script_normalize_system | script_writing | 1.0 | text | 23 | 1214 | 0 | 55b5ab4f |
| script_evaluation_system | script_evaluation | 2.0 | json | 47 | 2048 | 13 | 91d9386e |
| qc_keyframe_prompt | qc | 1.0 | json | 5 | 327 | 1 | 058ffdb9 |
| qc_clip_prompt | qc | 1.0 | json | 11 | 502 | 1 | 5c6cad4b |
| director_system | agent | 1.0 | text | 1 | 940 | 0 | 2e799f97 |
| voice_profile_system | voice_profile | 1.0 | json | 1 | 482 | 1 | 8e573010 |
| session_title_system | session_title | 1.0 | text | 1 | 23 | 0 | 85e8d56f |
| session_title_naming | session_title | 1.0 | text | 10 | 169 | 2 | 0894be55 |
| episode_continue_system | episode_writing | 1.0 | json | 11 | 686 | 1 | deacc3f9 |

合计 27 个模板。最大 parse_system（4,618 字符 / 78 行），最小 session_title_system（23 字符 / 1 行）。占位符最多 compile_shot_video（24 个），占位符为 0 的纯 system 指令模板实测 8 个：agent_session_summary、director_system、parse_system、script_create_system、script_expand_system、script_normalize_system、script_outline_system、session_title_system。

---

## 三、分层质量结论

### 3.1 剧本理解层（parse_system / script_outline_system）

- parse_system：78 行 4,618 字符，全库体量最大，0 占位符，纯 system 指令。规则与数据分离，符合『system 承载规则、user 承载数据』的分工，体量与职责匹配。
- script_outline_system：8 行 413 字符，json 输出，字段契约明确（total_episodes / estimated_total_duration / dialogue_char_estimate / episodes 及 episodes 内 4 个字段），契约完整。

### 3.2 剧本创作层（script_create / script_expand / script_normalize）

- create 与 expand 各 8 行（296 / 266 字符），长度对称，首句即区分『创作』与『扩写』，职责边界清晰。
- 二者文本相似度 0.616，重复部分为通用剧本格式条款，详见 03 文档。
- normalize 23 行 1,214 字符，体量约为 create 的 4 倍，符合『规范化需覆盖格式、集号、人物、旁白、声音提示多类规则』的定位。

### 3.3 资产层（asset_* 与 compile_*）

- asset_character / asset_scene / asset_prop：统一 2.1 版，13–19 行，279–462 字符，各 3 个占位符，接口完全对齐。
- compile_character_asset / compile_scene_asset / compile_prop_asset：统一 2 版，20–24 行，661–840 字符，8–15 个占位符。占位符密度显著高于 asset_*，符合『编译侧承接结构化事实』的定位。

### 3.4 分镜与视频编译层

- compile_storyboard_image：24 行 865 字符 20 占位符。P0-1（7bab27f）已恢复 plan_posture_block 注入，此前该占位符缺失导致站位信息静默丢失。
- compile_shot_video：11 行 811 字符 24 占位符，全库占位符最多、密度最高。经直读确认为纯结构性槽位模板（英文键值骨架，含 Scene / Reference bindings / Shot 1 / overall_soundscape / non_diegetic_music 等段），不含中文规则语句，故关键词矩阵为空属预期，不是缺失。
- video_prompt_first（1.1 版 28 行 484 字符）与 video_prompt_body（1.0 版 30 行 694 字符），相似度 0.769，差异全部落在 H3 首镜/续镜职责。
- image_prompt：2.0 版 17 行 551 字符 11 占位符，为 LLM 未产出 image_prompt 时的系统侧兜底渲染模板。

### 3.5 质检层（qc_*）

- qc_keyframe_prompt 5 行 327 字符，qc_clip_prompt 11 行 502 字符，均 json 输出、1 个占位符，短小聚焦，符合『单次判定 + 结构化输出』的定位。
- 二者均含 issues 与 fixed_prompt 的约定，允许整段重写但保留硬性格式。

### 3.6 会话、语音与导演层

- session_title_system 仅 1 行 23 字符，全库最薄；与 session_title_naming（10 行 169 字符、2 占位符）配合使用，整体职责闭合。
- voice_profile_system 1 行 482 字符、director_system 1 行 940 字符，均为单行超长文本，功能无问题，但 diff 与审阅友好度差。
- agent_intent_system 24 行 1,737 字符 json 输出，1 占位符，为意图路由的结构化输出模板，体量与职责匹配。

---

## 四、发现的问题与定性

| 编号 | 问题 | 客观证据 | 严重度 | 处置 |
| --- | --- | --- | --- | --- |
| Q-01 | script_create 与 script_expand 共享通用剧本格式条款，重复维护 | 相似度 0.616；diff 显示格式条款、第X集分隔、人物名一致、只输出正文 4 类重复 | 低 | 本轮不改（改动影响生成文本，需 A/B），列入 05 计划 |
| Q-02 | video_prompt_first / body 相似度 0.769 | diff 差异全部落在 Picture 1/2 职责 | 可接受 | 首镜/续镜属 H3 硬约束下的必要分叉，不合并 |
| Q-03 | 3 个模板为单行超长或过薄 | director_system 940 字符、voice_profile_system 482 字符、session_title_system 23 字符，行数均为 1 | 低（可维护性） | 格式化会改变模型输入，须 A/B，本轮不动 |
| Q-04 | compile_shot_video 占位符密度最高，缺省值完全依赖编译侧 | 24 占位符 / 11 行 | 中（可维护性） | 已由 test_prompt_registry 的 input_vars 与占位符一致性测试守护 |
| Q-05 | script_outline_system 含对白字数与每镜平均 10 秒估算表述 | 模板第 4、5 条 | 无冲突 | 该字段为预估元数据（dialogue_char_estimate / estimated_shots），不参与合格判定，未违反『禁止固定字数判断合格』硬约束 |
| Q-06 | 历史上 5 个模板存在内置与外置不一致 | P0-1、P0-3a~3d 共 5 项对齐提交 | 已修复 | 76a2770 三项防漂移测试守护 |
| Q-07 | compile_shot_video 无中文规则语句 | 关键词矩阵五项全空 | 无问题 | 该模板为结构性槽位模板，规则由编译侧 mapping 与 validate_compilation 保证 |

---

## 五、分层质量评级

评级仅依据客观指标（接口完整性、输出契约明确度、体量与职责匹配度、版本齐备），不使用主观印象：

| 层 | 模板 | 接口完整性 | 契约明确 | 体量匹配 | 综合 |
| --- | --- | --- | --- | --- | --- |
| 剧本理解 | parse_system, script_outline_system | 高 | 高（json 字段契约） | 高 | 优 |
| 剧本创作 | create, expand, normalize | 高 | 高 | 中（create/expand 偏薄） | 良 |
| 资产 | asset_*, compile_* 三件套 | 高（版本统一） | 高 | 高 | 优 |
| 分镜视频 | compile_storyboard_image, compile_shot_video, image_prompt, video_prompt_* | 高 | 中（compile_shot_video 规则不在模板内） | 高 | 良 |
| 质检 | qc_keyframe_prompt, qc_clip_prompt | 高 | 高（json） | 高 | 优 |
| 会话语音导演 | session_title_*, voice_profile_system, director_system | 高 | 中 | 低（单行超长 / 过薄） | 中 |

---

## 六、结论

1. 27 个模板全部有版本号、输出契约与真实调用方（0 孤儿，见 04 文档），接口层无缺失。
2. 质量问题集中在可维护性（Q-01、Q-03、Q-04）而非功能正确性；曾经造成实际功能缺陷的是 Q-06（内外置漂移）与 P0-1 的 plan_posture_block 静默缺失，均已在 7bab27f 至 e10cb5f 修复。
3. 本轮未删除任何模板：删模板必须证明零调用且无历史价值，当前 27 个均有调用方，7 个死模板有设计文档价值，一律保留。
4. 视觉与生成质量指标（人物一致性、道具连续性准确率）不在本文档口径内，需 10 类剧本实跑，见 06 文档。
