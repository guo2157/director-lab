# 04. 提示词调用图

审计日期：2026-11-25
审计方法：全仓 grep 定位全部 get_prompt 调用点，逐处直读调用上下文；对动态派发点回溯取值集合
取证命令：`grep -rn 'get_prompt(' app --include=*.py`（排除 def 定义），共 29 行命中

---

## 一、读取链路总览

所有业务模板最终都经由同一条读取链路，这是本项目的核心架构事实：

```
业务代码 get_prompt(name)
        ↓
app/services/prompt_store.py :: get_prompt(name)
        ↓
    外置优先：backend/workspace/prompts/{name}.md
        ↓ 缺失时回退
    DEFAULT_PROMPTS 内置默认值
```

关键特性（01 文档已记录，此处重申与调用图的关系）：

- 无缓存，每次调用重新读文件，模板热更新即时生效，不存在陈旧读。
- 因此调用图描述的是代码到模板名的映射关系，而非模板内容的加载路径。

---

## 二、全部调用点清单

共 29 处 get_prompt 命中，其中业务调用 24 处、框架与运维调用 4 处、二选一表达式 1 处（覆盖 2 个模板）。

### 2.1 业务静态调用（23 处）

| # | 调用位置 | 模板 | 业务语义 |
| ---: | --- | --- | --- |
| 1 | app/services/intent_router.py:547 | agent_intent_system | 意图路由结构化判定 |
| 2 | app/services/session_memory.py:95 | agent_session_summary | 会话记忆摘要 |
| 3 | app/services/script_agent.py:672 | parse_system | 剧本解析 agent 主控 |
| 4 | app/worker/parse_executor.py:219 | parse_system | 解析任务异步执行 |
| 5 | app/services/script_outline_segmented.py:148 | script_outline_system | 分段大纲生成 |
| 6 | app/services/script_parse.py:105 | script_outline_system | 解析前大纲 |
| 7 | app/services/prompt_qc.py:240 | qc_keyframe_prompt | 关键帧提示词 LLM 质检 |
| 8 | app/services/prompt_qc.py:859 | qc_clip_prompt | 视频片段提示词 LLM 质检 |
| 9 | app/services/series/episode_writing_service.py:116 | episode_continue_system | 多集续写 |
| 10 | app/services/script_writing.py:304 | script_normalize_system | 剧本规范化 |
| 11 | app/services/voice_matching.py:119 | voice_profile_system | 音色配置生成 |
| 12 | app/services/script_evaluation.py:217 | script_evaluation_system | 剧本可制作性评估 |
| 13 | app/services/scene_plate.py:100 | scene_plate_compiler | 场景空镜编译 |
| 14 | app/services/script_parse.py:723 | image_prompt | 分镜图提示词系统侧兜底渲染 |
| 15 | app/services/asset_prompts.py:92 | asset_character | 角色资产提示词 |
| 16 | app/services/asset_prompts.py:110 | asset_scene | 场景资产提示词 |
| 17 | app/services/asset_prompts.py:130 | asset_prop | 道具资产提示词 |
| 18 | app/services/video_workflow.py:274 | script_create_system | 网页创作入口复用剧本创作规则 |
| 19 | app/services/session_title.py:75 | session_title_naming | 会话命名 |
| 20 | app/services/session_title.py:82 | session_title_system | 会话标题系统规则 |
| 21 | app/api/v1/agent.py:1639 | director_system | 导演 agent 多轮对话 |
| 22 | app/api/v1/agent.py:1885 | director_system | 导演 agent 单轮入口 |
| 23 | app/services/script_parse.py:676 | video_prompt_first / video_prompt_body | 变量派发（见第三节 3.1） |

### 2.2 框架与运维调用（4 处，非业务语义）

| # | 调用位置 | 说明 |
| ---: | --- | --- |
| 24 | app/services/prompt_store.py:69 | 内部自调用 |
| 25 | app/services/prompt_registry.py:68 | template_input_vars 解析占位符时读取 |
| 26 | app/services/prompt_registry.py:81 | template_content_hash 计算指纹时读取 |
| 27 | app/api/v1/settings.py:404 | 管理后台读取模板内容供编辑（经 prompt_store 前缀调用） |

