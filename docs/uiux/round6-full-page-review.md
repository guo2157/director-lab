# 第六轮：全站页面视觉复核（12 页 × 5 档）

> 承接 `round5-data-and-content.md`。第五轮只覆盖 4 页（首页/解析/资产库/分镜看板），
> `acceptance-report.md` 明确记录其余页面为未完成项。本轮补齐全站 12 页复核。

## 1. 范围与方法

### 1.1 覆盖面

从 `src/router/index.ts` 读出全部真实路由，脚本覆盖 12 页（另 3 条为 modal / 404 重定向）：

| key | 路由 | 根选择器 |
|---|---|---|
| home | `/` | `.project-card` |
| script | `/project/:id/script` | `.script-page` |
| parse | `/project/:id/parse` | `.result-panel` |
| assets | `/project/:id/assets` | `article` |
| storyboard | `/project/:id/storyboard` | `.shot-card` |
| sb-images | `/project/:id/storyboard-images` | `.gen-bar` |
| compose | `/project/:id/compose` | `.compose-page` |
| series | `/series` | `.series-grid` |
| series-detail | `/series/:id` | `.series-detail-page` |
| jobs | `/jobs` | `.jobs-page` |
| settings | `/settings` | `.settings-page` |
| director | `/director` | `.console-page` |

`ready` 一律取各 view template 首行的根 class，而不是业务节点 —— 业务节点依赖数据，空数据页会超时。

### 1.2 判据

在原有「横向溢出 / 按钮被裁」之外新增两项：

| 判据 | 阈值 | 为什么需要 |
|---|---|---|
| `errHits` | 出现后端报错原文即 FAIL | 页面在真实数据下可能是坏的，但布局是好的 |
| `textLen` | 正文 < 20 字符即 FAIL | 白屏不会报任何错，只能靠正文长度抓 |
| `inScrollerClipped` | WARN 级，不计失败 | 见 §4.3，可滚动容器内的元素不算被裁死 |

错误原文关键词：`接口不存在`、`加载失败`、`请求失败`、`Network Error`、`undefined is not`、`Cannot read propert`。

## 2. 第一轮结果：60 张，3 张 FAIL

```
FAIL  script    390x844  clipped=1
FAIL  jobs      390x844  clipped=15
FAIL  settings  390x844  clipped=2
```

关键观察：三张的 `scrollW` 都是 `390/390`，**没有页面级横向溢出**。
说明按钮不是被视口裁掉，而是被**内部容器**裁掉。这个区别决定了修法完全不同。

## 3. 根因：三个页面各不相同

用 `probe-clipped.mjs` 打印每个被裁按钮的右边界、溢出量、最近可滚动祖先：

| 页面 | 被裁 | 溢出 | 可滚动祖先 | 性质 |
|---|---|---|---|---|
| script | 1 | 80px | **无** | 真 bug，按钮点不到 |
| jobs | 15 | 355px | `n-data-table` 724/252 | 表格内滚动 |
| settings | 2 | 269–309px | `settings-page` 699/390 | 表格内滚动 |

### 3.1 script：flex-wrap 对单个超宽项无效

实测几何：

```
script-page     w=390  scrollW=470
script-header   w=390  flexWrap=wrap        ← 已经设了 wrap
header-actions  w=454  flexShrink=0  minWidth=auto
```

`.header-actions` 有 `flex-shrink: 0` + `min-width: auto`，宽度被内容撑到 **454px**，
父容器只有 390px。父级的 `flex-wrap: wrap` **对「单个本身就超宽的 flex 项」不起作用** ——
它在换行前就已经溢出了。

修法（`ScriptView.vue` @768）：

```css
.script-header { flex-direction: column; align-items: stretch; flex-wrap: nowrap; }
.header-actions { flex-shrink: 1; min-width: 0; flex-wrap: wrap; justify-content: flex-start; }
```

