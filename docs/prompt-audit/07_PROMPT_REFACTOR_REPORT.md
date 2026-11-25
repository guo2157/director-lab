# 07. 提示词体系重构总结报告

报告日期：2026-11-25
项目：/home/ubuntu/director-lab
交付目录：docs/prompt-audit/（01–07 共 7 份）

---

## 一、执行摘要

本次对项目提示词体系做了全量工程审计与整改，覆盖 36 份提示词文本资产（27 个运行时注册模板、2 处经评估不注册的内联提示词、7 个死模板），产出 14 个提交，修复 6 项模板级功能缺陷与 4 项机制缺陷，新增 129 行注册表模块与 9 项守护测试，交付 7 份审计文档。

核心结论：

1. 运行时提示词全部收口到 prompt_store 单一读取链路，27 个模板零孤儿、零真实冲突。
2. 曾造成实际功能缺陷的内置/外置漂移问题已全部对齐，并有自动化测试守护。
3. 生成质量类指标（人物一致性、道具连续性）本轮未实测，须按 06 文档的 10 类剧本计划执行后才能下质量结论。

---

## 二、13 个核心问题逐条回答

### Q1：项目里一共有多少个提示词模板？

36 份文本资产，分三类：

| 类别 | 数量 | 说明 |
| --- | ---: | --- |
| 运行时注册模板 | 27 | 经 prompt_store 注册并被业务代码调用，是真正的提示词资产 |
| 未注册内联提示词 | 2 | keyframe_image_qc 动态拼接质检提示词、video_workflow 一次性 user 文案，评估后不注册 |
| 死模板 | 7 | docs/prompts/prop/ 下 11-07 设计期文档，功能已由纯代码实现 |

### Q2：其中真正被运行时调用的有多少个？

27 个，覆盖率 100%，零孤儿。

证据：29 处 get_prompt 调用点（24 业务 + 4 框架 + 1 三元表达式覆盖 2 模板），逐模板调用方对照表见 04 文档第四节。该结论由 tests/test_prompt_registry.py::test_registry_has_no_orphan_entries 自动守护。

### Q3：有多少个模板是重复的？

严格意义上的重复只有 2 对（351 对两两比较，相似度不低于 0.55 的仅此 2 对）：

| 模板对 | 相似度 | 性质 | 处置 |
| --- | ---: | --- | --- |
| video_prompt_first / video_prompt_body | 0.769 | 必要分叉（H3 首镜与续镜职责不同） | 不合并 |
| script_create_system / script_expand_system | 0.616 | 结构重复（4 条通用剧本格式条款双处维护） | 列入 P3-1，待 A/B 后治理 |

### Q4：有多少个模板之间存在冲突？

0 个真实冲突。不存在两个模板对同一产物给出互斥指令的情况。

附带的语义重叠（非冲突）：类型层与历史层负向词存在词汇重复，但 merge_negative 去重后不会重复下发，详见 03 文档第六节。

### Q5：精简了多少个模板？

本轮删除 0 个，理由：删模板必须同时满足零调用与无历史价值，而 27 个均有调用、7 个死模板有设计史料价值。

实际精简发生在机制层：

1. 9 个静态内联提示词从代码字符串收编为注册模板（总量 18 → 27，反而增加，但消除了散落）。
2. 负向词从多处内联收编为单一事实源（COMMON + 类型层 + 历史层，唯一合并入口 negative_for）。
3. 可选的后续精简：script_create/expand 公共段抽取（P3-1）、历史负向词层清理（P3-2），均待 A/B 基线。

### Q6：提示词质量有没有变化？

分两个层面，必须分开表述：

功能正确性：已修复 6 项模板级缺陷，均为恢复设计意图而非新增行为：

| 缺陷 | 影响 | 修复提交 |
| --- | --- | --- |
| compile_storyboard_image 丢失 plan_posture_block 注入 | 站位信息静默缺失，多人镜头站位不受控 | 7bab27f |
| scene_plate_compiler 缺内置默认 | 外置文件缺失时直接 FileNotFoundError | 69210d5 |
| parse_system 内外不一致 | on_screen/offscreen 字段与 beats 手别约束丢失 | 62fe6c4 |
| image_prompt 内置版本过旧 | 未回写到 11-22 单帧化新版 | e52d5df |
| video_prompt_first 丢失 11-14 H3 禁止词 | 续镜误把尾帧当首帧复现 | d36cfd7 |
| script_evaluation_system 缺可制作性字段 | 评估输出字段不完整 | e10cb5f |

