# Director-Lab 端到端执行报告

- 执行日期：2026-10-26
- 测试项目：prj_c4d1175b2c70（名称『22』，《小马过河》剧本，16:9，8 镜头）
- 执行方式：真实调用后端 API（http://127.0.0.1:8000），worker 真实提交远端 ComfyUI（CloudStudio A10 实例）

## 一、环境检查结果

| 检查项 | 实际结果 | 证据 | 是否通过 |
|---|---|---|---|
| 后端服务 | uvicorn 运行中（:8000） | /api/health 返回 healthy，db/worker/comfyui 均 ok | ✅ |
| worker | app.worker.scheduler 运行中 | 任务被真实调度执行 | ✅ |
| 前端 | vite dev 运行中（:5174） | 进程列表 | ✅ |
| ComfyUI | 远端实例可达 | /system_stats 返回 200，ComfyUI 0.41.0，A10 24GB，PyTorch 2.8.0+cu128 | ✅ |
| LLM | deepseek（api.deepseek.com） | 剧本评估/解析任务成功 | ✅ |
| ffmpeg | 6.1.1-3ubuntu5 可用 | ffmpeg -version | ✅ |
| 数据库 | SQLite WAL，alembic 版本 1283779e3172 | 迁移已全部应用 | ✅ |
| 工作流 | 8 个内置工作流已注册 | GET /api/workflows | ✅ |

## 二、各阶段执行状态

### 阶段 A：剧本解析 — ✅ PASS
- 剧本评估（estimate）：job_6856667c9d9f 成功，产出主题/亮点/伏笔/分集计划
- 剧本解析（parse）：成功，产出 4 角色、6 场景、3 道具、8 镜头（含对白分段、beats、情绪曲线）
- 证据：GET /api/projects/prj_c4d1175b2c70/shots?episode=1 返回 8 个结构完整镜头

### 阶段 B：剧本评估与分析 — ✅ PASS
- 评估结果真实落盘（project_analysis 含 summary/theme/highlights/foreshadowing）
- 历史失败任务（LLM 401/502）为早期 API Key 与上游超时问题，最后一次执行成功

### 阶段 C：资产图生成 — ✅ PASS
- 4 角色定妆图、6 场景基准图、3 道具锚定图全部真实生成（13 个任务全部 succeeded）
- 产物验证：PNG 720×1280 有效图片，portrait/baseline/anchor_asset_id 全部回写
- 生成链路：Qwen-Image-Edit-2511（wf_image_ref_qwen_edit 工作流）

### 阶段 D：分镜脚本 — ✅ PASS
- 8 镜头含 image_prompt/video_prompt/对白分段/beats/情绪曲线/时长（10-20s）
- 镜头顺序、场景覆盖、对白对应关系经检查正确

### 阶段 E：分镜图生成 — ✅ PASS
- 8 镜头分镜图全部真实生成（批量 generate target=keyframe，含 @引用参考图注入）
- 批量确认：POST /projects/{id}/keyframe/confirm 确认 8/8

### 阶段 F：分镜视频生成 — ⏳ 进行中（受 ComfyUI H3 生成耗时限制）
- 修复后批次：8 镜头任务已提交（ref2va 工作流 + 自动参数注入）
- 镜头 1（job_08027283aae3）运行中（attempts=2，1800s 超时配置已生效）
- H3 单镜生成约 20-25 分钟（A10 实测参考：RTX 3090 362 帧约 23 分钟），8 镜串行预计约 3 小时
- 按用户指示：ComfyUI 相关长耗时任务转后台继续，不再阻塞等待

### 阶段 G：视频整合 — ⏸ 待阶段 F 完成
- 合成任务已验证可提交（job_2947fc9e68ed 排队中，worker 串行排在 clip 之后）

### 阶段 H：成片评估 — ⏸ 待阶段 G 完成

## 三、真实产物统计

- 资产图：13 个（4 角色 + 6 场景 + 3 道具），PNG 720×1280
- 分镜图：8 个（含缩略图 webp）
- 分镜视频：0 个已完成（1 个运行中，7 个排队）
- 产物目录：backend/data/media/（{asset_id}_v{version}.{ext} 命名规范）

## 四、总体状态：PARTIAL

- 已完成：环境、剧本解析、剧本评估、资产图、分镜脚本、分镜图（含确认）
- 进行中：分镜视频生成（后台继续）
- 待执行：视频整合、成片评估
- 阻塞说明：H3 视频生成为真实 GPU 长耗时任务，非系统故障；按用户指示跳过等待
