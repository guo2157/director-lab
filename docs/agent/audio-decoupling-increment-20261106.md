# TTS 与 H3 完全解耦方案 — 增量改造交付（2026-11-06）

## 一、方案定夺

确定采用 TTS 与 H3 完全解耦：TTS 不进 H3 workflow、不占参考槽位，最终由 FFmpeg 合成汇合。

声音参考协同方案挂起为 V2 探索项：阶段一审计已确认 H3 Ref2VA 节点链（[4]音频VAE→[11]联合采样→[12]LTXVSeparateAVLatent→[14]VAEDecodeAudio→[15]CreateVideo→[16]SaveVideo）无音频参考输入接口，需先在真实 ComfyUI 验证 ref_audios 端口存在性。

## 二、缺陷修复

### P1：speakers 取错 workflow（高）

原实现按 created_at 取第一条 tts workflow，可能取到 voice_clone（无 voice_values，返回空列表）。修复为遍历 tts workflow，按 capabilities.voice_mode == 'preset' 且 voice_values 非空选取。

### P2：regenerate 去重粒度错误（高）

原实现按 shot_id + job_type 去重，同镜头多对白片段互相误判 409，shot_id 为 None 时去重失效。修复为按 input_snapshot.audio_segment_id 粒度去重。

### 附带修复：Job.created_at 缺失（阻断级）

Job.created_at 为 nullable=False 且无默认值，regenerate_segment 与 preview_voice 创建 Job 时未赋值，导致片段重新生成与音色试听在数据库层 IntegrityError 失败。已新增 _now_naive() 辅助函数并两处赋值。此为既存缺陷，因无测试覆盖未暴露。

## 三、项目级音频策略

落点 Project.settings.audio_config（JSON 列，无需迁移），字段：tts_enabled / tts_provider / tts_model / native_audio_policy / bgm_enabled / sfx_enabled。

API：GET 与 PUT /api/audio/projects/{id}/audio-config，native_audio_policy 仅接受三档枚举，非法值 422。

策略语义：discard（V1 默认）丢弃 H3 原生音轨，有 TTS 片段按时间轴混入、无片段补静音；preserve 保持原有行为；preserve_ambience_only 因 V1 无可靠人声分离能力按 discard 处理。

## 四、合成链路改造

compose.py：ShotInput 新增 tts_audios 与 drop_native_audio；build_timeline 新增 tts_mix 分支，以对白总时长为镜头目标时长权威依据，超长延长镜头并记 warning，不截断不加速；shot_segment_cmd 新增 tts_mix 混音链（aresample 统一采样率与声道 → adelay 定位 → amix normalize=0 求和 → atrim/apad 对齐），不映射 [0:a]；h3_va 分支增加 not drop_native_audio 条件，discard 下落入静音分支。

compose_executor.py：新增 _collect_tts_audios，收集已完成 dialogue/narration 片段，按 sequence → created_at 排序，offset_ms 顺序累加保证不重叠，跳过时长未知片段；_to_shot_input 增加 audio_policy 参数；execute_compose_job 读取 Project.settings.audio_config.native_audio_policy 并逐镜传入。

未改动：BGM 混音、响度归一（-16 LUFS / -1 dBTP 两遍法）、字幕生成与烧录、渲染后自检、跨集连续性处理。

## 五、前端改造

src/types/audio.ts 新增 NativeAudioPolicy、AudioConfig 类型与 NATIVE_AUDIO_POLICY_OPTIONS；src/api/audio.ts 新增 getAudioConfig / setAudioConfig 与 getAudioAssetUrl（复用 /api/assets/{id}/file）；新建 src/components/AudioPolicyPanel.vue（TTS 开关、原生音轨策略单选带说明、BGM/SFX 开关，改动即时持久化）；ComposeView.vue 挂载该面板；AudioTracksPanel.vue 移除本地 ref 假策略（原 pending_detection/keep/mute 三档与后端无关联），改为读写项目级持久化配置。

ShotAudioPanel.vue 重写补齐三个操作缺口：① 已生成片段内嵌 audio 控件试听播放（src 指向资产文件服务端点）；② 单句语气指令 instruct 与 emotion 编辑（按片段 id 独立缓冲，regenerate 时随 payload 提交，覆盖角色默认值）；③ 替换音频入口（填服务器绝对路径后调 replace 登记新版本资产，旧版本保留）。

### 前端音频绑定链路

前端不自行计算时间轴，只触发绑定动作并展示结果。绑定关系落在后端 AudioSegment 表的四个外键：shot_id→镜头、character_id→角色、voice_profile_id→音色、audio_asset_id→音频文件。四个前端入口：ParseView→AudioPlanPanel（规划生成）、StoryboardView→ShotAudioPanel（镜头级片段操作）、VoiceConfigDrawer（角色音色绑定）、ComposeView→AudioPolicyPanel+AudioTracksPanel（项目策略与轨检）。时间轴由后端 build_timeline 逐镜累加与 _collect_tts_audios 顺序累加得出，前端不参与计算，避免两套时序逻辑。

## 六、测试与验证

新增 tests/test_audio_policy.py 六例全部通过：speakers 优先 custom_voice；同镜头两片段互不阻塞且同片段重复提交 409；audio-config 默认 discard、更新 preserve、持久化、非法值 422；tts_mix 对白 3.5s 超计划 2s 时目标时长延长至 3.5s；混音命令含 adelay 与 amix 且不含 [0:a]；discard 丢内嵌音轨补 anullsrc。

回归结果：后端 7 个相关测试文件 37 passed（audio_policy、clip_probe、compose_export、compose_export_task_integration、subtitles、media_serve、shot_state）；前端 vue-tsc --noEmit EXIT=0；前端生产构建 built in 7.46s（修复 1 处未使用导入 TS6133）。

## 七、遗留项

1. preserve_ambience_only 仅保留枚举与校验，人声/环境音分离未实施（方案明确禁止用高通低通或降音量替代）。
1b. 替换音频当前由前端填写服务器绝对路径，尚未接入浏览器上传（后端 upload_asset 已具备，前端可后续接入）。
2. BGM Ducking（对白出现时自动降 BGM 音量）未实现，当前固定音量加结尾 1s 淡出。
3. 对白超镜头时长当前自动延长并记 warning，尚未接入人工审核闸口。
4. SFX/环境声未接入 tts_mix 混音链（当前仅 dialogue/narration 参与）。
5. ComfyUI 端到端实测未做（需真实 GPU 环境）。
6. 完整测试套件（540 tests）未跑完。

## 八、回滚方式

后端三文件（app/api/v1/audio.py、app/services/compose.py、app/worker/compose_executor.py）单文件回滚，未新增数据库迁移；前端 AudioPolicyPanel.vue 为新增文件，ComposeView.vue 与 AudioTracksPanel.vue 单文件回滚；生产默认 native_audio_policy=discard，如需恢复旧行为可切至 preserve。
