# 角色智能音色匹配与音频生产管线 — 阶段 1 审计报告

日期：2026-11-07
范围：/home/ubuntu/director-lab（backend + frontend + docs）
关联：docs/agent/audio-module-audit-report-20261106.md、audio-module-delivery-20261106.md、audio-decoupling-increment-20261106.md

## 一、结论摘要

项目已于 2026-11-06 完成音频模块与 H3 解耦改造（VoiceProfile / AudioSegment / AudioReview 三表、10+ 条 /api/audio 路由、TTS/BGM Worker 回写链路、tts_mix 合成分支、5 个前端音频组件）。本次需求与现有实现重叠度高，核心差距集中在：系统级音色库元数据、LLM 声音画像与全局匹配算法、音色确认驱动的调度闸口、音频生产进度聚合 API、导演台音频进度区、角色列表批量音色操作。

## 二、可复用（不重建）

| 模块 | 现状 | 结论 |
|---|---|---|
| Job / Worker / Scheduler | Job 表含 job_type=generate_audio/generate_bgm/generate_sfx、request_key 幂等、attempts 重试、priority、input_snapshot；scheduler 已路由音频类型；executor._sync_audio_segment_after_generate 已回写资产/ffprobe 时长/状态/试听缓存 | 直接复用 |
| 9 个真实 speaker_id | wf_tts_qwen3_custom_voice.meta.json voice_values：男 5（Uncle_Fu/Dylan/Eric/Ryan/Aiden）、女 4（Vivian/Serena/Ono_Anna/Sohee），含性别/展示名/描述/emotion_map；GET /api/audio/speakers 已按 meta 输出 | 元数据唯一来源，禁止硬编码与虚构 |
| TTS 调用链 | executor → ComfyUI wf_tts_qwen3_custom_voice（$tts_text/$tts_voice/$tts_lang/$tts_emotion/$seed）；meta.supports.tts_speed=false，emotion 为 free_text_instruct | 按 meta.supports 适配，不假设未验证参数 |
| BGM 生成 | wf_bgm_stable_audio.json（1-47s 立体声）已部署 | 复用 |
| SFX 生成 | 无专用 workflow | Provider 抽象 + 素材库兜底 + 导演台显示未配置 |
| 音频规划 | audio_plan.build_audio_plan 从 Shot 确定性生成 AudioSegment（dialogue/narration/bgm/sfx/ambient），同 parse 版本幂等重建 | 复用，增强音色绑定闸口 |
| 音色试听 | POST /voices/{id}/preview + preview_asset_id/preview_text/preview_signature 三重缓存指纹 | 复用 |
| 合成链路 | compose.build_timeline tts_mix 分支、_collect_tts_audios（sequence→created_at 排序 + offset_ms 防重叠）、H3 原生音轨策略（V1 discard） | 复用，不改 H3 workflow |
| 角色模型 | Character（项目级资产层，normalized_name 跨集去重，含 traits/description/voice_id 预留字段） | 复用，音色绑定走 VoiceProfile |
| 前端基础 | VoiceConfigDrawer / AudioPlanPanel / ShotAudioPanel / AudioTracksPanel / AudioPolicyPanel；src/api/audio.ts 13 函数 | 复用并扩展 |
| 项目音频策略 | Project.settings.audio_config（tts_enabled/native_audio_policy/bgm_enabled/sfx_enabled/disabled_speakers），非法值 422 | 复用 |

## 三、需新增 / 改造

### N1 系统音色库元数据（阶段 2）
- 现状：speakers 从 workflow meta 动态读取，无结构化系统表（缺听感年龄/音调/音色特点/性格标签/说话风格/启用状态/排序的持久化）。
- 方案：新增 VoiceLibrary 系统级表（全局，不隶属项目），初始化数据由 meta.json 生成，speaker_id 全部取自 voice_values；元数据缺项（听感年龄等）以 meta.voice_descriptions 为准推导，不虚构；GET /api/v1/voices 读表；与现有 disabled_speakers 项目级禁用逻辑兼容。

### N2 角色音色绑定增强（阶段 2）
- 现状：VoiceProfile 已有 approved/is_default/preview 缓存，缺 match_score/match_reason/alternatives/is_locked/source（ai/manual/inherited）。
- 方案：VoiceProfile 增列（Alembic 沿用现有 versions 链），一个角色仅一条当前绑定（is_default 唯一性应用层保证已有）；锁定后自动匹配跳过。

### N3 LLM 声音画像 + 评分 + 全局分配（阶段 4）
- 全新能力：LLM 产出结构化 voice_profile 画像 → 程序按权重评分（性别 20 / 年龄 20 / 音色 25 / 性格 20 / 风格 15）→ 全局分配（主角优先、重要配角区分、次要复用、群众复用、锁定排除、>9 角色允许复用）。分数由程序计算，LLM 不决定分数、不创造 speaker_id。

### N4 音色确认调度闸口（阶段 5/6）
- 现状：AudioSegment.generation_status 枚举无 waiting_voice_confirmation；对白片段与角色音色确认之间无强制闸口，确认后无自动触发调度检查的钩子。
- 方案：新增枚举值；音色确认（approved）后触发该角色对白片段调度检查；scheduler 扫描条件增加『音色已确认且未锁定冲突』判定；幂等键沿用 request_key（project+target+version）。

### N5 音频生产进度 API + 导演台（阶段 7）
- 现状：progress.py 仅 asset/keyframe/clip 三类进度。
- 方案：新增 GET /api/v1/projects/{id}/audio/progress（dialogue/narration/bgm/sfx 分组实时统计）+ GET /audio/segments 明细分页（episode/character/shot/type/status 过滤）；导演台新增音频生产进度区，复用现有 SSE 事件机制。

### N6 前端增强（阶段 3）
- 角色列表：音色状态列 + 批量操作（匹配/试听/确认/重匹配，锁定保护与二次确认）。
- 角色详情：音色配置区扩展（当前音色/匹配原因/推荐候选/高级信息：speaker_id、voice_id、匹配方式、是否人工修改、是否锁定）。
- 系统音色库管理页：项目设置 → 音频设置 → 默认音色库（9 音色卡片 + 试听 + 启用状态，管理员维护）。
- 批量自动匹配结果页：逐角色试听/确认/更换 + 全部确认。

### N7 续集继承（阶段 2）
- Character 项目级 + normalized_name 跨集去重已具备；需落地规则：已绑定不重匹配、锁定绝不覆盖、同名不自动合并（以稳定 character_id 为准）、原音色被禁用时提示用户确认更换。

## 四、约束确认

- Alembic 迁移沿用现有 versions 链（当前 head：b2c3d4e5f6a8）。
- 不修改 H3 workflow；TTS 不进 H3、不占参考图槽位。
- 前端接口扩展沿用 src/api/audio.ts 既有模式。
- speaker_id 唯一来源：wf_tts_qwen3_custom_voice.meta.json 的 voice_values。
- 保留现有剧本/资产/分镜/视频/导出流程，不破坏既有功能。

## 五、实施顺序

按需求文档阶段 2 → 8 顺序实施：音色库与绑定 → 前端音色配置 → LLM 智能匹配 → AudioPlanningAgent 增强 → 音频生产管线 → 导演台集成 → 音画合成。每阶段完成后执行对应测试并报告实际完成情况。