这 4 处不承载业务语义，不产生对模型的调用。

### 2.3 二选一表达式（1 处，覆盖 2 个模板）

```python
system = get_prompt('script_create_system' if mode == 'create' else 'script_expand_system')
```

位于 script_writing.py:323。mode 由调用方传入，create 走创作规则、expand 走扩写规则，两者不共存。

---

## 三、动态派发点（2 处）

全库只有 2 处通过变量间接取模板名，必须回溯其取值集合才能确认覆盖度。

### 3.1 视频提示词首镜/续镜二选一

```python
template_name = 'video_prompt_first' if is_first else 'video_prompt_body'
template = get_prompt(template_name)
```

位置：app/services/script_parse.py:675-676
取值集合：video_prompt_first 与 video_prompt_body，由 is_first 布尔值决定，无第三个分支。
与 H3 规则一致：首镜 Picture 1 为当前分镜图；续镜 Picture 1 为上一镜尾帧、Picture 2 为当前分镜图。

### 3.2 编译侧模板注册表派发

```python
meta = TEMPLATE_REGISTRY.get(template_id)
template = get_prompt(meta['prompt_name'])
```

位置：app/services/prompt_compiler.py:500（render_template 内）
取值集合：TEMPLATE_REGISTRY 五个条目的 prompt_name 字段，直读确认（第 46/48/50/52/54 行）：

| template_id | prompt_name | 版本 |
| --- | --- | --- |
| character_asset_prompt | compile_character_asset | 2 |
| scene_asset_prompt | compile_scene_asset | 2 |
| prop_asset_prompt | compile_prop_asset | 2 |
| storyboard_image_prompt | compile_storyboard_image | 3 |
| shot_video_prompt | compile_shot_video | 4 |

该派发点是资产与镜头提示词的唯一编译入口，调用方为 prompt_compiler 的 compile_asset_image / compile_storyboard_image / compile_shot_video 三个函数，最终由 app/api/v1/generate.py 的生成接口消费，并通过 _attach_compilation 把 template_id、template_version、template_content_hash 写入 job.input_snapshot 供历史追溯。

---

## 四、27 个模板的调用方覆盖表

| 模板 ID | 调用方 | 派发方式 | 覆盖 |
| --- | --- | --- | --- |
| agent_intent_system | intent_router.py:547 | 静态 | 是 |
| agent_session_summary | session_memory.py:95 | 静态 | 是 |
| asset_character | asset_prompts.py:92 | 静态 | 是 |
| asset_scene | asset_prompts.py:110 | 静态 | 是 |
| asset_prop | asset_prompts.py:130 | 静态 | 是 |
| compile_character_asset | prompt_compiler.py:500 | 动态 | 是 |
| compile_scene_asset | prompt_compiler.py:500 | 动态 | 是 |
| compile_prop_asset | prompt_compiler.py:500 | 动态 | 是 |
| compile_storyboard_image | prompt_compiler.py:500 | 动态 | 是 |
| compile_shot_video | prompt_compiler.py:500 | 动态 | 是 |
| image_prompt | script_parse.py:723 | 静态 | 是 |
| video_prompt_first | script_parse.py:676 | 动态 | 是 |
| video_prompt_body | script_parse.py:676 | 动态 | 是 |
| scene_plate_compiler | scene_plate.py:100 | 静态 | 是 |
| parse_system | script_agent.py:672、parse_executor.py:219 | 静态 | 是 |
| script_outline_system | script_outline_segmented.py:148、script_parse.py:105 | 静态 | 是 |
| script_create_system | script_writing.py:323、video_workflow.py:274 | 静态 + 三元 | 是 |
| script_expand_system | script_writing.py:323 | 静态（三元） | 是 |
| script_normalize_system | script_writing.py:304 | 静态 | 是 |
| script_evaluation_system | script_evaluation.py:217 | 静态 | 是 |
| qc_keyframe_prompt | prompt_qc.py:240 | 静态 | 是 |
| qc_clip_prompt | prompt_qc.py:859 | 静态 | 是 |
| director_system | agent.py:1639、agent.py:1885 | 静态 | 是 |
| voice_profile_system | voice_matching.py:119 | 静态 | 是 |
| session_title_system | session_title.py:82 | 静态 | 是 |
| session_title_naming | session_title.py:75 | 静态 | 是 |
| episode_continue_system | episode_writing_service.py:116 | 静态 | 是 |

