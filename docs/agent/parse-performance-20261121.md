# 剧本解析耗时优化（2026-11-21）

## 结论

单集剧本解析任务耗时 **776s → 172s（-78%）**。

同一剧本版本（`sv_916591a10875`）、同一网关（`212.64.16.68:3065`）对比：

| 阶段 | 改前 | 改后 | 说明 |
|---|---|---|---|
| LLM 大纲 | 19s × 3 次 | 18s × 1 次 | 改前同一 job 内被重跑 3 遍 |
| 剧本评估 | 84s × 3 次（全失败） | **0s** | 每次 `eval_skipped`，零产出 |
| LLM 解析 | 600s（6 次调用） | 153s（2 次调用） | — |
| **合计** | **776s** | **172s** | |

## 归因（含一次自我纠错）

### 主力 1：worker 停机被强杀 → 同一任务重跑 3 遍

**这不是 OOM。** 早期判断有误，证据：

- `MemoryCurrent=80MB`（worker 内存完全正常）
- journal 实际是 `Failed with result 'timeout'` → `Killing process with signal SIGKILL`
- `dmesg` 无任何 OOM 记录

关键澄清：776s 是**一个 job 记录内的 3 次 `agent.started`**，不是 3 个 job：

```
15:09:41 agent.started {attempt:1}  → 评估卡住
15:11:12 agent.started {attempt:2}  → 评估卡住
15:14:25 agent.started {attempt:3}  → 终于跑完
```

因果链：

```
SIGTERM
  → scheduler 在 f.result() 上无限等待在途 LLM 任务（单次 100s+）
  → systemd 等满 TimeoutStopSec 发 SIGKILL
  → 进程死掉，DB 里任务仍是 running
  → 下次启动 _recover_interrupted_jobs 无条件把 running 重置为 queued
  → 整个解析从头重跑
```

历史上有过一个 `10-fast-stop.conf` 把 `TimeoutStopSec` 压到 15s 想"快速停机"，
但那只是把症状从「停不掉」换成「被杀掉」，重跑问题依旧。

→ 修复（`worker/scheduler.py`）：
1. 收到停止信号后先对在途任务发 `job_cancel.request()`（DB 轮询 + 掐断 socket，让 LLM 调用尽快返回）
2. 按 `worker_shutdown_grace_seconds`（默认 20s）**有界**等待，超时即放弃
3. `pool.shutdown(wait=False)`
4. `TimeoutStopSec` 15s → **45s**（必须 > grace，留出取消生效余量）

**契约**：`worker_shutdown_grace_seconds` 必须小于 systemd `TimeoutStopSec`。
改任一侧都要同步检查另一侧，`tests/test_script_agent_perf.py` 有断言锁定。

### 主力 2：评估阶段 84s 零产出

`script_evaluation.evaluate_script` 要求 7 个必需字段，模型持续给不全，每次都以
`agent.eval_skipped: 评估报告缺少必需字段` 结束。占单集任务 32% 时间，且评估报告
**没有任何下游消费方**（解析页职责是拆解剧本，不是剧本评审）。

→ 新增 `script_evaluation_enabled`（默认 False）。关闭时写
`agent.eval_skipped` 事件保留可观测性，而不是静默跳过。

### 已撤销的结论：thinking 开关（无效）

初版文档曾写「关闭 thinking 使 completion 从 16000 降到 2312，快 3.1 倍，是最大单点」。
**该结论已撤销**，多次对照实验证伪：

| 参数 | `reasoning_content` 字符数 | 判定 |
|---|---|---|
| 无参数 | 8351 | 基线 |
| `thinking:{type:disabled}` | 9535 | **无效** |
| `enable_thinking:false` | 6558 | **无效** |
| `reasoning_effort:minimal` | 12606 | **无效** |
| `chat_template_kwargs:{enable_thinking:false}` | 18653 | **无效** |

**该网关不遵守任何关闭思考的参数**，`reasoning_content` 始终存在。
最初那次「17.6s vs 54.3s」是单次采样的噪声，不是因果。