两个属性必须同时改：`flex-shrink: 0 → 1` 让它可收缩，`min-width: auto → 0` 解除内容宽度下限。

### 3.2 jobs：n-data-table 列宽无法用 CSS 重排

6 列合计 724px（归属 200 + 类型 76 + 状态 148 + 耗时 84 + 创建时间 124 + 操作 92），
容器只有 252px。列宽由 `colgroup` 决定，**纯 CSS 无法把 `<tr>` 变成卡片**（边框、colspan 都在组件内部）。

选择「真降级为卡片」而不是「加滚动提示」的理由：
操作按钮在滚动区最右侧，即使有提示，用户仍需先滑动才能操作 —— 这是「能看见但难用」。
（对比：第五轮分镜看板加滚动提示是对的，那里是图片浏览，横向滑动本身是自然交互。）

实现：`isNarrow`（768 断点，与 `check:breakpoints` 白名单一致）切卡片视图，
复用已有的 `jobContext` / `statusLabels` / `typeLabels` / `fmtDuration` / `fmtTime` / `isStalled`，
**零新业务逻辑**。错误原因改为就地展开（窄屏无 hover，tooltip 不可达），并单独补了卡片视图分页器。

### 3.3 settings：自绘 CSS grid，纯 CSS 可降级

两张表都是 `.wf-thead` / `.wf-tr` 自绘 grid，不是 Naive 组件：

```
实例表    minmax(200px,1fr) 90px 70px 120px 120px  → 最小 600px
工作流表  minmax(220px,2fr) 220px 180px auto       → 最小 620px
```

修法：`thead` 隐藏 + 每行变卡片 + 列名由 `data-label` 的 `::before` 提供。
**零 JS**，只加了 `data-label` 属性和一段 media query。

## 4. 三个「静默失效」的坑

这三处都不报错、不警告，只是不生效，是本轮排查的主要成本。

### 4.1 `.n-card__content` 不存在

Naive UI 的真实类名是 **`.n-card-content`（单横线）**。
写成 `.n-card__content`（双下划线，Vue 3 常见习惯）会静默失效。

后果：390px 下统计卡保持默认 `padding: 20px 24px`，4 张卡占 206px（首屏 24%）。
修正后 206 → 144px。

同一个文件 557 行本来就有同类教训注释（`.n-statistic-label` vs `.n-statistic__label`），本轮又踩一遍。
**排查方法**：DOM 树逐层打印 `className + padding`，不要靠猜。

### 4.2 `flex: 0 1 auto` 不收缩

筛选轴 `.type-tabs` 改横向滚动时，只加 `overflow-x: auto` 不够。
实测宽度链：

```
type-tabs       w=529  sw=527  cw=527   flex=0 1 auto   ← 基准宽=内容宽，拒绝收缩
filter-row      w=565                                    ← 被顶破
wf-toolbar      w=252  sw=565                            ← 被顶破
n-card-content  w=300  sw=589                            ← 被顶破
```

必须 `flex: 1 1 0%`（basis **0** 而非 auto），才能在剩余空间里定位、由 `overflow-x` 自己滚。
修正后 `type-tabs w=216 cw=214 sw=527`，父链全部恢复。

### 4.3 量测判据本身是错的

筛选轴改成横向滚动后，7 个按钮必然「右边界超视口」，但它们完全可点。
原判据把「超视口」一律算被裁 → 误报 FAIL。

改为：**祖先链上存在可横向滚动容器（`overflow-x: auto|scroll` 且 `scrollWidth > clientWidth`）→ 不算失败**。
同时另记 `inScrollerClipped` 作为 WARN 级信号，避免信号完全丢失 ——
如果这个数很大，说明窄屏布局其实没真正适配，只是靠横向滚动兜住了。

## 5. 首屏空间：390×844 实测

修复前：

```
header    top= 82  h=111
stats     top=217  h=206   ← 4 张卡吃掉首屏 24%
filters   top=479  h=240   ← 15 个按钮 wrap 成 5 行，吃掉 28%
首张任务卡 top=731          ← 只剩 113px，卡片露出半张
```

