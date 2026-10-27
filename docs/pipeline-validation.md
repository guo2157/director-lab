# 各业务阶段验收情况

- 验收日期：2026-10-26
- 测试项目：prj_c4d1175b2c70（《小马过河》，8 镜头）

## 验收总表

| 阶段 | 状态 | 验收证据 |
|---|---|---|
| 环境 | ✅ PASS | /api/health 全绿；ComfyUI /system_stats 200；ffmpeg 6.1.1；alembic 1283779e3172 |
| 剧本解析 | ✅ PASS | 8 镜头结构完整（对白分段/beats/情绪曲线/时长），4 角色 6 场景 3 道具正确提取 |
| 剧本评估 | ✅ PASS | project_analysis 落盘（主题/亮点/伏笔/分集计划），job_6856667c9d9f succeeded |
| 资产图 | ✅ PASS | 13/13 生成成功（4 定妆+6 基准+3 锚定），PNG 720×1280 有效，ID 全部回写 |
| 分镜脚本 | ✅ PASS | 8 镜头 prompt/对白/beats/时长完整，顺序正确 |
| 分镜图 | ✅ PASS | 8/8 生成成功并批量确认（keyframe/confirm 8/8） |
| 分镜视频 | ⏳ 进行中 | 修复后批次已提交：1 运行中 + 7 排队（H3 单镜约 20-25 分钟，串行约 3 小时） |
| 音频 | ⏳ 排队中 | 镜头 1 TTS 任务已提交（job_61462f84eb7b，tts_text 自动取对白），worker 串行等待中 |
| 视频整合 | ⏸ 待执行 | 合成任务可提交（job_2947fc9e68ed 排队中，排在 clip 之后） |
| 成片评估 | ⏸ 待执行 | 依赖合成完成 |
| 自动恢复 | ✅ 已验证 | retry 机制（新任务 ID 继承 attempts）验证通过；超时任务重试后正常运行 |
| 最终交付 | ✅ 报告就绪 | e2e-run-report.md / failure-analysis.md / pipeline-validation.md / known-issues.md |

## 各阶段验收明细

### 环境
- 后端 uvicorn（:8000）、worker、前端 vite（:5174）运行中
- ComfyUI 远端实例（CloudStudio A10 24GB，ComfyUI 0.41.0）可达
- LLM（deepseek）可用；ffmpeg 6.1.1 可用
- 数据库迁移全部应用（alembic 1283779e3172）

### 剧本解析（阶段 A）
- 场次/对白/旁白/动作提取正确；角色统一命名（小马/马妈妈/老牛/小松鼠）
- 对白分段含 speaker/emotion/start_time/end_time；beats 含动作/起止姿态
- 解析结果被后续模块直接消费（资产生成/分镜生成均基于解析产物）

### 剧本评估（阶段 B）
- 主题识别：独立思考、结合自身情况判断、安全第一
- 核心冲突：老牛与小松鼠基于体型差异的相反经验
- 伏笔与回收：4 组 setup/reveal/payoff 完整

### 资产图（阶段 C）
- 13 个任务全部 succeeded（Qwen-Image-Edit-2511 工作流）
- 抽样验证：PNG 文件头正确、720×1280 RGB、873KB 非空文件
- portrait_asset_id/baseline_asset_id/anchor_asset_id 全部回写

### 分镜脚本（阶段 D）
- 8 镜头覆盖 6 场景全部关键情节；镜头顺序（集数→场景序→镜头序）正确
- 每镜头含 image_prompt/video_prompt/shot_type/camera/emotion/duration

### 分镜图（阶段 E）
- 8/8 生成成功（含 @引用参考图注入：角色定妆图/场景基准图）
- 批量确认接口返回 confirmed_count=8

### 分镜视频（阶段 F）— 进行中
- 修复链路后（默认工作流 ref2va + executor 自动参数注入 + 1800s 超时），任务真实提交 ComfyUI 执行
- 镜头 1 运行中（job_08027283aae3，attempts=2）
- 按用户指示：ComfyUI 长耗时任务转后台，不阻塞验收

### 回归测试
- 后端：pytest tests/ 59 passed（executor.py 修改后全量通过）
- 前端：npm run build 成功（修复 dist 目录属主后）
