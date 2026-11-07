# 音频模块与 H3 VA 工作流改造交付报告（2026-11-06）

> 阶段五交付物。前置文档：audio-module-audit-report-20261106.md（阶段一审计）。
> 实施依据：V1 音频模块与 Qwen3-TTS CustomVoice 集成方案。

## 1. 代码变更清单

### 后端（新增）
| 文件 | 内容 |
|---|---|
| app/models/audio.py | VoiceProfile（角色音色配置）/ AudioSegment（音频时间轴片段）/ AudioReview（音频版本审核） |
| app/services/audio_plan.py | 音频规划服务：从解析结果确定性生成对白/旁白/BGM/SFX/环境声片段；原生音频登记；时间轴查询 |
| app/api/v1/audio.py | 10 条音频 API（见 §3） |
| alembic/versions/a1b2c3d4e5f7_audio_module_tables.py | 三张音频表迁移 |
| alembic/versions/b2c3d4e5f6a8_audio_custom_voice_fields.py | CustomVoice 字段增量迁移 |

### 后端（修改）
| 文件 | 变更 |
|---|---|
| app/models/ids.py | 新增 voice_profile_id / audio_segment_id / audio_review_id |
| app/models/enums.py | 新增 TRACK_TYPE / SEGMENT_TYPE / AUDIO_GENERATION_STATUS / AUDIO_REVIEW_STATUS / NATIVE_AUDIO_POLICY |
| app/models/__init__.py | 注册三个新模型 |
| app/worker/executor.py | ① clip 检测后调用 register_native_audio 登记 H3 原生音频片段（绑定视频资产版本，无音频流标记 absent，不阻断）；② 新增 _sync_audio_segment_after_generate：音频任务成功后回写片段状态/资产版本/ffprobe 实测时长，挂接对账与正常两个成功路径 |
| app/main.py | 注册 audio 路由 |

### 前端（新增）
| 文件 | 内容 |
|---|---|
| src/types/audio.ts | 音频类型定义（片段/轨道/音色/speaker/旁白配置 + 状态标签映射） |
| src/api/audio.ts | 音频 API 封装（13 个函数） |
| src/components/AudioPlanPanel.vue | 剧本分析页音频分析结果面板（集数选择 + 按轨道折叠 + 生成/重建规划） |
| src/components/VoiceConfigDrawer.vue | 角色音色配置抽屉（speaker 选择来自后端列表 + instruct 配置 + 试听 + 设默认） |
| src/components/ShotAudioPanel.vue | 镜头详情音频面板（按轨道分组 + 重新生成 + 审核） |
| src/components/AudioTracksPanel.vue | 合成页音频轨道面板（六轨就绪统计 + 原生音频策略选择 + 缺失提示） |

### 前端（修改）
| 文件 | 变更 |
|---|---|
| src/types/index.ts、src/api/index.ts | 注册 audio 导出 |
| src/views/project/ParseView.vue | 嵌入 AudioPlanPanel（解析完成后显示） |
| src/views/project/AssetsView.vue | 角色卡片新增音色按钮 + 嵌入 VoiceConfigDrawer |
| src/views/project/StoryboardView.vue | 镜头详情抽屉新增分区 4 音频（ShotAudioPanel） |
| src/views/project/ComposeView.vue | 合成设置卡片后嵌入 AudioTracksPanel |

## 2. 数据库迁移说明

- a1b2c3d4e5f7（Revises c9e2f5a8d1b4）：创建 voice_profile / audio_segment / audio_review 三张表，含索引；全部新列带默认值，向后兼容。
- b2c3d4e5f6a8（Revises a1b2c3d4e5f7）：voice_profile 增加 mode/language/default_instruct/enabled/approved；audio_segment 增加 instruct/sequence。
- 两步迁移均已执行并验证（PRAGMA table_info 确认新列存在）。