修复后：

```
header    top= 82  h=111
stats     top=217  h=144
filters   top=414  h= 88   ← 单行横向滚动（两条轴各一行）
首张任务卡 top=514          ← 上移 217px
```

处理方式：
- 统计卡：压缩 padding（`.n-card-content`）与字号，`border-radius` 降到 md
- 筛选轴：`flex-wrap: nowrap` + `overflow-x: auto` + `flex: 1 1 0%`，隐藏滚动条

筛选保持**两条轴各一行**（类型 / 状态），不是合并成一行 ——
两条轴是独立维度，合并会让用户分不清哪个是主筛选。

## 6. 失败任务：用户看不到失败原因

### 6.1 发现的真实问题

DB 实测失败任务分布：

```
comfy_unreachable      23   环境错误
llm_invalid_output     12   LLM 输出非法 JSON —— 不是环境错误
llm_output_truncated    3   LLM 输出截断 —— 不是环境错误
comfy_execution_error   2
```

前端 `isEnvError()` 实现是 `return job.retryable === true`，
而后端 `retryable` 的定义（`api/v1/jobs.py:29`）只是：

```python
retryable = j.status in ("failed", "lost") and (j.attempts or 0) < settings.job_max_attempts
```

**跟「是不是环境错误」没有关系**。后果：表格视图里「查看原因」分支（给不可重试任务用的）
对 `llm_invalid_output` 这类配置错误永远走不到，全部显示成「重试」，
用户看到「失败 + 重试」但永远不知道错在哪。

这是本轮之前就存在的问题，非本轮引入。

### 6.2 卡片视图的处理

不改 `isEnvError` 语义（那属于要不要动既有判定的决定），而是在卡片视图里绕开：

- 失败卡同时给「重试」+「查看原因/收起原因」
- 失败原因**常驻展示一行摘要**（`truncateError` 截 60 字），点开看全文
- 摘要前加 `error_code → 中文` 映射（`errorHints`），后端原文放第二行

中文映射只解释「是什么问题、该做什么」，原文完整保留供排查：

| error_code | 中文 |
|---|---|
| comfy_unreachable | ComfyUI 实例连不上，检查实例地址或重新启用 |
| comfy_execution_error | ComfyUI 工作流执行报错，检查节点参数 |
| llm_invalid_output | 模型返回内容无法解析，可重试或降低输出长度 |
| llm_output_truncated | 模型输出被截断，可提高 max_tokens 或缩短输入 |

`truncateError` 必要性：`comfy_unreachable` 的 `error_msg` 里带完整实例 URL，
不截断会把卡片撑高。

### 6.3 表格视图未改（遗留）

`isEnvError` 语义问题在表格视图仍然存在。要不要把判定从「能不能再试」
改成「是不是环境问题」（例如按 `error_code` 白名单），属于产品决策，本轮不动。

## 7. 后端缺陷：SSE 长连接耗尽连接池

这是本轮排查中最有价值的发现，与前端改动无关，但被截图测试量放大。

### 7.1 现象

跑完 60 张截图后，**所有** `/api/*` 全部超时：

```
projects   TIMEOUT
assets     TIMEOUT
jobs       TIMEOUT
jobs/stats TIMEOUT
```

但进程状态正常：`%CPU 2.5`、系统 `load 0.12`、独立进程读 DB `0.00s` 成功。
说明它没在算，而是**卡在等待**。

### 7.2 定位

```
uvicorn :8000   34 个 LISTEN 积压
45 个线程       全部 S 状态（sleep/阻塞）
```

`py-spy dump` 直接指到根因：

