# 音频模块与 H3 VA 工作流审计报告（2026-11-06）

> 阶段一交付物。审计范围：前后端代码、ComfyUI workflow JSON、数据模型、任务系统、合成与导出链路。
> 原则：先审计、再设计、再实施；不破坏现有 H3 Ref2VA 多参考图生成流程。

## 1. 现状结论

### 1.1 H3 Ref2VA workflow（关键结论：属于文档 6.2 情况 1）

`backend/data/workflows/` 下三个 H3 Ref2VA 档位（std_5s / fast_5s / quality_10s）节点结构一致：

- [1] UNETLoader（H3 扩散模型）→ [35] LoRA（ref2v 4步 Turbo）
- [2] CLIPLoader（Qwen3-VL 文本编码器）
- [3] VAELoader（视频 VAE）+ [4] VAELoader（**音频 VAE**）
- [6] MiniMaxH3ReferenceToVideo（Ref2VA 主节点，9 参考图槽位）
- [11] SamplerCustomAdvanced（**音视频联合采样**）
- [12] LTXVSeparateAVLatent（**拆分音视频 latent**）
- [13] VAEDecode（视频）+ [14] VAEDecodeAudio（**音频**）
- [15] CreateVideo（24fps 合成）→ [16] SaveVideo（$output.clip）

meta 声明：`audio: true`、`audio_output_mode: embedded`、容器 mp4/h264。

**结论：workflow 原生输出内嵌音频的 mp4，无需为音频改造 workflow JSON。** 重点在后端检测、登记与轨道化管理。参考图槽位（ref_image_0~8）与首帧绑定保持不动。

### 1.2 已有音频 workflow

| 文件 | 类型 | 输出 | 说明 |
|---|---|---|---|
| wf_tts_qwen3_custom_voice.json | tts | $output.voice (mp3) | 9 预置音色，单说话人，emotion 自由文本指令 |
| wf_tts_qwen3_voice_clone.json | tts | mp3 | 音色克隆模式 |
| wf_bgm_stable_audio.json | bgm | $output.bgm (mp3) | Stable Audio，1-47s，立体声 |

### 1.3 后端已有能力（可复用）

- **枚举已预置**：ASSET_TYPE 含 audio；WORKFLOW_TYPE 含 tts/bgm/sfx；JOB_TYPE 含 generate_audio/generate_bgm/generate_sfx；AUDIO_SOURCE（h3_va/tts_fallback/none）。
- **Shot 模型已有音频字段**：audio_source、audio_check_status、dialogue_check_status、av_sync_delta、actual_duration、video_actual_duration、dialogue_segments（speaker/text/emotion/order）、soundscape、bgm。
- **executor**：clip 生成后 ffprobe 检测回写 audio_source（h3_va/none）与实际时长（clip.probe 事件）；voice/bgm/sfx 输出已登记为 audio 资产。
- **generate API**：POST /shots/{id}/generate 支持 target=audio/bgm/sfx，tts_text 默认取 shot.dialogue；已有幂等（request_key）、在途去重、取消。
- **compose**：ffprobe 音轨检测、h3_va/tts_fallback 双模式混音、BGM 循环+淡出+音量、两遍响度归一（loudnorm）、字幕烧录、音画对齐校验。
- **统一任务系统**：Job/JobEvent outbox/Task/TaskStage，SSE 事件流。
- **Character 模型**：已有 voice_id 字段（String(64)）。

### 1.4 前端现状

- 页面：ScriptView / ParseView / AssetsView / StoryboardView / StoryboardImagesView / ComposeView（合成与导出合并页）。
- 无音频工作台一级导航（符合要求，保持）。
- 音频 UI 几乎空白：仅 JobsView 有 generate_audio/bgm/sfx 任务类型标签。

## 2. 缺口清单（对照验收标准）