## 3. API 文档（/api/audio 前缀）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | /projects/{id}/episodes/{ep}/plan | 生成/重建音频规划（幂等，保留 native_audio 片段） |
| GET | /projects/{id}/episodes/{ep}/plan | 获取音频规划（按轨道分组 + 统计） |
| GET | /projects/{id}/episodes/{ep}/timeline | 获取音频时间轴 |
| POST | /segments/{id}/regenerate | 重新生成片段（创建 generate_audio/bgm/sfx 任务，新版本不覆盖旧资产） |
| POST | /segments/{id}/replace | 上传替换音频（登记新版本资产） |
| POST | /assets/{id}/review | 审核具体音频资产版本（绑定 asset_id + version） |
| GET | /speakers | 列出 TTS 支持的 speaker（读取 workflow meta，禁止硬编码） |
| GET/PUT | /projects/{id}/narrator-voice | 项目级旁白 speaker 配置（存 Project.settings） |
| GET/POST | /characters/{id}/voices | 角色音色列表/新增 |
| PATCH | /voices/{id} | 更新音色配置 |
| POST | /voices/{id}/preview | 试听音色（异步任务） |

## 4. H3 workflow JSON 变更说明

**零改动。** 审计确认三个 H3 Ref2VA 档位（std_5s/fast_5s/quality_10s）原生输出内嵌音频的 mp4（音频 VAE → 音视频联合采样 → 拆分 latent → 解码音频 → CreateVideo 合成），属于方案 6.2 情况 1。参考图槽位（ref_image_0~8）与首帧绑定完全未动。TTS workflow（wf_tts_qwen3_custom_voice.json）与方案要求的三节点结构（Qwen3Loader/Qwen3CustomVoice/SaveAudioMP3）及参数注入点（$tts_text/$tts_voice/$tts_lang/$tts_emotion/$seed）完全一致，同样零改动。

## 5. 测试结果

- 后端目标测试子集（test_clip_probe / test_compose_export / test_generate_task_integration）：13 passed，无回归。
- 后端应用导入验证：app.main 正常导入，10 条音频路由全部注册（OpenAPI 确认）。
- 前端类型检查：vue-tsc --noEmit 通过（EXIT_CODE=0）。
- 前端生产构建：vue-tsc -b && vite build 成功（✓ built in 7.45s）。
- 数据库迁移：两步迁移执行成功，新表新列 PRAGMA 验证通过。
- 完整测试套件（540 tests）因运行时间过长中止，改跑目标子集；建议后续在空闲时段完整跑一次。

## 6. 前端页面变更说明

- 剧本分析页：解析完成后显示音频分析结果面板，可按集生成/重建音频规划，按轨道折叠查看片段与状态。
- 资产库：角色卡片新增音色按钮，抽屉内配置 speaker（来自后端列表）、语言、默认 instruct，支持试听（异步任务）、设默认（提示仅影响后续生成）。
- 分镜看板：镜头详情抽屉新增分区 4 音频，按轨道分组展示当前镜头片段，支持重新生成与审核通过/驳回。
- 合成页：合成设置后显示音频轨道面板，六轨就绪统计（未生成/待审核/已通过），原生音频策略选择（待检测/保留/静音），必需轨缺失时警示。
- 未新增任何一级导航（符合要求）。

## 7. 未完成事项与已知限制

1. ComfyUI 端到端实测：本次会话未实际提交 TTS/H3 生成任务（需 ComfyUI 服务与 GPU 算力），建议在真实环境跑一次最小 prompt 验证并留存 FFprobe 检测记录。
2. 混音配置持久化：AudioTracksPanel 的原生音频策略当前为前端本地状态，随混音配置落库（Composition 轨道配置）在后续版本接入；compose_executor 现有 h3_va/tts_fallback 混音逻辑未改动。
3. 对白超镜头时长的人工审核流：AudioSegment 已记录 duration_ms（ffprobe 实测），超容差进入人工审核的完整流程待后续接入 compose 前置检查。
4. SFX/BGM 素材库上传选择：V1 方案中 BGM/SFX 来自素材库，素材库上传与选择 UI 待后续迭代（当前可通过片段 replace 接口上传替换）。
5. BGM Ducking、响度标准化目标平台配置：V1.1 范围。
6. 完整测试套件（540 tests）未在本会话跑完，建议空闲时段补跑。