回归安全性：全量测试从历史 1394 通过提升到本轮 1410 通过（收集 1411），提示词专项 116 项全部通过，无回归。

生成质量（人物一致性、道具连续性、画面稳定性）：本轮未执行 10 类剧本实跑，不能声称提升。这是本报告最重要的未验证项，见第七节。

### Q7：出问题如何回滚？

三层回滚手段，按粒度从细到粗：

```bash
# 1. 回滚单个修复（推荐，粒度最细）
cd /home/ubuntu/director-lab
git revert 7bab27f        # 或任意一个 P0/P1/P2 提交

# 2. 回滚外置模板到内置默认（不改代码）
# 管理后台调用 reset_prompt(name)，或删除 workspace/prompts/{name}.md

# 3. 恢复外置模板的历史编辑版本
# workspace/prompts/{name}.{时间戳}.bak，每个模板自动保留最多 5 份
```

关键保障：内置默认值随 Git 版本管理，外置文件写入自动 .bak，两层都有可回溯的原始版本，不存在改坏无法恢复的情况。

### Q8：修改模板后是否需要重启服务？

不需要。prompt_store 无缓存，每次 get_prompt 重新读文件，修改即时生效。这也是本轮不引入模板缓存的原因（缓存会带来陈旧读风险）。

### Q9：内置与外置模板哪个优先？

外置优先：backend/workspace/prompts/{name}.md 存在则用外置，缺失时回退 DEFAULT_PROMPTS 内置默认值。内置值是安全网，外置文件是当前生效版本。

### Q10：如何追溯某次生成用的是哪个版本的提示词？

job.input_snapshot.prompt_version 记录三元组：

| 字段 | 含义 | 来源 |
| --- | --- | --- |
| template_id | 编译模板 ID | prompt_compiler.TEMPLATE_REGISTRY |
| template_version | 模板版本号 | 同上 |
| template_content_hash | 实际生效模板内容 sha256 前 12 位 | prompt_registry.template_content_hash（外置优先） |

content_hash 由 203f53a 引入：prompt_compiler 的 CompiledPrompt 新增 content_hash 字段，generate.py 的 _attach_compilation 写入 input_snapshot。即使模板内容被修改而版本号未变，也能凭 hash 定位当时的实际文本。

### Q11：如何防止内置与外置再次漂移？

三道防线：

1. 76a2770 新增 3 项内置/外置一致性防漂移测试，覆盖 5 个曾漂移模板。
2. tests/test_prompt_registry.py 的 test_content_hash_tracks_template_change 断言指纹随模板变更，test_template_input_vars_match_actual_placeholders 断言占位符与注册表声明一致。
3. 运行时 content_hash 写入 input_snapshot，漂移可被事后发现。

### Q12：死模板与未注册内联提示词如何处理？

| 对象 | 处理 | 依据 |
| --- | --- | --- |
| 7 个死模板（docs/prompts/prop/） | 保留不注册，加 README 定性 | b78c1dc；对应功能已由 prop_lifecycle.py 纯代码实现 |
| keyframe_image_qc 动态拼接提示词 | 不注册 | 内容由运行时站位数据拼装，固定部分已由 qc_keyframe_prompt 承担 |
| video_workflow 一次性 user 文案 | 不注册 | 一次性消息，注册会引入无收益间接层 |

### Q13：后续还需要做什么？

按风险从低到高（完整定义见 05 文档）：

| 优先级 | 事项 | 前置条件 |
| --- | --- | --- |
| 高 | P3-6 提交粒度纪律（禁止 git add -A） | 已在本周期执行，五次单文件提交均合规 |
| 中 | P3-5 视频双模板同步守护（纯测试） | 已完成（2d0daf2），5 项 + 3 项反向验证 |
| 中 | P3-4 compile_shot_video 占位符 mapping 覆盖测试（纯测试） | 已完成（62a1527），6 项 + 3 项反向验证 |
| 中 | P3-1 方案 2：create/expand 公共段一致性测试 | 已完成（a95ee66），6 项 + 3 项反向验证 |
| 中 | 静态源码断言测试（inspect.getsource）改为行为断言 | 已完成（3626277），改 AST 结构检查 + 2 项反向验证 |
| 低 | P3-2 零影响部分：删除历史层 6 个永不下发重复词 | 已完成（c119c45），五种类型输出逐字符不变 |
| 待定 | P3-1 方案 1 / P3-2 整层删除或合并 / P3-3 单行模板格式化 | 必须先在 06 文档的 A/B 基线就绪（P3-2 的两项实证已证明会改变模型输入） |
| 待定 | 10 类剧本回归实跑（S-01 至 S-10） | 需要真实 LLM 与生成资源 |

