# 跨集视频首尾帧连续性改造报告

## 一、H3 VA 工作流对首帧约束的实际支持情况

### 严格首帧锁定（FLF2V）

工作流 `wf_video_h3_flf2v.json` 通过 `MiniMaxH3ImageToVideo` 节点支持严格首帧输入：

- 标记 `$first_frame.~image!`（必填，媒体类型）
- 标记 `$end_frame.~image`（可选尾帧）
- 节点 24/25 分别对首尾帧做 `ImageScale` 裁切到画布尺寸
- 严格首帧锁定：首帧图像直接作为视频生成的起始 latent 条件

### 参考图约束（Ref2V）

工作流 `wf_video_h3_ref2va.json`（默认视频工作流）通过六参考槽位支持参考图约束：

- 槽位 0 = `ref_prev_frame`（上一镜头末帧，参考图约束而非严格首帧）
- 槽位 1 = `ref_image`（当前分镜图）
- 槽位 2-5 = 角色/服装/场景/道具参考图
- R2V 不支持严格首尾帧，参考图仅提供视觉引导

### 改造选择

跨集首帧优先使用 FLF2V 工作流（严格首帧锁定）。当 FLF2V 不可用时，降级为 Ref2V 的 `ref_prev_frame` 参考图约束，并明确记录降级状态。

## 二、修改文件和代码入口

### 新增文件

| 文件 | 用途 |
|------|------|
| `backend/app/models/continuity.py` | EpisodeContinuity 数据模型 |
| `backend/app/services/frame_extract.py` | FFmpeg 末帧提取服务（含黑屏检测、回退、校验） |
| `backend/app/services/continuity.py` | 跨集连续性服务（提取、校验、失效、摘要） |
| `backend/alembic/versions/f9a3b7c2d1e6_episode_continuity_table.py` | 数据库迁移 |
| `backend/tests/test_continuity.py` | 6 项集成测试 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `backend/app/models/__init__.py` | 注册 EpisodeContinuity 导出 |
| `backend/app/models/ids.py` | 新增 `episode_continuity_id()` |
| `backend/app/worker/compose_executor.py` | 合成成功后自动提取末帧 + 下游失效 |
| `backend/app/api/v1/generate.py` | 集首镜 clip 生成时注入 first_frame + 连续性校验 |
| `backend/app/worker/executor.py` | 生成成功后记录首帧使用 |

### 代码入口

- **末帧提取触发点**：`compose_executor.py` → `execute_compose_job()` → 合成成功后调用 `extract_episode_last_frame()`
- **首帧注入触发点**：`generate.py` → `generate_shot()` / `regenerate_shot()` → `_inject_continuity_first_frame()`
- **版本失效触发点**：`compose_executor.py` → 合成成功后调用 `invalidate_downstream_continuity()`
- **首帧使用记录**：`executor.py` → 生成成功后调用 `record_opening_frame_usage()`

## 三、跨集末帧提取与首帧传递机制

### 末帧提取流程（第四节）

1. 合成成功后，`compose_executor` 自动调用 `extract_episode_last_frame()`
2. FFprobe 确认视频可解码，获取时长/帧率/分辨率
3. FFmpeg 在 `duration - 1/fps` 处提取最后一帧
4. 黑屏检测：若末帧为黑屏，逐步回退 0.5s 寻找最近有效画面（最多 10 次）
5. 图片验证：文件存在、非空、PIL 可解码、尺寸 >= 256x256
6. 计算 SHA256 校验值
7. 保存到 `data/media/continuity/ep{N}_lastframe_v{version}.png`
8. 更新 EpisodeContinuity 记录状态为 `extracted`

### 首帧传递流程（第五节）

1. 集首镜 clip 生成时，`_inject_continuity_first_frame()` 检查：
   - 镜头是否为当前集第一个镜头（按 scene.seq, shot.seq 排序）
   - 当前集号 > 1（第 1 集无需首帧约束）
2. 调用 `check_continuity_for_episode()` 校验：
   - 上一集连续性记录存在
   - 状态为 `extracted` 或 `passed`
   - 末帧图片路径有效且可解码
3. 校验失败时抛出 422 `continuity_check_failed`，阻止生成
4. 校验通过时：
   - 优先选择 FLF2V 工作流，注入 `__media__.first_frame`
   - FLF2V 不可用时降级为 Ref2V，注入 `__media__.ref_prev_frame`
5. 生成成功后记录首帧使用路径和资产 ID

### 三类图片用途区分

| 类型 | 用途 | 数据来源 |
|------|------|----------|
| 角色参考图 | 保持人物外观 | Character.portrait_asset_id |
| 场景参考图 | 保持环境风格 | Scene.baseline_asset_id |
| 跨集连续性首帧 | 衔接上一集结束画面 | EpisodeContinuity.source_frame_path |

## 四、任务依赖与版本失效机制

### 依赖调度（第七节）

- 第 N 集合成成功并提取末帧后，第 N+1 集首镜才允许生成
- 连续性校验失败时返回 422，不静默降级为普通文本生成
- 不依赖末帧的非首镜镜头不受影响，可并行处理

### 版本失效（第七节）

- 某集重合成时，`invalidate_downstream_continuity()` 执行：
  - 重置当前集连续性记录（旧末帧来自旧视频）
  - 下游所有剧集 `continuity_version += 1`，状态回 `pending`
  - 清除旧的 `source_frame_path`、`opening_frame_path` 等字段
- 重合成后自动重新提取末帧，下游剧集可重新校验通过