`llm_disable_thinking: true` 保留（无害，且对支持该参数的端点有效），但**不能计入收益**。

### 关于 16000 上限：不是恒定硬上限，而是间歇性的

多次重复同一 `max_tokens` 得到不同结果：

```
max=32768  → comp=8109   JSON 闭合
max=32768  → comp=16000  JSON 未闭合   ← 撞上
max=65536  → comp=10725  JSON 闭合
max=65536  → comp=10402  JSON 闭合
max=128000 → comp=14165  JSON 闭合
```

推断：网关把请求路由到不同后端集群（响应体含 `"cluster":"normal_context"`），
不同集群预算不同。**所以「调大 max_tokens 一定解决」和「关 thinking 一定解决」
两条路都不成立。**

实际缓解手段是降低输出体积（见下）与把 `llm_max_tokens` 初始值抬高，
让首次尝试就有足够预算，避免「撞上限 → 重试」浪费一整轮（实测每轮 77s）。

## 检查后确认无需修改的项

- **场景板缓存已生效**：9 个场景中 8 个已有 `plate_text`，缓存命中正常。
  唯一为空的 `scn_6cad0c4f3d36`（海滩）是解析失败那集的残留。
- **`llm_parse_segment_chars=6000`** 未触发分段：单集正文 2369 字 < 6000。

## 测试

新增 `tests/test_script_agent_perf.py`（9 passed）锁定：
- 评估默认关闭 + 运行时开关 + settings API 暴露
- grace 持久化 + `grace < TimeoutStopSec(45s)` 契约
- `f.result(timeout=)` 必须抛 `FutureTimeout` 而非静默返回（有界等待的核心）
- `job_cancel` 能打断在途任务且 `clear` 后复位
- thinking payload 形状（注意：只锁客户端行为，不锁网关是否遵守）

回归：`test_script_agent_long_script.py` 里 `_Settings` 桩需显式
`script_evaluation_enabled = True`（该文件专门测评估链路），已补。
全量相关回归 **108 passed / 0 failed**。

## 剩余可优化项：提示词不该由 LLM 自由发挥

实测该次解析结果 36506 字节里：

```
image_prompt 合计 6034 字  占 17%
video_prompt 合计 9990 字  占 27%
两者合计      16024 字  占 44%
```

**44% 的输出是提示词，而提示词本来不该由 LLM 自由发挥：**

- 资产库提示词：`compile_asset_image` 已是纯模板渲染，**0 次 LLM**
- 分镜图提示词：`STORYBOARD_TEMPLATE` 已把景别/机位/站位/参考绑定全部结构化，
  LLM 实际只填了 `action` 一个槽位
- 分镜视频提示词：`VIDEO_TEMPLATE` 同理，且 `normalize_clip_dialogue` 还要
  事后清洗 LLM 写歪的台词格式

即：让 LLM 重写一遍模板已经能拼出来的东西，再用代码清洗它写歪的部分。
这既是那 44% 的来源，也是撞 16000 的根源。

### 已实测验证：去掉提示词后不截断

用同一剧本、同一网关，system prompt 改为「只出结构化事实，**不要输出
image_prompt 与 video_prompt**」：

| | 现行（含提示词） | 去提示词版 |
|---|---|---|
| 调用次数 | 2 次（第 1 次截断） | **1 次** |
| 耗时 | 85s + 71s = 156s | **49s** |
| completion | 16000（撞上限）/ 13958 | 10773 |
| JSON 闭合 | 第 1 次否 | **是** |
| 产出 | 3 场景 / 7 镜 | 12 场景 / **12 镜** |

**去提示词版不仅快 3 倍，产出的镜头还更多**（7 → 12）——因为不再被截断。
这直接证明：截断不是「模型能力不足」，是「输出体积超过网关上限」。

### 已修复的阻塞：空提示词导致模板被丢弃

改造前必须修的一个真实 bug（已修复 + 4 个测试锁定）：

