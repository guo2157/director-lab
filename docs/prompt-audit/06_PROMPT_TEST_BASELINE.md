# 06. 提示词测试基线

编制日期：2026-11-25
测试框架：pytest 8（backend/pyproject.toml 配置）
执行环境：Ubuntu / Python 3.12 / backend/.venv

---

## 一、基线口径

本基线区分三类数据，避免混淆：

| 类型 | 含义 | 采集方式 |
| --- | --- | --- |
| 全量回归 | 整个 backend/tests 的通过数 | pytest 全量运行 |
| 提示词专项 | 直接覆盖提示词机制的文件与用例 | 按文件名筛选后 collect-only 统计 |
| 剧本实跑 | 端到端生成质量验证 | 需真实 LLM 与生成资源，本轮未执行 |

前两类是自动化回归基线，第三类是质量基线，两者不能互相替代。

---

## 二、全量回归基线

### 2.1 收集规模演进

| 时点 | 收集数 | 说明 |
| --- | ---: | --- |
| P0-4 修复时点 | 1394 通过 | 项目历史基线 |
| P2-1 注册表落地后 | 1406 收集 | 新增 test_prompt_registry 6 项、防漂移 3 项等 |
| 本轮最终 | 1411 收集 | 累计结果 |

### 2.2 本轮完整运行记录

| 序号 | 结果 | 耗时 | 失败项 |
| ---: | --- | ---: | --- |
| 运行 1 | 2 failed, 1409 passed | 352.07s | test_keyframe_image_qc.py::TestH_PlanPostureBlock::test_posture_block_reaches_system_prompt<br>test_media_path_normalization.py::TestImageToDataUriBackslashPath::test_backslash_path_is_readable |
| 运行 2 | 1 failed, 1410 passed | 355.39s | test_media_path_normalization.py::TestExecutorNormalizesMediaPaths::test_media_normalized_before_upload |

两次运行相关 96 条警告均为依赖弃用提示（Starlette httpx、Pillow getdata、Alembic path_separator），与提示词无关。

---

## 三、失败项定性（三处，均已查清）

### 3.1 运行 1 的两个失败：断言过时，已修复

| 失败用例 | 断言内容 | 实际行为 | 定性 |
| --- | --- | --- | --- |
| test_posture_block_reaches_system_prompt | 期望 data:image/png | 返回 data:image/jpeg | 断言过时 |
| test_backslash_path_is_readable | 期望 data:image/png | 返回 data:image/jpeg | 断言过时 |

根因追溯：keyframe_image_qc._image_to_data_uri 在 72f39b6 中按注释有意改为输出 JPEG（PNG 原图 base64 后单张 3.2MB，一镜四张图请求体约 13MB，实测网关 HTTP 429 与超时，导致质检静默降级）。两个断言仍写死 png，属实现变更后未同步的遗留断言。

处置：

- test_keyframe_image_qc.py 的断言已随 4f42f0f 更新为 jpeg。
- test_media_path_normalization.py 的断言由本轮修正，提交 3a3ccf4。
- 修正后专项验证：tests/test_media_path_normalization.py + tests/test_keyframe_image_qc.py + tests/test_multichar_prompt_and_slots.py 共 31 项全部通过（11.53s）。

注意：test_backslash_path_is_readable 的真实意图是验证反斜杠路径能被 stored_path 归一化读取（断言 out is not None 与缺失文件返回 None），格式断言仅为附带，因此只改格式断言不改测试语义。

### 3.2 运行 2 的失败：环境并发写入，非代码缺陷

失败用例：test_media_normalized_before_upload

断言方式：inspect.getsource(executor._execute_generate_job_once)，静态读取函数源码文本，检查其中是否含 stored_path 归一化调用。

失败证据：断言报错信息中打印出的源码以 def _claim_job_for_execution 开头，说明 inspect 按模块导入时的旧行号读取了已变更的磁盘文件，抓到了相邻函数。

定性依据：

1. 该用例在单独运行 test_media_path_normalization.py 时通过（31 passed 记录）。
2. 全量运行期间，工作区存在外部会话对 backend/app/worker/executor.py 的并发改动（git status 显示 M executor.py，且改动内容为续跑轮询与 job.started 事件修复，与提示词审计无关）。
3. 该用例属静态源码断言，对文件内容变化极其敏感，天然不适合在文件被并发修改时运行。