---

## 三、变更清单（24 个提交）

| # | 提交 | 内容 | 类别 |
| ---: | --- | --- | --- |
| 1 | 7bab27f | compile_storyboard_image 外置对齐，恢复 plan_posture_block | P0 修复 |
| 2 | 69210d5 | scene_plate_compiler 注册内置默认 | P0 修复 |
| 3 | 62fe6c4 | parse_system 双向合并对齐 | P0 修复 |
| 4 | e52d5df | image_prompt 内置回写单帧化新版 | P0 修复 |
| 5 | d36cfd7 | video_prompt_first 恢复 H3 禁止词 | P0 修复 |
| 6 | e10cb5f | script_evaluation_system 内置回写 | P0 修复 |
| 7 | 8bd460c | prompt_store ensure_registered 集中注册 | P0 修复 |
| 8 | 8e2ad4c | 注册 9 个静态内联提示词（18 → 27） | P1 收编 |
| 9 | 72fc8c2 | 负向词单一事实源 | P1 收编 |
| 10 | b78c1dc | 死模板 README 定性 | P1 文档 |
| 11 | 76a2770 | 内置外置一致防漂移测试 3 项 | P2 测试 |
| 12 | 0b61dc5 | 01 资产盘点清单 | P2 文档 |
| 13 | 203f53a | 注册表模块 + content_hash 追溯 + 6 项测试 | P2 建设 |
| 14 | 3a3ccf4 | 修正 data URI 断言为 JPEG | 测试修正 |
| 15 | 2d0daf2 | P3-5 视频首镜/续镜模板同步演进守护（5 项） | P3 测试 |
| 16 | 62a1527 | P3-4 编译模板占位符与映射覆盖守护（6 项） | P3 测试 |
| 17 | a95ee66 | P3-1 剧本创作/扩写公共规范一致性守护（6 项） | P3 测试 |
| 18 | a77a984 | 06 基线同步（133 项专项、1428 收集、反向验证） | P3 文档 |
| 19 | 3626277 | R-05 静态源码断言改用 AST 结构检查 | 测试加固 |
| 20 | 9db7c25 | 05 计划执行进展同步 | P3 文档 |
| 21 | 5a0bc4f | 07 报告同步（20 提交、133 项专项、R-05 已修复） | P3 文档 |
| 22 | 95df654 | 02 审计占位符为零模板数修正（12 → 8） | P3 文档 |
| 23 | c119c45 | P3-2 清理历史负向词层 6 个永不下发重复词 | P3 重构 |
| 24 | 953efa9 | 05 计划同步 P3-2 实证结论 | P3 文档 |

代码资产变更统计（累计）：

| 文件 | 变更 |
| --- | --- |
| backend/app/services/prompt_registry.py | 新增 129 行（PROMPT_REGISTRY 27 条 + 5 个函数） |
| backend/app/services/prompt_compiler.py | +16 行（content_hash 字段与注入点） |
| backend/app/api/v1/generate.py | +2 行（input_snapshot 写 content_hash） |
| backend/tests/test_prompt_registry.py | 新增 79 行（6 项测试） |
| docs/prompt-audit/ | 7 份文档共 1000+ 行 |

---

## 四、验证证据汇总

