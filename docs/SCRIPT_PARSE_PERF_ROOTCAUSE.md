# 剧本分析耗时优化：实测根因与验证报告

> 日期：2026-11-21
> 范围：`backend/app` 剧本解析链路（大纲 → 评估 → 分批解析 → 合并）
> 结论：**真正的根因是模型输出上限 16000，不是输出冗余、不是思考链、不是评估阶段**

---

## 一、结论摘要

| 项目 | 改前 | 改后 | 变化 |
|---|---|---|---|
| 模型 | `deepseek-v4.1-flash` | `deepseek-v4.1-flash-max` | 输出上限 16000 → 64000 |
| 单集解析尝试次数 | 2 次（首轮必截断） | **1 次** | -1 次完整请求 |
| 单集解析耗时 | 181s（86s 白跑 + 79s 重试） | **105.7s** | **-75s（-42%）** |
| `llm_parse_max_tokens` | 65536（无效，超模型上限） | 64000（与能力一致） | 配置不再误导 |
| `llm_parse_hard_max_tokens` | 131072（无效） | 64000 | 同上 |

**一句话**：之前每一次解析都在做无用功——首轮 86 秒跑到网关 16000 token 硬上限被静默截断（`finish_reason` 还谎报 `stop`），然后代码把 `max_tokens` 翻倍到 65536 重试，网关照样停在 16000，但这次运气好碰上了"间歇性"上限没触发，于是成功。换掉模型后首轮直接成功。

---

## 二、实测证据

### 2.1 历史任务数据（`agent.parse_attempts` 事件）

从 `job_event` 表提取所有 script_agent 任务的解析尝试记录：

```
job_24846c12f34f  181s  首轮 compl=16000 reason=11950 truncated → 次轮 14650 ok
job_95f1792687c0  275s  首轮 compl=16000 reason=16000 empty → 16000 truncated → 13147 ok
job_6f560368a302  191s  首轮 compl=16000 reason=5825  truncated → 15500 ok
job_ebb8027265cb  166s  首轮 compl=16000 reason=9400  truncated → 13958 ok
job_91e9d46e99f6  172s  首轮 compl=16000 reason=6812  truncated → 15048 ok
job_f10fd4c2a068  872s  FAILED，9 次尝试，每批首轮全部 16000 truncated
job_dda7f28ce81e  776s  7 次尝试，首轮全部 16000 truncated
```

**规律**：几乎每个任务的首次尝试都精确停在 `completion_tokens=16000`，且 `reasoning_tokens` 占 4000–16000。

### 2.2 网关模型能力（`GET /v1/models`）

网关自己声明：

```
deepseek-v4.1-flash       in=116000    out=16000    credits=x0.06
deepseek-v4.1-flash-max   in=1000000   out=64000    credits=x0.06   ← 同价
```

对照项目配置 `llm_parse_max_tokens=65536` / `hard=131072` —— **两个值都在模型硬上限之上，永远不可能生效**。这就是为什么 `llm_retry.py` 的"截断就翻倍 max_tokens"策略在这个模型上完全空转。

### 2.3 直接压测（绕过业务代码）

同一个"生成 40 个分镜、每镜写满"的请求：

```
flash      max_tokens=131072   dur=132.4s  compl=15371  reason=2193   json=YES
flash      max_tokens=131072   dur= 85.1s  compl=16000  reason=5997   ← 精确撞 16000
flash-max  max_tokens=131072   dur=157.5s  compl=16941  reason=2112   json=YES  ← 突破
```

`flash` 两次分别停在 15371 和 16000 —— 16000 是硬墙。`flash-max` 达到 16941，证明上限确实不同。

### 2.4 真实剧本 A/B（同一份 4326 字剧本，走 `call_with_retry`）

```
BASELINE  flash       total=100.4s  attempt=1 compl=16000  reason=8421   ← 撞墙
CANDIDATE flash-max   total=143.6s  attempt=1 compl=22433  reason=13636  ← 未撞墙
```

`flash` 在真实剧本上同样精确停在 16000。

### 2.5 切换后端到端验证

```
model=deepseek-v4.1-flash-max  parse_max=64000  hard=64000
### RESULT total=105.7s  ok=True  attempts=1
   #1 max_tokens=64000  compl=16662  reason=8527  finish=stop  dur=105718ms  ok=True
```

**首轮一次成功，`attempts=1`，105.7s**。对比改前 181s（2 次尝试）。

---

## 三、思考模式无法关闭（方案 P0-1 走不通）

方案建议 `llm_disable_thinking=true` 优先处理。实测该网关**五种参数全部无效**：

```
默认（不传 thinking 字段）      reasoning_tokens=98
thinking.type=disabled         reasoning_tokens=49   ← 约减半，但没归零
max_tokens=131072              reasoning_tokens=49
reasoning_effort=low           reasoning_tokens=46
enable_thinking=false          reasoning_tokens=58
```

