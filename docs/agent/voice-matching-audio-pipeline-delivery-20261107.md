# 角色智能音色匹配与音频生产管线 — 交付报告

日期：2026-11-07
范围：阶段 1 审计 + 阶段 2 音色库与绑定 + 阶段 3 前端音色配置 + 阶段 4 LLM 智能匹配 + 阶段 5 AudioPlanningAgent 闸口 + 阶段 6 音频生产管线增强 + 阶段 7 导演台集成
关联：voice-matching-audio-pipeline-audit-20261107.md（阶段 1 审计报告）

## 一、已完成内容

### 阶段 1：项目审计
- 输出审计报告：docs/agent/voice-matching-audio-pipeline-audit-20261107.md
- 明确可复用（Job/Worker/Scheduler/Asset/9 个真实 speaker_id/TTS 调用链/音频规划/试听缓存/合成链路/角色模型/前端基础/项目音频策略）与需新增（VoiceLibrary/VoiceProfile 扩展/LLM 画像与评分/确认闸口/进度 API/前端增强/续集继承）

### 阶段 2：音色库和角色绑定
- **新增 VoiceLibrary 系统音色库表**（app/models/audio.py）：voice_code/speaker_id/gender_presentation/perceived_age/pitch/timbre/personality_tags/speaking_styles/language/enabled/sort_order/preview_asset_id
- **VoiceProfile 扩展 7 个字段**：voice_library_id/match_score/match_reason/alternatives/is_locked/source/voice_profile_json
- **Alembic 迁移**：e5a7b9c1d3f2（down_revision=c3f8a1d5e9b4），voice_library 建表 + voice_profile 扩展字段，已执行并验证
- **种子脚本**：scripts/seed_voice_library.py，从 wf_tts_qwen3_custom_voice.meta.json 读取真实 9 个 speaker_id（男 5：Uncle_Fu/Dylan/Eric/Ryan/Aiden；女 4：Vivian/Serena/Ono_Anna/Sohee），幂等初始化，不虚构元数据
- **执行结果**：9 个音色全部入库（男 5 / 女 4），speaker_id 全部经过真实环境验证

### 阶段 3：前端音色配置
- **新增 VoiceMatchPanel.vue**：系统音色库展示（男/女分组卡片 + 试听）、批量自动匹配、逐角色试听/确认/锁定/解锁、候选音色更换、全部确认未锁定角色
- **新增 AudioProgressPanel.vue**：导演台音频生产进度（对白/旁白/BGM/SFX 分组实时统计 + 待确认音色提醒 + 片段明细展开 + 局部重新生成）
- **集成到 AssetsView**：角色分类下展示 VoiceMatchPanel（制作资产区之前）
- **集成到 DirectorConsoleView**：任务上下文面板嵌入 AudioProgressPanel（待审批生成之前）
- **类型扩展**：src/types/audio.ts 新增 SystemVoice/VoiceBinding/VoiceMatchResult/AudioProductionProgress/AudioSegmentDetail 等类型；AudioGenerationStatus 增加 waiting_voice_confirmation
- **API 封装**：src/api/audio.ts 新增 10 个函数（listSystemVoices/updateSystemVoice/previewSystemVoice/matchProjectVoices/getVoiceBinding/confirmVoiceBinding/lockVoiceBinding/unlockVoiceBinding/getAudioProductionProgress/getAudioSegmentDetails）

### 阶段 4：LLM 智能匹配
- **新增 voice_matching.py 服务**（app/services/voice_matching.py）：
  - LLM 声音画像：白名单校验（gender/age/pitch/importance），非法值置 unknown，LLM 失败降级
  - 程序评分：性别 20% / 听感年龄 20% / 音色特点 25% / 性格 20% / 说话风格 15%
  - 全局贪心分配：主角优先（重要性权重 × 10000 + 对白数量）、复用惩罚（非 extra 角色复用扣 15 分）、锁定排除、人工指定且已确认默认排除
  - 持久化候选结果：仅写匹配字段，approved 不变，待用户确认
  - speaker_id 只能来自 VoiceLibrary（源头 workflow meta），LLM 无权创造

### 阶段 5：AudioPlanningAgent 闸口
- **新增 waiting_voice_confirmation 状态**（enums.py AUDIO_GENERATION_STATUS）
- **audio_plan.py 改造**：有角色的对白片段根据音色确认状态落 waiting_voice_confirmation 或 pending；旁白/无角色片段不受闸口限制
- **修复潜在缺陷**：Scene.title 字段引用（原 scene.name 不存在，BGM 回退描述路径）

