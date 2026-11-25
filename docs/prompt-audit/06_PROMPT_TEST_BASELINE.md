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
| 本轮最终（两次全量运行） | 1411 收集 | 2 failed → 1 failed，失败项均已定性 |
| P3 守护测试落地后 | 1428 收集 | 新增 P3-5（5）+ P3-4（6）+ P3-1（6）共 17 项 |

> 当前仓库实际 collect 为 1432 项，差额 4 项来自工作区未跟踪的 tests/test_prompt_qc_positions.py（外部会话产出，不属本轮范围），故本轮口径按 1428 项计。

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

后续处置（已完成，R-05）：已按该建议把 test_media_normalized_before_upload 从 inspect.getsource 文本匹配改为 ast.parse 直读磁盘的结构断言（提交 3626277）。新写法定位 _execute_generate_job_once 函数节点，断言 app.core.config 的 stored_path 以 _norm_stored_path 别名导入、存在 _norm_stored_path 调用、且该调用行号早于 upload_image，不再依赖 linecache 行号缓存，因此不受并发写入影响。验证：test_media_path_normalization.py 5 项全部通过（1.19s）；另做反向验证，正常结构通过、缺少归一化导入与归一化晚于上传两种漂移均被捕获。

---

## 四、提示词专项测试清单（133 项）

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
| test_video_prompt_sync.py | 5 | 视频首镜/续镜模板同步演进（P3-5） |
| test_shot_video_mapping.py | 6 | 编译映射对模板占位符覆盖（P3-4） |
| test_script_prompt_common.py | 6 | 剧本创作/扩写公共规范一致（P3-1 方案 2） |
| 合计 | 133 | — |

---

## 五、本轮新增测试

| 文件 | 用例数 | 提交 | 守护目标 |
| --- | ---: | --- | --- |
| tests/test_prompt_registry.py | 6 | 203f53a | 注册表覆盖 27 模板、无孤儿、元数据完整、input_vars 与占位符一致、指纹随模板变更、快照形状 |
| 内置外置防漂移测试 | 3 | 76a2770 | 5 个曾漂移模板的内外一致性 |
| tests/test_video_prompt_sync.py | 5 | 2d0daf2 | 12 条共享规则双向存在、身份约束核心句式、首镜无 Picture 2、续镜双参考职责不可对调、模板未被合并 |
| tests/test_shot_video_mapping.py | 6 | 62a1527 | 视频/分镜图/三类资产映射对模板占位符的覆盖关系、映射键必须为字符串字面量 |
| tests/test_script_prompt_common.py | 6 | a95ee66 | 剧本创作/扩写 4 条共享规范、人物名一致性、职责边界、专属规则不互相串入 |

新增测试全部通过，且与既有 116 项专项测试不重复：新增 17 项与既有 116 项合并运行共 133 项全部通过（16.04s），无状态污染。三项新增守护测试均做了反向验证（见第九节），注入模拟漂移后测试如实失败，证明不是永远通过的无效守护。

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

第一步，提示词专项回归（133 项，约 16 秒）。在 backend 目录下运行 pytest，参数为 13 个提示词相关测试文件并追加 -q：test_prompt_registry、test_prompt_store_consistency、test_prompt_compiler、test_prompt_docs_sync、test_prompt_ref_consistency、test_asset_prompt_hardening、test_prompt_reform、test_prompt_and_ref、test_multichar_prompt_and_slots、test_prompt_empty_fill、test_video_prompt_sync、test_shot_video_mapping、test_script_prompt_common。

第二步，全量回归（1428 项，约 6 分钟）。在 backend 目录下运行 pytest tests -q，输出重定向至 /tmp/pytest_run.log，结束后查看末尾 5 行确认摘要。

注意事项：

1. 全量运行前必须确认工作区无并发写入（无其他 pytest 进程、无外部会话改文件），否则静态源码断言类测试会误报（本轮已实测，见 3.2 节）。
2. 失败项须先定性（断言过时 / 环境干扰 / 真实回归）再修，不得直接改测试让其通过。
3. 本轮基线结论：提示词机制相关测试 133 项全部通过；全量两次运行分别为 2 failed 与 1 failed，失败项均已定性为断言过时与环境并发写入，无代码回归。

---

## 九、守护测试的反向验证记录

新增的三项守护测试均执行了「注入漂移后断言必须失败」的反向验证，避免写出永远通过的无效测试。全部在内存内 monkeypatch 完成，未修改任何模板或业务代码文件。

| 测试文件 | 注入的漂移 | 是否捕获 | 失败信息 |
| --- | --- | --- | --- |
| test_video_prompt_sync.py | 续镜模板删除一条共享规则 | 是 | 共享规则漂移 |
| test_video_prompt_sync.py | 首镜模板误加 Picture 2 | 是 | 首镜不应出现 Picture 2 |
| test_video_prompt_sync.py | 两份模板合并为同一文本 | 是 | H3 参考图职责会失效 |
| test_shot_video_mapping.py | 视频模板新增未同步占位符 | 是 | mapping 未覆盖占位符 |
| test_shot_video_mapping.py | 分镜图模板新增未同步占位符 | 是 | mapping 未覆盖占位符 |
| test_shot_video_mapping.py | 角色资产映射漏字段 | 是 | _character_mapping 未覆盖占位符 |
| test_script_prompt_common.py | 扩写模板删除一条共享规范 | 是 | 通用规范漂移 |
| test_script_prompt_common.py | 创作模板串入扩写专属约束 | 是 | 不应包含扩写专属约束 |
| test_script_prompt_common.py | 两份模板合并 | 是 | 扩写模板缺少扩写语义 |

结论：9 项模拟漂移全部被捕获，三项守护测试有效。