### 异常处理覆盖（第九节）

| 异常场景 | 处理方式 |
|----------|----------|
| 上一集视频生成失败 | 连续性记录不存在，下游校验返回错误 |
| 视频合成失败 | 合成执行器返回失败，不触发末帧提取 |
| 末帧提取失败 | 记录 error_detail，状态置 failed |
| 末帧图片损坏 | validate_frame_image 检测，状态置 failed |
| 图片尺寸不符合 | PIL 检测尺寸 < 256x256，继续回退寻找 |
| 下一集提前启动 | check_continuity_for_episode 返回 422 |
| 上一集被重新生成 | invalidate_downstream_continuity 使下游失效 |
| 文件路径失效 | os.path.exists 检查，校验失败 |
| ComfyUI 未接收到首帧 | __media__ 注入机制确保上传 |
| 输出首帧与预期不一致 | record_verification_result 记录差异 |
| 状态与文件不一致 | 每次校验都重新检查文件存在性和可解码性 |

## 五、测试结果

### 单元/集成测试（6 项全部通过）

```
tests/test_continuity.py::test_a_first_episode_last_frame_extraction PASSED
tests/test_continuity.py::test_b_second_episode_first_frame_injection PASSED
tests/test_continuity.py::test_c_output_consistency_tracking PASSED
tests/test_continuity.py::test_d_dependency_check_blocks_without_frame PASSED
tests/test_continuity.py::test_e_version_invalidation_on_recompose PASSED
tests/test_continuity.py::test_f_end_to_end_multi_episode PASSED
```

### 全量回归测试（70 项全部通过）

```
70 passed, 4 warnings in 20.87s
```

### 真实 FFmpeg 端到端验证

在项目 `prj_0d1a37ddcb6f`（含第 1、2 集）上执行 8 步验证：

| 步骤 | 验证内容 | 结果 |
|------|----------|------|
| Step 1 | 第 1 集末帧提取 | extracted, 953KB PNG |
| Step 2 | 第 2 集连续性校验 | ok=True |
| Step 3 | 首帧使用记录 | path 匹配 |
| Step 4 | 首帧图片验证 | valid=True |
| Step 5 | 验证结果记录 | passed |
| Step 6 | 重合成版本失效 | 2 个记录失效, version 递增 |
| Step 7 | 失效后阻止生成 | ok=False, 错误信息明确 |
| Step 8 | 重新提取后恢复 | ok=True |

### 真实末帧提取验证

在项目 `prj_d3bd83c2e8d7` 的现有合成视频上验证：

- 视频: `data/media/cmp_526cdd9c3246.mp4`, 时长 4.02s
- 末帧: `data/media/continuity/ep1_lastframe_v1.png`, 953KB
- SHA256: `4b55f04bcb39e0126979bb860f2acae39ae2d7a523dae442c83b66fbd86e6510`
- 时间戳: 3.978s (duration - 1/24fps)
- 帧率: 24.0 fps
- 回退: 未使用（末帧有效）

## 六、首帧对比结果和实际文件路径

### 末帧文件路径

```
data/media/continuity/ep1_lastframe_v1.png
```

### 首帧对比

- 上一集末帧 SHA256: `4b55f04bcb39...`
- 下一集首帧使用路径: `data/media/continuity/ep1_lastframe_v1.png`
- 首帧与末帧路径一致（同一文件作为首帧输入）
- 严格首帧模式（FLF2V）下，输出视频第一帧应与输入首帧一致
- 参考图模式（Ref2V 降级）下，首帧相似但非严格一致

## 七、已解决问题与剩余限制

### 已解决

1. 集级跨集连续性数据结构（EpisodeContinuity 表 + 状态机 + 版本追踪）
2. 合成成功后自动末帧提取（FFmpeg + 黑屏检测 + 回退 + 校验）
3. 下一集首镜强制首帧衔接（FLF2V 优先 + Ref2V 降级）
4. 连续性校验失败时阻止生成（422 错误，不静默降级）
5. 上一集重合成时下游版本失效（版本递增 + 状态重置）
6. 首帧使用记录与验证结果追踪
7. 三类图片用途明确区分（角色参考/场景参考/跨集连续性首帧）

### 剩余限制

1. **ComfyUI 实例当前不可达**：真实 H3 VA 视频生成需 ComfyUI 实例在线且 GPU >= 24GB 显存。当前实例返回 Cloud Studio 登录页面，无法执行真实视频生成。代码逻辑已就绪，实例恢复后可直接使用。

2. **严格首帧保证依赖模型能力**：FLF2V 工作流提供严格首帧锁定，但实际输出第一帧与输入首帧的一致性取决于 MiniMax H3 模型能力。本系统已确保首帧正确注入工作流，但不对模型输出做绝对一致性保证。

3. **首帧一致性自动验收待增强**：当前记录了首帧使用路径和验证结果，但输出视频第一帧与输入首帧的图像相似度对比需要额外的图像分析能力（如 SSIM/CLIP 相似度），本期未实现自动化图像对比。

4. **跨集连续性摘要为基础实现**：`build_continuity_summary` 从剧本上下文提取场景和动作信息，但末帧中人物位置、姿态、朝向、表情的自动分析需要视觉理解模型，本期使用剧本推导而非图像分析。

5. **并行调度未实现**：当前依赖校验在 API 层同步执行，未实现独立的依赖调度队列。不依赖末帧的镜头可并行处理，但集间依赖通过校验门控保证顺序。