`generate.py` 入参组装是 `params.setdefault("prompt", shot.image_prompt or "")`。
当 `image_prompt` 为空时，`prompt` 键**已存在且为空串**，于是
`_compile_shot_prompt` 里 `if "prompt" not in out` 恒为 False，走进
`_apply_compiled_media`——而该函数原本只合并参考图槽位、不碰提示词，
结果编译出的模板（实测 1026 字）被整段丢弃，工作流收到**空提示词**。

实测对象 `sht_045190812cb5`：修复前 `params['prompt']` 长度 **0**，
修复后 **1026** 字。

两段式改造后 `image_prompt` 恒为空，这条分支是**必现路径**，所以必须先修。

→ 下一步：LLM 只产出结构化事实（beats/dialogue/emotion/state/shot_purpose），
提示词由 `STORYBOARD_TEMPLATE` / `VIDEO_TEMPLATE` / `compile_asset_image` 渲染。
输出量可降到 ~10000 字，间歇性截断自然消失。

## 两段式改造：已实施（2026-11-21）

### 改了什么

| 位置 | 改动 |
|---|---|
| `workspace/prompts/parse_system.md` | §七 改为「不要输出 image_prompt 与 video_prompt」 |
| `script_parse.PARSE_SYSTEM_PROMPT` | 内置默认值同步（实际生效的是上面那个 md） |
| `script_parse._assemble_image_prompt()` | **新增**：image_prompt 为空时按模板渲染 |
| `script_parse` 落库处 | `image_prompt=sh.get("image_prompt") or _assemble_image_prompt(sh)` |
| `prompt_compiler._shot_props()` | 改为优先读结构化字段 `shot.props`，提示词正则仅兜底 |
| `generate._apply_compiled_media()` | 空提示词用编译结果填充（见上节） |

**踩到的坑（重要）**：`get_prompt()` 优先读 `workspace/prompts/parse_system.md`，
只改代码里的内置默认值是**无效的**。第一次改完重跑，LLM 仍在写提示词
（image_prompt 2559 + video_prompt 5057 字），因为生效的是 md 文件。
`tests/test_two_stage_parse.py` 里加了断言锁住这一点。

### 为什么要改 `_shot_props`

它原本从 `shot.image_prompt` 正则提取 `@道具名`。LLM 不再写提示词后，
正则提取不到任何道具、**道具参考图会静默全部丢失**。
`Shot.props` 是解析阶段就落库的结构化字段（实测 `["帆布包"]`），应作权威来源。

### 实测结果

| 指标 | 改造前 | 改造后 |
|---|---|---|
| LLM 写提示词字节 | 16024 字（44%） | **0** |
| 解析出的镜头数 | 7 | **14** |
| 单次调用耗时 | 86s + 79s | 86s + 79s |
| 总耗时 | 172~191s | 181s |

**核心收益是镜头数翻倍（7 → 14）**，不是耗时：不再被提示词挤占输出预算，
同一份剧本能拆出更多镜头，下游分镜密度直接提升。

耗时没有明显下降，原因已查明：`reasoning_tokens` 波动极大
（attempt1 = 11950，attempt2 = 6487），reasoning + content 一起计入
completion 的 16000 上限。reasoning 高的时候仍会撞上限并重试。
这个波动无法从客户端控制（所有关闭思考的参数都被网关忽略，见上文）。

### 端到端验证（真实数据，已回滚）

```
落库前 shot 数: 11
apply stats: shots_created=6, shots_updated=8, asset_plans.keyframe=17, clip=17
落库后 shot 数: 17
  seq1 ip=761 vp=887   seq2 ip=745 vp=1109
  seq3 ip=724 vp=1089  seq4 ip=707 vp=1072
第一条 image_prompt: 「任务：生成第 1-1 镜的单帧分镜图 【本镜剧情】夏日晚风中，
江屿与林晚并肩从沙滩一侧缓步走向海边…」
```

模板渲染链路完整生效，下游 6 处依赖（prompt_qc / shot_refs /
reference_resolver / asset_plan 道具绑定 / prop_lifecycle / regeneration）
全部拿到非空提示词。

### 测试

- `tests/test_two_stage_parse.py`（6 passed）：提示词禁令生效、normalize 不伪造字段、
  渲染覆盖关键分段、缺字段不崩、video_prompt 兜底不被破坏