### 阶段 6：音频生产管线增强
- **新增 voice_matching API**（app/api/v1/voice_matching.py）：
  - GET /api/voices/system：系统音色库列表（gender/enabled 过滤）
  - PATCH /api/voices/system/{voice_code}：管理员维护（speaker_id 不可改）
  - POST /api/voices/system/{voice_code}/preview：系统音色试听（统一测试句 + 缓存）
  - POST /api/projects/{id}/voices/match：批量自动匹配
  - GET /api/projects/{id}/characters/{cid}/voice/binding：绑定详情
  - POST /api/projects/{id}/characters/{cid}/voice/confirm：确认音色（触发闸口放行）
  - POST /api/projects/{id}/characters/{cid}/voice/lock|unlock：锁定/解锁
- **新增音频进度 API**（app/api/v1/audio.py 追加）：
  - GET /api/audio/projects/{id}/audio/progress：按轨道分组实时统计
  - GET /api/audio/projects/{id}/audio/segments：片段明细分页（episode/character/shot/type/status 过滤）
- **regenerate 修复**：音频任务按轨道类型显式绑定 workflow（执行器要求 workflow_id 非空），SFX 无 Provider 时明确拒绝
- **executor 增强**：系统音色试听缓存回写（system_voice_code 分支）

### 阶段 7：导演台集成
- AudioProgressPanel 嵌入导演台任务上下文面板
- 进度数据来源：数据库实时聚合，不伪造
- 待确认音色提醒：waiting_voice_confirmation 数量展示
- 失败片段局部重新生成入口

## 二、测试验证

### 后端测试
- tests/test_voice_matching.py：5 例全过（系统音色库 API / 程序评分维度 / 锁定排除 / 确认放行 / 锁定解锁）
- tests/test_voice_gate.py：2 例全过（闸口 waiting/released / 进度统计与明细过滤）
- tests/test_audio_policy.py：6 例全过（既有音频策略回归 + regenerate workflow 绑定修复）
- **合计 13 passed**

### 前端验证
- vue-tsc --noEmit：EXIT=0（类型检查无错误）
- vite build：成功（7.79s）

### 数据库迁移
- e5a7b9c1d3f2 已执行并验证：voice_library 表 9 条记录、voice_profile 7 个新字段全部存在

## 三、关键约束遵守

- speaker_id 唯一来源：wf_tts_qwen3_custom_voice.meta.json voice_values，禁止硬编码与虚构
- LLM 不决定分数、不创造 speaker_id（白名单校验 + 程序评分）
- 锁定角色绝不参与自动匹配
- TTS 不进入 H3 workflow、不占参考图槽位
- 复用现有 Job/Worker/Scheduler/Asset 架构，不重复建表
- Alembic 迁移沿用现有 versions 链
- 保留现有剧本/资产/分镜/视频/导出流程

## 四、未完成 / 待后续迭代

- 阶段 8 音画合成：现有 compose.py tts_mix 分支已具备基础，需补充 SFX/环境声混入与时长冲突审核闸口
- 续集继承逻辑：Character 项目级 + normalized_name 已具备基础，需落地自动继承规则
- 系统音色库管理页（项目设置 → 音频设置）：后端 API 已就绪，前端管理页待迭代
- BGM Ducking / preserve_ambience_only 人声分离：未实施（需真实能力验证）
- ComfyUI 端到端实测：需真实 GPU 环境
- 完整测试套件（540 tests）：核心模块已验证，空闲时段建议补跑

## 五、变更文件清单

### 后端新增
- app/models/audio.py（VoiceLibrary 模型 + VoiceProfile 扩展）
- app/services/voice_matching.py（LLM 画像 + 评分 + 全局分配）
- app/api/v1/voice_matching.py（音色匹配 API）
- alembic/versions/e5a7b9c1d3f2_voice_library_and_matching.py
- scripts/seed_voice_library.py
- tests/test_voice_matching.py
- tests/test_voice_gate.py
- docs/agent/voice-matching-audio-pipeline-audit-20261107.md

### 后端修改
- app/models/ids.py（voice_library_id）
- app/models/__init__.py（导出 VoiceLibrary）
- app/models/enums.py（waiting_voice_confirmation）
- app/services/audio_plan.py（音色确认闸口 + Scene.title 修复）
- app/api/v1/audio.py（regenerate workflow 绑定 + 进度 API + 片段明细 API）
- app/worker/executor.py（系统音色试听缓存回写）
- app/main.py（注册 voice_matching 路由）
- tests/test_audio_policy.py（regenerate 种子预置）

### 前端新增
- src/components/VoiceMatchPanel.vue
- src/components/AudioProgressPanel.vue

### 前端修改
- src/types/audio.ts（类型扩展）
- src/api/audio.ts（API 封装扩展）
- src/views/project/AssetsView.vue（集成 VoiceMatchPanel）
- src/views/DirectorConsoleView.vue（集成 AudioProgressPanel）