`thinking.type=disabled` 只能把思考量减半，无法关闭。当前配置已经是 `llm_disable_thinking=True`，但实测 `reasoning_tokens` 仍占 `completion_tokens` 的 50%+。

**结论**：这条优化路径在这个网关上收益有限，不能作为主要手段。

---

## 四、方案中已完成的部分（基于旧代码的误判）

原方案把以下内容当作待办，实际代码里已经落地：

| 方案项 | 实际状态 | 证据 |
|---|---|---|
| P1 砍输出量（称"收益最大"） | ✅ 已完成 | `parse_system.md` 第 27 条：「**不要输出 image_prompt 与 video_prompt**」，由 `_assemble_image_prompt()` 模板渲染 |
| P1 前置调研「image_prompt 下游用途」 | ✅ 已有答案 | `script_parse.py:850` 注释列出 6 处依赖，全部已由模板覆盖 |
| P1「@道具名引用改结构化字段」 | ✅ 已完成 | `prompt_compiler.py:848` `_shot_props` 优先用 `shot.props` |
| 评估阶段耗时 | ✅ 已关闭 | `script_evaluation_enabled=False`；代码注释记录实测：776s 中评估占 84s（32%）零产出 |
| 按错误码分流重试 | ✅ 已有 | `llm_retry.py` 完整实现 |
| 部分批次失败保留其余 | ✅ 已有 | `script_agent.py` 失败区间写入 `job.result` |
| 失败粒度 | ✅ 比方案更细 | 截断时递归二分正文（depth≤2）重解析再合并 |

---

## 五、仍待推进的项

| 优先级 | 项 | 说明 |
|---|---|---|
| **P0** | 模型能力校验 | 解析前查 `/v1/models` 比对 `max_output_tokens`，配置超限告警。本次坑的本质是配置与供应商能力脱钩 + 网关静默截断 |
| P1 | `agent_first_batch_auto = false` | 当前 `true` 只跑首批；改 `false` 才能单次跑完全部批次、`llm_parse_concurrency` 才生效。**仅对多集剧本有效** |
| P2 | 大纲改流式 | 当前 15s 无进度事件（小问题） |
| P2 | `agent.llm_delta` 落库 | 每秒一条 `job_event`，剧本越长回放越慢 |
| P2 | chunker fallback 排查 | 实测 `agent.chunking` 出现 `mode:fallback` 且只识别出 1 批，长剧本会退化为"整本发单批" |
| P3 | normalize/write 加输入预检 | 目前无预检，输出上限 32768 |
| P3 | 前缀缓存验证 | 用 `prompt_cache_hit_tokens` 验证命中率 |

---

## 六、改动清单

### 配置（`backend/data/runtime_settings.json`）

```diff
- "llm_model": "deepseek-v4.1-flash",
+ "llm_model": "deepseek-v4.1-flash-max",

- "llm_parse_max_tokens": 65536,
- "llm_parse_hard_max_tokens": 131072,
+ "llm_parse_max_tokens": 64000,
+ "llm_parse_hard_max_tokens": 64000,
```

`llm_instances_json` 内实例 `model` 同步改为 `deepseek-v4.1-flash-max`。

备份：`backend/data/runtime_settings.json.bak-model16000`

### 服务重启

```bash
sudo systemctl restart director-lab-backend director-lab-worker
```

### 验证

```bash
curl -s http://127.0.0.1:8000/api/settings | python3 -m json.tool | grep -E 'llm_model|parse_max|hard_max'
```

---

## 七、风险与回滚

**风险**：
- `flash-max` 是 `reasoning_source: undeclared`（网关未声明思考行为），实际表现需持续观察
- 64000 是网关自述上限，实际可用值可能更低；若遇截断，`llm_retry` 的二分降级仍然生效
- 同价（x0.06 credits）已确认，成本不变

**回滚**：
```bash
cp backend/data/runtime_settings.json.bak-model16000 backend/data/runtime_settings.json
sudo systemctl restart director-lab-backend director-lab-worker
```

---

## 八、方法学备注

原方案第七节要求"先取数据再动手"是对的，但数据取出来后的判读规则需要修正：

| 方案判读规则 | 实测修正 |
|---|---|
| `reasoning_tokens` 占大头 → 优先关思考 | 该网关**关不掉**，此路不通 |
| `finish_reason=length` 且接近 max_tokens → 输出量过大 | 网关**谎报 `stop`**，不能靠 `finish_reason` 判断截断，要看 `completion_tokens` 是否精确等于某个整数（16000） |
| `duration_ms` 大而 token 不多 → 供应商排队 | 本案例是 token 打满 + 重试，不是排队 |

**新增判据**：`completion_tokens` 精确落在整数（16000/32000/64000）→ 命中模型硬上限，先查 `/v1/models` 的 `max_output_tokens`，而不是调 `max_tokens`。
