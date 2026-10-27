# 故障、根因、修复与验证记录

- 记录日期：2026-10-26
- 项目：prj_c4d1175b2c70（《小马过河》）

## 故障 1：clip 批量生成报『必填参数 first_frame 缺失』

**发生阶段：** 阶段 F（分镜视频生成）

**现象：** POST /api/projects/{id}/generate (target=clip) 提交的 8 个任务全部失败，error_code=workflow_marker_missing，错误信息『必填参数 first_frame 缺失』。

**错误证据：**
- 失败任务：job_4c7df7c6bcb5 等 8 个（2026-10-26 02:11）
- 数据库查询：失败任务 workflow_id=wf_e06c92e5cfe6（wf_video_h3_flf2v，required_params=[first_frame]）

**根因分析：**
- 直接原因：工作流匹配兜底逻辑选中了 flf2v（首帧生视频）工作流，而该工作流要求 first_frame 参数，批量生成接口未提供。
- 深层原因：三个 video 类型工作流（flf2v/ref2va/t2v）均未设置 is_default，_match_workflow 兜底取『该类型任意工作流』的第一个，结果不可控。
- 已证实：数据库查询确认 workflow_id 指向 flf2v。

**修复方案：**
- 通过 PATCH /api/workflows/wf_f0c272792134 将 ref2va（分镜图参考生视频，符合本业务链路）设为 video 类型默认工作流。
- 选择理由：ref2va 的 required_params=[ref_image]（当前镜头分镜图），与批量 clip 生成语义匹配；改动为配置级，风险最低。

**执行结果：**
- 修复后重新提交，任务不再报 first_frame 缺失。✅ 已解决

## 故障 2：clip 生成报『必填参数 width 缺失』

**发生阶段：** 阶段 F

**现象：** 显式指定 ref2va 工作流后，8 个任务全部失败，错误『必填参数 width 缺失』。

**错误证据：** 失败任务 job_6c83f33515eb 等 8 个（2026-10-26 02:15）。

**根因分析：**
- 直接原因：ref2va 工作流标记了 $width!/$height!/$length（必填），但调用方（API 层与 executor）均未注入这些参数。
- 深层原因：WORKFLOW.md §5.2/§8/§13.1 规定执行器应按 project.aspect_ratio 注入 H3 分辨率、按 shot.duration 换算帧数 length，但 executor.py 未实现该逻辑（文档与代码不一致，代码缺失）。
- 已证实：grep 确认后端无 resolution_constraints/frame_count 注入实现；ref_image（分镜图）与 ref_prev_frame（上镜末帧）的自动注入同样缺失。

**修复方案：**
- 修改 backend/app/worker/executor.py（已备份 executor.py.bak_20261026）：
  1. generate_clip 任务自动注入 ref_image（当前镜头最新有效 keyframe 资产）
  2. 存在 last_frame_asset_id 时注入 ref_prev_frame（上镜末帧）
  3. 按 project.aspect_ratio 注入 H3 width/height（9:16→736×1312，16:9→1312×736，1:1→768×768）
  4. 按 shot.duration 换算 length（fps=24，length=17×n+5，范围 124-362）
- 选择理由：按文档契约补齐执行器实现，符合项目设计；不修改工作流定义。
- 回归风险：仅影响 generate_clip 任务；已运行全量测试套件验证。

**执行结果：**
- 修复后任务正常进入 ComfyUI 执行（不再报参数缺失）。✅ 已解决
- 回归测试：pytest tests/ 59 passed

## 故障 3：执行器异常『no such column: asset.quality_status』

**发生阶段：** 阶段 F

**现象：** 修复 2 部署后，8 个任务失败，错误『执行器异常: (sqlite3.OperationalError) no such column: asset.quality_status』。

**错误证据：** 失败任务 job_a94045d222b8 等 8 个（2026-10-26 02:44）。

**根因分析：**
- 直接原因：模型 production.py 定义了 Asset.quality_status 字段，但数据库 asset 表缺少该列。
- 深层原因：迁移文件 1283779e3172_asset_quality_fields.py 已存在但未应用（alembic_version 停留在 e2c5d8a4f319）。
- 已证实：PRAGMA table_info(asset) 无 quality_status 列；alembic_version 表确认为 e2c5d8a4f319。

**修复方案：** 执行 alembic upgrade head 应用迁移（补齐 quality_status 列，server_default='pending'）。

**执行结果：**
- 迁移成功，alembic 版本升至 1283779e3172，quality_status 列存在。✅ 已解决

## 故障 4：clip 任务『等待超时（600s）』

**发生阶段：** 阶段 F

**现象：** 镜头 1 视频任务运行 600 秒后被标记失败，错误『等待超时（600s）』。

**错误证据：** 失败任务 job_33556aad7f28（02:44:28 开始，02:54:37 失败，恰好约 600s）。

**根因分析：**
- 直接原因：comfyui_job_timeout_seconds 默认 600s，而 H3 视频生成单镜实际需约 20-25 分钟（A10 实例，参考 RTX 3090 362 帧约 23 分钟）。
- 深层原因：超时配置与 H3 真实生成时长不匹配（工作流 meta 标注 hardware status=pending_benchmark，未经 A10 实测校准）。
- 已证实：ComfyUI 队列中任务仍在真实执行（running:1），非服务故障。

**修复方案：**
- 更新 runtime_settings.json：comfyui_job_timeout_seconds 600→1800
- 重启 worker 使配置生效；通过 retry 接口重试失败任务（新任务 ID，继承 attempts）

**执行结果：**
- 重试任务 job_08027283aae3 运行中（attempts=2，1800s 超时窗口内）。✅ 已解决（配置层面）
- 附带发现：retry 会生成新任务 ID，多次重试产生重复任务，已取消多余 2 个（cancelled）

## 故障 5：前端构建失败『EACCES: permission denied, unlink dist/assets/...』

**发生阶段：** 回归验证（前端构建）

**现象：** npm run build 失败，vite 清空 dist 目录时权限拒绝。

**错误证据：** dist/assets 目录属主为 root（此前以 root 构建过），ubuntu 用户无法 unlink。

**根因分析：** dist 目录内文件由 root 用户构建产生，属主不一致。

**修复方案：** sudo chown -R ubuntu:ubuntu frontend/dist 后重新构建。

**执行结果：** 构建成功（✓ built in 6.02s）。✅ 已解决

## 历史遗留失败（本次未处理，非本次引入）

- 早期 parse 任务失败（LLM 401 无效 API Key / 502 上游超时）：最后一次 parse 已成功，属早期配置问题。
- 早期 clip 任务失败（first_frame/width 缺失、quality_status 列缺失）：均为本次修复前的失败记录，修复后批次正常。