- `tests/test_shot_props_source.py`（8 passed）：结构化字段优先、JSON/逗号串解析、
  历史数据兜底、未知道具不崩
- 相关回归 **61 passed / 0 failed**

### 遗留

`reasoning_tokens` 波动导致的首轮截断（约 86s）目前无法从客户端消除。
若后续要压这 86s，方向是给网关换一个 reasoning 更稳定的模型，
或把单次输出进一步切小（`llm_parse_segment_chars` 调低，强制分段）。


### initial_max_tokens 已上调（但仍非充分条件）

`script_agent._run_segment` 的 `call_with_retry` 现在显式传
`initial_max_tokens=llm_parse_max_tokens`（65536），不再从 `llm_max_tokens`（32768）起步。

实测效果有限：首轮仍然在 16000 处截断（`max=65536 comp=16000`），
说明**该上限与请求参数无关，由网关强制施加**。这一项只是把重试预算提前，
不能单独消除截断——真正的解是降低输出体积。


## 对"三 card 拆分"方案的影响

原始诉求是按「资产库提示词 / 分镜图提示词 / 分镜视频提示词」拆三个 agent card。
核查代码后的修正：

| Card | 实际成本 | 结论 |
|---|---|---|
| ① 资产库提示词 | `asset_plan._compiled_asset_prompt` → `compile_asset_image`，**纯模板渲染，0 次 LLM** | 不需要 agent，给它加状态机/停止按钮是给 0ms 操作建队列 |
| ② 分镜图提示词 | 生成时下发的是 LLM 写的 `shot.image_prompt`（`_apply_compiled_media` 只合并槽位，不改写提示词） | 与③同源 |
| ③ 分镜视频提示词 | 同理，下发 `shot.video_prompt` | 与②同源 |

门禁也不用新写：`stage_readiness.py` 的三阶段判定 + `GET /stage-readiness/{stage}`
已就绪，前端 `useStageTransition.ts` 在用。

**正确的拆分维度是「产出物性质」（事实 vs 文本渲染），不是「页面 card」。**


## 生产库最终确认（2026-11-21）

真实任务 `job_24846c12f34f`（`sv_916591a10875`、`episode_start=1`、`force=true`，
23:47:33 → 23:50:34，**181s**）：

| 检查项 | 结果 |
|---|---|
| LLM 返回的 shots | 14 条，`image_prompt` 非空 **0**、`video_prompt` 非空 **0** |
| 落库 `shot` 表 | 43 条，`image_prompt` 非空 **43/43**、`video_prompt` 非空 **43/43** |
| 抽样 `sht_c9621c5e09a3` | image_prompt **761** 字 / video_prompt **887** 字 |
| 内容来源判定 | 含 `任务：生成第 1-1 镜的单帧分镜图`、`【本镜剧情】`、`【人物动作】`、`【出场角色】`、`【身份约束】` 等模板锚点 → 由 `_assemble_image_prompt` / `_assemble_video_prompt` 渲染 |
| `shot.props` 结构化字段 | 正常（`["帆布包"]` / `["兔子玩偶"]` / `[]`），`_shot_props` 取到道具 |

结论：**两段式闭环在生产数据上成立** —— LLM 侧提示词产出为 0，落库后 100% 由代码模板渲染。

### 前端同步改动（已构建部署）

| 文件 | 改动 |
|---|---|
| `views/project/ParseView.vue` | 计时改用 `parseServerTime()` 把无时区串按 UTC 解析（修「刚点开始就显示 08:00:26」）；阶段改为读取后端 `agent.stage` 事件而非前端猜 JSON；新增 `cancelJob` 停止解析 |
| `api/projects.ts` / `api/jobs.ts` / `types/job.ts` | 对接 `episode_start` / `force` / `cancelJob` |
| `components/script/ScriptStageBar.vue`、`ScriptEditor.vue`、`useProjectFlow.ts`、`ScriptView.vue` | 阶段条与流程状态对齐 |