| # | 缺口 | 现状 | 目标 |
|---|---|---|---|
| G1 | 音频规划实体 | 解析仅产出 dialogue_segments 挂在 Shot 上；BGM/SFX/环境声仅有文本字段 | 结构化 audio_plan/audio_segment（对白/旁白/BGM/SFX/环境声），支持跨镜头 |
| G2 | 角色音色配置 | Character 仅 voice_id | voice_profile 完整配置（provider/model/voice_id/name/speed/emotion/is_default）+ 试听 |
| G3 | 音频审核 | Asset.quality_status 仅图片质量 | 音频版本审核记录（绑定 asset_id+version） |
| G4 | H3 原生音频管理 | audio_source 检测已有，compose 直接混入 h3_va | 原生音频作为独立轨道：待审核、启用/静音策略 |
| G5 | 多轨混音配置 | compose 仅 bgm_asset_id/bgm_volume 两参数 | 六轨（对白/旁白/BGM/SFX/环境声/原生）独立启用/音量/偏移/淡入淡出 |
| G6 | 旁白跨分镜 | 无旁白实体 | 旁白片段可跨镜头，时间轴不被分镜截断 |
| G7 | 前端音频 UI | 空白 | 剧本分析音频结果、角色音色、镜头音频面板、原生音频检测、合成轨道配置 |
| G8 | 音频时长校验 | compose 有音画对齐 | 对白超镜头时长进入人工审核而非截断 |

## 3. 复用决策

- **不新建**统一任务表：复用 Job/JobEvent（generate_audio/generate_bgm/generate_sfx 已在枚举）。
- **不新建**媒体资产表：复用 Asset（asset_type=audio，版本化/validity 已有）。
- **不改动** H3 workflow JSON：属于情况 1（内嵌音频），后端补检测与轨道管理。
- **新增**最小实体集：voice_profile、audio_segment、audio_review、mix_track_config（挂 Composition）。

## 4. 修改文件清单

### 后端
- app/models/enums.py：新增 TRACK_TYPE、AUDIO_REVIEW_STATUS、NATIVE_AUDIO_POLICY 等枚举
- app/models/audio.py（新）：VoiceProfile、AudioSegment、AudioReview
- app/models/narrative.py：Character 增加音色配置字段（或关联 voice_profile）
- app/models/production.py：Composition 增加轨道配置 JSON 字段
- app/models/ids.py：新增 ID 生成器
- alembic/versions/（新迁移）
- app/services/audio_plan.py（新）：音频规划生成与查询
- app/services/audio_mix.py（新）：多轨混音配置与 FFmpeg 命令构建
- app/worker/executor.py：clip 检测后登记原生音频轨道策略；audio 任务回写 segment
- app/worker/compose_executor.py：接入多轨混音配置
- app/api/v1/audio.py（新）：音频规划/片段/审核/试听 API
- app/api/v1/characters.py：音色配置 CRUD
- app/api/v1/compose.py：混音配置保存
- app/main.py：注册新路由

### 前端
- src/views/project/ParseView.vue：剧本分析页音频分析结果折叠区
- src/views/project/AssetsView.vue：角色音色配置与试听
- src/views/project/StoryboardView.vue：镜头详情音频面板
- src/views/project/StoryboardImagesView.vue：分镜视频原生音频检测展示（如该页含视频）
- src/views/project/ComposeView.vue：轨道配置、混音预览、状态提示
- src/api/（新）audio.ts、扩展 characters.ts/compose.ts
- src/types/（新）audio.ts

## 5. 风险与迁移计划

- R1：SQLite 迁移需向后兼容（现有库不重建），新增表与字段全部可空/带默认值。
- R2：compose 现有 h3_va 直接混入行为改为默认待审核，需保证旧 Composition 记录可读。
- R3：TTS workflow 单说话人限制：多人对白由后端按 dialogue_segments 分段调用拼接（meta notes 已注明）。
- R4：不破坏 Ref2VA 槽位：workflow JSON 零改动，仅后端读取 meta 的 audio 能力声明。
- R5：前端页面较大（StoryboardView 76KB），音频面板以独立子组件嵌入，避免整页重写。

## 6. 实施顺序

1. 阶段二：数据模型 + 迁移 + 音频规划服务 + 音频 API
2. 阶段三：executor/compose_executor 接入原生音频轨道与多轨混音（workflow JSON 不改）
3. 阶段四：前端五个页面嵌入音频 UI
4. 阶段五：测试与交付文档
