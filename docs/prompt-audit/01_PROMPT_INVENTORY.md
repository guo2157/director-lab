# 01. 提示词资产盘点清单

审计日期：2026-11-25
审计范围：全仓库（backend/app、backend/workspace、frontend/src、docs），关键词覆盖 prompt / system_prompt / system_message / instruction / template / compiler / agent / generation / storyboard / analysis / parsing / negative_prompt。
审计方法：以代码真实调用关系为准——凡未经 prompt_store 注册且未被 get_prompt 或直接引用消费的文本，均不认定为运行时模板。

---

## 一、提示词编译体系架构

### 1.1 PromptStore（app/services/prompt_store.py）

- 模板目录：backend/workspace/prompts/*.md，文件名即模板名。
- 读取策略（get_prompt）：外置文件优先，缺失时回退 DEFAULT_PROMPTS 内置默认值。
- 写入策略（write_prompt）：写入前自动备份为 {name}.{时间戳}.bak，最多保留 5 份。
- 注册机制（P0-4 修复后）：ensure_registered() 集中惰性导入全部注册模块，list_prompts / get_prompt / write_prompt / reset_prompt 四入口统一调用，行为不再依赖模块 import 时机。
- 模板缓存：无缓存，每次调用重新读文件，修改即时生效，不存在修改不生效问题。

### 1.2 PromptCompiler（app/services/prompt_compiler.py）

- 阶段 B 编译器：把『结构化事实 + 模板 + 已审核参考图 + 工作流真实能力』编译为 positive_prompt / negative_prompt / reference_slots / generation_constraints。
- 模板注册表 TEMPLATE_REGISTRY（5 个编译模板，带 version 写入任务快照）：character_asset_prompt v2 / scene_asset_prompt v2 / prop_asset_prompt v2 / storyboard_image_prompt v3 / shot_video_prompt v4。
- render_template：仅做模板侧占位符校验（模板声明的字段必须被 mapping 覆盖，缺字段显式报错），不反向探测渲染结果中的花括号（用户数据可合法含 {}）。
- 负向词（P1-2 统一后单一事实源）：negative_for(type) = 通用层 COMMON + 类型层（_NEGATIVE_BY_TYPE）+ 历史层（_LEGACY_NEGATIVE），merge_negative 按分隔符切分去重后一次下发。
- 输出必须经 validate_compilation 校验后才允许提交生成任务。

### 1.3 PromptQC（app/services/prompt_qc.py）

- 规则质检（check_keyframe_prompt / check_clip_prompt 等）：站位、道具可见性、道具数量一致性、前置状态连续性、提示词结构。
- LLM 质检（llm_audit_keyframe_prompt / llm_audit_clip_prompt）：使用 qc_keyframe_prompt / qc_clip_prompt 模板（P1-1 已注册），输出 {ok, issues, fixed_prompt}，允许整段重写但保留硬性格式。
- 参考图职责回写：relabel_reference_block 与 _DUTY_LINE 同口径维护。

---

## 二、运行时注册模板清单（27 个）

说明：「占位符」列为模板真实 format 变量；标注〔JSON 示例〕的花括号是输出格式示例，运行时不做 format、数据经 user 消息注入。hash 为 2026-11-25 审计时点 sha256 前 8 位。

### A. 剧本理解与生成（第一层）

| prompt_id | 外置 | hash | 长度 | 调用入口 | 输出格式 |
|---|---|---|---|---|---|
| script_create_system | 否 | 7c1c6295 | 296 | script_writing.py（create 模式）、video_workflow.py generate_script_from_materials | 自由文本剧本 |
| script_expand_system | 否 | 7cef1d78 | 266 | script_writing.py（expand 模式） | 自由文本剧本 |
| script_normalize_system | 否 | 55b5ab4f | 1214 | script_writing.py normalize | 自由文本剧本 |
| script_evaluation_system | 是 | 91d9386e | 2048 | script_evaluation.py evaluate_script（json_mode + parse_json_safely） | JSON（评估维度 + 可制作性五维） |
| script_outline_system | 否 | 8e1c8327 | 413 | script_parse.py analyze_script_outline、script_outline_segmented.py | JSON 分集大纲 |

### B. 剧本结构化解析（第二层）

| prompt_id | 外置 | hash | 长度 | 调用入口 | 占位符 | 输出格式 |
|---|---|---|---|---|---|---|
| parse_system | 是 | 4b77c330 | 4618 | worker/parse_executor.py、script_agent.py | 无（剧本经 user 消息注入） | JSON（角色/场景/道具/镜头全量结构化） |

### C. 资产提示词（第三层）

| prompt_id | 外置 | hash | 长度 | 调用入口 | 占位符 |
|---|---|---|---|---|---|
| asset_character | 是 | 71dfee7a | 351 | asset_prompts.build_character_prompt | name, identity, style |
| asset_scene | 是 | 35b2f2aa | 462 | asset_prompts.build_scene_prompt | name, identity, style |
| asset_prop | 是 | 253caa54 | 279 | asset_prompts.build_prop_prompt | name, identity, style |
| compile_character_asset | 否 | f75b890c | 797 | prompt_compiler.render_template（TEMPLATE_CHARACTER v2） | name, species, age_appearance, face_and_head, hair_or_head, body_shape, clothing, fixed_accessories, main_colors, distinctive_features, style |
| compile_scene_asset | 是 | 1c341378 | 840 | prompt_compiler.render_template（TEMPLATE_SCENE v2） | name, time_and_weather, empty_expr, foreground, midground, background, lighting, camera_shot, texture, palette, identity, standable_items, fixed_elements, excluded_props, style |
| compile_prop_asset | 否 | 08c0eaf1 | 661 | prompt_compiler.render_template（TEMPLATE_PROP v2） | name, identity, shape, color, material, relative_scale, distinctive_features, style |
| scene_plate_compiler | 是 | fe90fa7c | 1847 | scene_plate.py _call_llm | title, description, style, location_bible, aspect（其余为 JSON 示例） |

### D. 分镜图片（第四、五层）

| prompt_id | 外置 | hash | 长度 | 调用入口 | 占位符 |
|---|---|---|---|---|---|
| image_prompt | 是 | 6205bc91 | 551 | script_parse.py 镜头级分镜图提示词渲染 | episode, seq, shot_purpose, beats, character_id, props, emotion, shot_type, camera_angle, camera_movement, start_state |
| compile_storyboard_image | 是 | 9e1d93c8 | 865 | prompt_compiler.render_template（TEMPLATE_STORYBOARD v3） | shot_id, aspect_desc, ref_duty_block, scene, action_moment, plan_posture_block, position_specs, opening_blocking, standable_items, gaze_targets, prop_interaction, emotion, shot_size, camera_angle, composition, camera_movement, lighting, character_count_rule, props, style |

### E. 分镜视频（第六层）

| prompt_id | 外置 | hash | 长度 | 调用入口 | 占位符 |
|---|---|---|---|---|---|
| video_prompt_first | 是 | d4c45855 | 484 | script_parse.py（首镜视频提示词） | episode, seq, start_state, shot_purpose, beats, camera_movement, end_state, sound, duration |
| video_prompt_body | 是 | 688a870e | 694 | script_parse.py（后续镜视频提示词） | 同 video_prompt_first |
| compile_shot_video | 是 | 655df301 | 811 | prompt_compiler.render_template（TEMPLATE_VIDEO v4） | scene, character_count, props, style, continuity_lock, ref_bindings, spatial_lock, position_specs, screen_geography, shot_size, camera_angle, camera_movement, start_state, main_action, end_state, pacing, environment_changes, continuity_in_type, source_shot_id, source_frame_file_id, continuity_out_state, dialogue, soundscape, music |

### F. Agent 与质检（第七层）

| prompt_id | 外置 | hash | 长度 | 调用入口 | 说明 |
|---|---|---|---|---|---|
| director_system | 否 | 2e799f97 | 940 | api/v1/agent.py chat（搜索路径与主链路两处） | 导演台系统提示词：导航确认/搜索纪律/任务状态纪律 |
| agent_intent_system | 否 | 848ea15a | 1737 | intent_router.py chat_json | 意图识别，输出 JSON 分类（intent/subcategory/goal/risk 等） |
| agent_session_summary | 否 | dc7f1c7e | 180 | session_memory.py chat | 会话长期记忆摘要压缩 |
| qc_keyframe_prompt | 否 | 058ffdb9 | 327 | prompt_qc.llm_audit_keyframe_prompt | 分镜图提示词质检（站位/道具引用），输出 {ok, issues, fixed_prompt} |
| qc_clip_prompt | 否 | 5c6cad4b | 502 | prompt_qc.llm_audit_clip_prompt | 分镜视频提示词质检（角色一致/道具连续/首帧衔接等五维） |
| session_title_system | 否 | 85e8d56f | 23 | session_title.py chat | 会话标题生成系统提示词 |
| session_title_naming | 否 | 0894be55 | 169 | session_title.py（.format 注入 user/assistant） | 会话标题命名用户模板 |
| voice_profile_system | 否 | 8e573010 | 482 | voice_matching.py chat_json | 角色声音画像分析，输出结构化 JSON |
| episode_continue_system | 否 | deacc3f9 | 686 | series/episode_writing_service.py chat_json | 系列剧集续写（儿童成长类），输出 JSON |

---

## 三、未注册的内联提示词（2 处，均经评估不适合注册）

| 位置 | 内容 | 不注册原因 |
|---|---|---|
| keyframe_image_qc.py _build_image_qc_prompt | 视觉质检系统提示词 | 运行时动态拼接：站位线索（plan_posture_block）与构图要求随镜头实时变化，静态注册后仍需大量 format 占位，收益低 |
| video_workflow.py generate_script_from_materials / revise_script 的 user 文案 | 网页资料创作/按建议修改剧本 | 一次性临时 user 消息文案，非系统提示词，且 style/orientation 动态拼接 |

## 四、死模板（7 个，docs/prompts/prop/）

11-07 提交 57284e9 的设计阶段 LLM 方案文档；审计确认 prop_lifecycle.py / prop_validator.py / bindings.py 无任何 LLM 调用，功能以纯代码（正则匹配+状态机）落地。已添加 docs/prompts/prop/README.md 标记状态并建立与代码实现位置的映射（提交 b78c1dc）。

---

## 五、调用链总览

- 用户/API → worker/parse_executor、script_agent → parse_system → LLM（结构化 JSON）→ normalize_parse_output → 落库
- 用户/API → script_evaluation → script_evaluation_system → JSON 评估
- 用户/API → script_writing / script_outline_segmented → script_create/expand/normalize/outline_system → 文本/JSON
- 资产计划 → asset_prompts.build_*（asset_character/scene/prop）→ 资产图提示词；→ prompt_compiler.compile_asset_image（compile_*_asset v2）→ 校验 → Job
- 分镜 → script_parse.image_prompt / prompt_compiler.compile_storyboard_image（v3）→ prompt_qc 规则+LLM 质检（qc_keyframe_prompt）→ Job
- 视频 → script_parse.video_prompt_first/body / prompt_compiler.compile_shot_video（v4）→ prompt_qc 质检（qc_clip_prompt）→ H3 工作流 Job
- Agent → api/v1/agent.py（director_system）→ intent_router（agent_intent_system）→ session_memory（agent_session_summary）→ session_title（2 个模板）
- 声音 → voice_matching（voice_profile_system）→ TTS 参数
- 系列 → episode_writing_service（episode_continue_system）→ 续写 JSON

---

## 六、第一阶段十个问题的审计结论

1. 实际运行时优先读取哪个模板？外置 workspace/prompts/{name}.md 优先于内置默认。
2. 外置模板缺失时如何回退？回退 DEFAULT_PROMPTS 内置默认；P0-2 前 scene_plate_compiler 无内置默认（缺失即 FileNotFoundError），已修复。
3. 内置模板和外置模板是否一致？P0-3 修复前 5 个漂移，现已全部一致，并由 P2-2 一致性测试强制。
4. 是否存在多个模板同时注入同一规则？负向词曾双处定义（P1-2 已收编）；正向模板无此问题。
5. 是否有变量未替换成功？render_template 模板侧校验保证不缺字段；compile_storyboard_image 曾因外置旧版丢失 {plan_posture_block} 导致站位信息静默不注入（不报错），P0-1 已修复。
6. 是否存在空值被拼接进提示词？_text / _describe_state 等提供『未提供』兜底；_prune_empty_fields 清理空字段；test_prompt_empty_fill 覆盖。
7. 是否存在相同内容重复注入？merge_negative 按词去重；正向未发现重复注入。
8. 是否存在旧模板仍然被调用？P0-3 修复前 video_prompt_first 外置旧版在运行时生效（丢失 11-14 H3 修复禁止词），已修复。
9. 模板修改后是否能够追踪历史任务？编译模板版本号写入 job.input_snapshot；外置写入自动 .bak 备份（最多 5 份）；content_hash 经 list_prompts 可查。
10. 模板缓存是否会导致修改不生效？无缓存，get_prompt 每次重读文件，即时生效。

---

## 七、P0/P1 修复记录（本审计的直接产出）

| 提交 | 内容 |
|---|---|
| 7bab27f | P0-1 compile_storyboard_image 外置对齐，恢复 plan_posture_block 注入 |
| 69210d5 | P0-2 scene_plate_compiler 注册内置默认 |
| 62fe6c4 | P0-3a parse_system 双向合并对齐（on_screen/offscreen 字段 + beats 手别等三约束） |
| e52d5df | P0-3b image_prompt 内置回写至 11-22 单帧化新版 |
| d36cfd7 | P0-3c video_prompt_first 外置恢复 H3 修复禁止词 |
| e10cb5f | P0-3d script_evaluation_system 内置回写含可制作性字段的 11-03 版 |
| 8bd460c | P0-4 prompt_store 集中注册 ensure_registered，修复惰性注册 |
| 8e2ad4c | P1-1 注册 9 个静态内联提示词（注册总量 18 → 27） |
| 72fc8c2 | P1-2 负向词单一事实源（legacy 收编 + negative_for_asset_type 委托） |
| b78c1dc | P1-3 docs/prompts/prop 死模板定性 README |
| 76a2770 | P2-2 内置/外置一致性防漂移测试（3 项） |

回归状态：全量 1394 项测试通过（P0-4 时点）；各阶段相关测试子集均绿。