| 验证项 | 方式 | 结果 |
| --- | --- | --- |
| 注册表覆盖 27 模板 | test_registry_covers_all_registered_prompts | 通过 |
| 零孤儿模板 | test_registry_has_no_orphan_entries | 通过 |
| 元数据完整性 | test_registry_metadata_complete | 通过 |
| 占位符与声明一致 | test_template_input_vars_match_actual_placeholders | 通过 |
| 指纹随模板变更 | test_content_hash_tracks_template_change | 通过 |
| 快照形状 | test_registry_snapshot_shape | 通过 |
| 提示词专项回归 | 13 个测试文件 133 项 | 全部通过（16.04s） |
| 全量回归（本轮两次） | pytest tests（1411 收集时点） | 1410 通过 + 1 环境性失败（已定性，见 06 文档 3.2） |
| 全量收集（守护测试落地后） | pytest tests --collect-only | 1428 项（另有 4 项来自外部会话未跟踪文件，不计入本轮） |
| 三项守护测试反向验证 | 注入 9 种模拟漂移 | 9/9 全部被捕获，非永远通过 |
| R-05 AST 改造反向验证 | 正常结构 + 2 种漂移 | 正常通过、2 种漂移均被捕获 |
| P3-2 历史层差集分析 | 三层逐词比对（类型层/通用层/历史层） | 历史层含 23 个独有词（character 11 / scene 7 / prop 5），原「整层删除」前提不成立 |
| P3-2 合并方案等价性 | 内存模拟「独有词并入类型层 + 删历史层」 | 去重后词集合等价（82/67/70/70/62 词），但 character/scene/prop 三类输出字符串不一致（词序改变），方案不可用 |
| P3-2 零影响精简（c119c45） | 删除 6 个永不下发的重复词 | 五种类型输出长度 422/337/337/364/311 与修改前逐字符一致；79 项相关测试全部通过（16.14s） |
| 调用图 | 29 处调用点 + 2 处动态派发直读 | 27/27 有调用方 |
| 冲突扫描 | 351 对相似度比较 + diff | 0 真实冲突 |

---

## 五、风险与遗留问题

| 编号 | 问题 | 性质 | 建议 |
| --- | --- | --- | --- |
| R-01 | 203f53a 混入 executor.py / image_quality.py 等范围外改动 | 流程问题 | 已推送不改写历史；后续严格执行 P3-6 |
| R-02 | dc89ed7 同样混入范围外文件（存储测试、任务认领改动） | 流程问题 | 同上 |
| R-03 | 工作区存在外部会话遗留的 executor.py / prompt_qc.py 等未提交改动 | 环境问题 | 非本轮范围，需由对应负责人单独提交 |
| R-04 | 生成质量指标未实测 | 验证缺口 | 执行 06 文档的 10 类剧本回归 |
| R-05 | inspect.getsource 类静态断言测试对并发写入敏感 | 测试健壮性 | 已修复（3626277）：改用 ast.parse 直读磁盘，不依赖行号缓存；正向 5 项通过，反向 2 种漂移均被捕获 |
| R-06 | script_create/expand 结构重复未治理 | 可维护性 | 待 A/B 基线后执行 P3-1 |

---

## 六、交付物清单

| 文件 | 内容 | 行数 |
| --- | --- | ---: |
| 01_PROMPT_INVENTORY.md | 资产盘点清单（36 份资产分类） | 156 |
| 02_PROMPT_QUALITY_AUDIT.md | 质量审计（27 模板全量指标与问题定性 Q-01~Q-07） | 132 |
| 03_PROMPT_CONFLICT_MATRIX.md | 冲突矩阵（351 对扫描，C-01~C-06，0 真实冲突） | 162 |
| 04_PROMPT_CALL_GRAPH.md | 调用图（29 调用点、2 动态派发、27 模板覆盖表） | 205 |
| 05_PROMPT_REFACTOR_PLAN.md | 重构计划（P3-1~P3-6，含风险与回滚、执行进展） | 188 |
| 06_PROMPT_TEST_BASELINE.md | 测试基线（全量记录、133 项专项、A/B 设计、反向验证） | 197 |
| 07_PROMPT_REFACTOR_REPORT.md | 本报告（13 问回答、P3 执行结果、反向验证） | 252 |

---

## 七、结论与未完成声明

已完成：

1. 36 份文本资产全部定性，27 个运行时模板全部纳入注册表管理。
2. 6 项模板级功能缺陷、4 项机制缺陷修复并验证。
3. 提示词版本与内容指纹写入任务快照，历史可追溯。
4. 116 项专项测试 + 1411 项全量回归基线建立。

未完成（不得声称已完成）：

1. 10 类剧本端到端实跑与三项质量指标（人物数量错误率、道具连续性断裂率、场景跳变率）测量。
2. P3-1 方案 1、P3-2、P3-3 三项触碰模型输入的优化（须 A/B 基线支撑）。
3. 工作区外部会话遗留改动的归属确认与提交。

审计结论：本项目提示词体系在机制层已达到可追溯、可回滚、可守护的工程标准；质量层的提升需以 10 类剧本实跑数据为准，当前证据不足以量化。