验证：`npm run build` ✓ 10.2s（nginx 直接服务 `dist/`，已生效：
`index-CONgv_-r.js` → `ParseView-C-swvR-l.js`）；
`check:colors` 硬编码色值 **0 处**、`check:breakpoints` 全部标准档（1100/768/480/min-769/min-1101）、
`check:contrast` 全部文本令牌 ≥ 4.5:1。


## 顺带修掉的两个真 bug（回归发现，非本次改造引入）

### 1. `asset_plans.py` NameError —— 分镜图批量生成 500

`POST /api/projects/{id}/asset-plans/generate`（`plan_type=keyframe`）直接 500：

```
NameError: name 'declared_reference_slots' is not defined
app/api/v1/asset_plans.py:328
```

`declared_reference_slots` 只在 `_default_image_workflow()` 函数体内（第 47 行）
做了局部 import，但第 328 / 348 行在别的函数里裸用。来自 `990aea1`。
修法：import 提到模块顶部。验证 `test_keyframe_generate_after_assets_ready`
修复前 NameError → 修复后 **1 passed（69.75s）**。

### 2. 「本镜主体角色」有三份互相冲突的定义 —— 分镜图生成被误判 409

同一概念在三处各写一份，逐渐分叉：

| 位置 | 原判定口径 |
|---|---|
| `reference_resolver`（注入器） | `character_id` 或 名字在 `image_prompt` 或 **名字在 `dialogue` 正文** |
| `prompt_compiler.audit_reference_slots`（硬门禁） | `character_id` + 对白说话人 |
| `prompt_compiler._shot_characters`（编译器） | `character_id` + 对白说话人 |

真实故障（`test_pony_e2e`）：镜头 2「@小马 向牛伯伯询问河水深浅」，
`牛伯伯` 只出现在台词正文「牛伯伯，河水深不深？」里。注入器据此判定他在镜内，
把他塞进 `ref_character_2`；门禁按自己的主体集合判定 → `identity_reference_leak`
→ **整个生成请求 409，分镜图出不来**。

修法：新增 `shot_refs.shot_subject_characters()` 作为**唯一**口径，
构成 = `character_id` ∪ 对白分段说话人 ∪ 提示词（图+视频）显式 `@引用`。
刻意**不**含「名字出现在 dialogue 正文」的子串匹配 —— 台词里被谈论的人
不等于在画面里（「妈妈，我能把麦子驮到对岸吗」会让妈妈凭空占一个身份槽）。
注入器、门禁、编译器三处全部改为委托这一个函数。

新增 `tests/test_shot_subject_characters.py`（6 passed）锁定：
- 只在台词正文出现的名字不算主体角色
- 对白分段说话人算主体角色
- 提示词 `@引用` 算主体角色（图/视频提示词都算）
- 注入器与门禁对同一镜头得出相同主体集合
- **门禁仍拦住真泄漏**（非主体角色图进身份槽照样 `identity_reference_leak`）——
  放宽口径不等于取消防护

### 测试语义同步（代码对、测试过期）

`test_prompt_and_ref` 2 个、`test_ref_and_parallel` 1 个断言的是旧语义
「工作流未声明槽位 → 422」，而 `ref_inject.py` 模块注释早已改为
「未声明某类槽位 → 该类 @引用降级为文字描述，只有『声明了槽位却装不下』才 422」。
且 `None` 的语义也变了：从「不限制定位表」改为「工作流未声明任何槽位，一律不注入」
（原因：默认槽位表会把 `ref_scene` 当通用槽位，给 H3 Ref2VA 注入它不存在的输入参数）。
测试已按现行语义更新。

### 回归结果

| 阶段 | 结果 |
|---|---|
| 修复前全量 | 1146 passed / **5 failed** |
| 解析相关 10 文件 | 87 passed |
| 受影响 8 文件（角色口径 + 参考注入 + 资产计划） | **60 passed** |
| **修复后全量** | **1157 passed / 0 failed（760s）** |

5 个失败全部清零，且用例总数从 1146 涨到 1157（新增 6 个主体角色口径测试 + 5 个此前被 NameError 阻断的资产计划测试）。