覆盖结论：27 / 27 全部有真实业务调用方，零孤儿模板。该结论已由 tests/test_prompt_registry.py 的 test_registry_has_no_orphan_entries 自动守护。

---

## 五、业务链路视图

按用户操作触发顺序串联，便于定位故障时该查哪个模板：

| 链路阶段 | 触发入口 | 涉及模板 |
| --- | --- | --- |
| 用户输入理解 | 对话入口 | agent_intent_system（意图路由）、director_system（导演对话） |
| 剧本创作 | 创建 / 扩写 / 规范化 | script_create_system、script_expand_system、script_normalize_system |
| 剧本结构分析 | 大纲、解析 | script_outline_system、parse_system |
| 剧本评估 | 可制作性评估 | script_evaluation_system |
| 多集续写 | 剧集续写 | episode_continue_system |
| 资产提示词 | 角色 / 场景 / 道具 | asset_character、asset_scene、asset_prop |
| 资产图编译 | 提交生成 | compile_character_asset、compile_scene_asset、compile_prop_asset |
| 场景空镜 | 空镜生成 | scene_plate_compiler |
| 分镜图 | 分镜提示词 | image_prompt（兜底渲染）、compile_storyboard_image（编译，含 plan_posture_block） |
| 视频提示词 | 分镜转视频 | video_prompt_first（首镜）、video_prompt_body（续镜） |
| 视频编译 | 提交生成 | compile_shot_video |
| 质检 | 提示词质检 | qc_keyframe_prompt、qc_clip_prompt |
| 会话运维 | 标题、记忆 | session_title_naming、session_title_system、agent_session_summary |
| 音色 | 配音配置 | voice_profile_system |

---

## 六、未注册的内联提示词（2 处，评估后不注册）

| 位置 | 内容 | 不注册理由 |
| --- | --- | --- |
| app/services/keyframe_image_qc.py :: _build_image_qc_prompt | 动态拼接站位线索的质检提示词 | 内容由运行时数据动态拼装，无法作为静态模板维护；固定部分已由 qc_keyframe_prompt 承担 |
| app/services/video_workflow.py | 网页创作 / 改稿的一次性 user 文案 | 一次性消息，非可复用规则，改造为模板会引入无收益的间接层 |

这两处已在 01 文档记录，本次调用图复核维持原判断。

---

## 七、死模板（7 个，不注册）

位置：docs/prompts/prop/
性质：11-07 设计期文档。对应功能已由纯代码正则 + 状态机落地（道具生命周期由 prop_lifecycle.py 实现），无运行时读取。
定性提交：b78c1dc（README 说明）。
处置：保留为设计史料，不删除、不注册。

---

## 八、结论

1. 27 个注册模板全部有真实业务调用方，孤儿模板数量为 0，且由测试守护防止回归。
2. 动态派发仅 2 处，取值集合封闭且可枚举（2 个视频模板、5 个编译模板），不存在运行时拼出未知模板名的路径。
3. 读取链路唯一（prompt_store.get_prompt），无旁路读取，这是 P0-4（8bd460c）集中注册修复后的稳定形态。
4. 未注册的 2 处内联提示词与 7 个死模板均有明确定性，不计入运行时资产。
5. 故障定位检索路径：从用户操作入口按第五节链路表查到阶段，再到第二节调用点表查到文件行号，最后按模板 ID 在 02 文档查指标。