结论：这是测试隔离与并发写入问题，不是本次提示词审计引入的回归，也不是业务代码缺陷。

改进建议（列入 05 的计划范畴）：静态源码断言类测试（inspect.getsource）应改为行为断言，或至少改为从模块对象属性检查而非文本匹配，避免对文件内容变化敏感。

---

## 四、提示词专项测试清单（116 项）

按文件统计实际收集用例数（collect-only 直读）：

| 测试文件 | 用例数 | 覆盖内容 |
| --- | ---: | --- |
| test_prompt_compiler.py | 42 | 编译链路、占位符校验、负向词合并、validate_compilation |
| test_prompt_docs_sync.py | 14 | 文档与模板同步 |
| test_prompt_ref_consistency.py | 14 | 参考图职责一致性 |
| test_asset_prompt_hardening.py | 10 | 资产提示词加固 |
| test_prompt_reform.py | 9 | 提示词改造验收 |
| test_prompt_and_ref.py | 7 | 提示词与参考图 |
| test_prompt_registry.py | 6 | 注册表覆盖、孤儿、元数据、占位符一致、指纹、快照 |
| test_multichar_prompt_and_slots.py | 6 | 多角色与槽位 |
| test_prompt_empty_fill.py | 5 | 空值填充 |
| test_prompt_store_consistency.py | 3 | 内置与外置一致性 |
| 合计 | 116 | — |

---

## 五、本轮新增测试

| 文件 | 用例数 | 提交 | 守护目标 |
| --- | ---: | --- | --- |
| tests/test_prompt_registry.py | 6 | 203f53a | 注册表覆盖 27 模板、无孤儿、元数据完整、input_vars 与占位符一致、指纹随模板变更、快照形状 |
| 内置外置防漂移测试 | 3 | 76a2770 | 5 个曾漂移模板的内外一致性 |

新增测试全部通过，且与既有 116 项专项测试不重复。

---

## 六、未执行部分：10 类剧本回归实跑

以下验证需要真实 LLM 调用与生成资源，本轮未执行，因此不能声称质量指标已验证：

| 编号 | 剧本类型 | 验证重点 |
| ---: | --- | --- |
| S-01 | 单角色单场景 | 基础链路可用性 |
| S-02 | 多角色对话 | 站位注入、身份一致性（plan_posture_block） |
| S-03 | 多场景切换 | 场景资产绑定、空镜编译 |
| S-04 | 单道具交接 | 道具生命周期与可见性 |
| S-05 | 跨镜道具延续 | 道具锚定图复用 |
| S-06 | 复杂动作多拍 | 首拍起始瞬间约束（image_prompt 单帧化） |
| S-07 | 多集衔接 | episode_continue_system 与大纲连续性 |
| S-08 | 首镜与续镜混合 | video_prompt_first / body 的 Picture 1/2 职责 |
| S-09 | 场景空镜插入 | scene_plate_compiler 输出 |
| S-10 | 剧本评估回路 | script_evaluation_system 可制作性字段 |

执行建议：每类剧本至少跑 1 个完整流程（解析 → 大纲 → 资产 → 分镜图 → 视频），记录人物数量错误率、道具连续性断裂率、场景跳变率三项指标。手动抽检样本量不低于 20 张图与 10 段视频。

---

## 七、A/B 对比基线设计

05 计划中触碰模型输入的改动（P3-1 方案 1、P3-2、P3-3）必须先建立以下基线：

| 项目 | 做法 | 记录内容 |
| --- | --- | --- |
| 固定输入集 | 准备 5 个剧本（覆盖 S-01 至 S-07） | 剧本正文与结构化参数快照 |
| 固定模型参数 | temperature、模型版本、上下文窗口锁定 | 记录在 input_snapshot |
| 双跑对比 | 改动前后各跑一次，同输入同参数 | 输出文本 + image_prompt + video_prompt 全文 |
| 指标口径 | 格式合规率、字段完整率、人物一致性人工判定、道具连续性人工判定 | 每项 0-100 分或通过率 |
| 判定门槛 | 格式与字段类指标不得下降；质量类指标允许 ±5% 波动 | 超出即回滚 |

关键约束：不得用固定字数或时长判断合格（硬约束 H-6），所有质量指标必须是结构或人工判定。

---

## 八、回归执行手册

日常改动提示词后的最小验证集：