```
Thread (idle): "MainThread"
    Condition.wait (threading.py:359)
    Queue.get (sqlalchemy/util/queue.py:199)
    QueuePool._do_get (sqlalchemy/pool/impl.py:155)
    ...
    Query.all (sqlalchemy/orm/query.py:2768)
    event_stream.<locals>.gen (app/api/v1/events.py:54)   ← 这里
    StreamingResponse.stream_response
```

### 7.3 根因

`app/api/v1/events.py:42-67`：

```python
while elapsed < _MAX_STREAM_SECONDS:
    db = SessionLocal()
    try:
        rows = db.query(JobEvent)...all()
        for ev in rows:
            yield f"id: ..."        # ← yield 在 try 内
    finally:
        db.close()                   # ← 要等生成器再次推进才执行
    await asyncio.sleep(_POLL_INTERVAL)
```

`yield` 写在 `try` 里，`finally: db.close()` 必须等生成器**下一次推进**才执行。
每一条 SSE 连接在 yield 期间**持续占住一个 DB 连接**。

SQLAlchemy 默认池：`pool_size=5 + max_overflow=10` = **15 个连接**。
SSE 单连接最长 `_MAX_STREAM_SECONDS = 300` 秒。

→ 打开 15 个任务页/详情页（每个都建 SSE），连接池耗尽，之后所有请求在 `QueuePool._do_get` 排队，全站卡死。

### 7.4 对测试的影响与规避

60 张截图 = 60 次页面加载，每次一条 SSE。必然复现。

规避（`uiux-shots.mjs`）：`page.route('**/api/events*', route => route.abort())`。
截图不需要实时推送。加拦截后 60 张跑完，后端仍 `200 / 0.012s` 健康。

### 7.5 建议的正式修法（未实施，属后端改动）

把 DB 查询移出生成器持有期 —— 查完立即 `db.close()`，再 `yield`：

```python
while elapsed < _MAX_STREAM_SECONDS:
    with SessionLocal() as db:
        rows = db.query(JobEvent)...all()
        payloads = [_event_payload(ev, db.get(Job, ev.job_id)) for ev in rows]
    # 会话已关闭
    for p in payloads:
        yield f"id: {p['seq']}\ndata: {json.dumps(p)}\n\n"
```

或改用 `poolclass=NullPool` 给 SSE 单独引擎。
本轮只做前端，未改后端。

## 8. 最终验证

```
vue-tsc -b            0 error
npm run build         ✓ 9.90s
check:colors:strict   hex 0 / rgba 0
check:breakpoints     全部标准档（1100 / 768 / 480）
check:contrast        全部 ≥ 4.5:1
五档截图 60 张        FAIL 0 张
后端健康              /api/jobs 200 / 0.012s
```

功能验证（`verify-jobs-mobile.mjs`，390px）：

```
hasTable: false          表格已隐藏
hasCards: true           卡片视图生效
cardCount: 20            20 张卡
hasPager: true           分页器存在
expandResult: 展开成功，显示 llm_invalid_output 原文
scrollResult: 313/313    筛选轴滚到末尾
```

FAB 遮挡验证（`_chk-fab3.mjs`）：滚到底后 20 张卡的所有操作按钮，**零个被 FAB 覆盖**。
`padding-bottom: 62px` 生效（与 `ScriptReader.vue:87` 同一处理）。

## 9. 遗留

| 项 | 状态 | 说明 |
|---|---|---|
| SSE 连接池耗尽 | ⚠️ 后端缺陷，未修 | 见 §7.5，建议单独处理 |
| `isEnvError` 语义错误 | ⚠️ 仅卡片视图绕开 | 表格视图仍把配置错误显示为「可重试」 |
| 视觉 AI 误判「筛选未压缩」 | — | 实测 `filterRows=2` 正确：两条独立轴各一行，行内横向滚动 |
| 第三张卡底部被视口截断 | — | 正常滚动，非缺陷 |
| 截图脚本 `networkidle` 超时 | ✅ 已修 | settings/jobs 有常驻轮询，`networkidle` 永远等不到，改 `domcontentloaded` |
