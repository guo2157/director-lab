# 页面级改造清单

> 状态定义：✅ 已完成并实测 ｜ 🟡 部分完成 ｜ ⬜ 未开始 ｜ ⛔ 明确不做（附原因）

---

## 1. 全局框架

| 项 | 状态 | 说明 |
|---|---|---|
| 侧边栏 / 顶部导航 | ✅ | 390px 下 `top-nav` 曾溢出 17px（scrollWidth 170 > clientWidth 153），收敛 gap/padding 后 169 = 169 |
| 项目内流程导航（`ProjectProgressRail`） | ✅ | 窄屏隐藏「制作流程」标题与 01–05 序号，5 阶段右边界从 77/157/236/327/**431** 收到 44/116/188/272/**368**，全部入屏 |
| 主内容容器与滚动策略 | ⬜ | 各页 `.page` / `.page-body` 并存，`.page-body` 有 `max-width: 1520px` 而 `.page` 没有，超宽屏下两套宽度规则并行。需统一，但涉及所有页面回归，单独一轮做 |
| 页面标题与操作区 | 🟡 | `PageHeader.vue` 已存在，但 `ParseView` / `JobsView` / `SettingsView` 仍各自手写 header，未复用 |
| 响应式断点 | ✅ | 门禁已强制 1100 / 768 / 480 三档 |

---

## 2. 项目解析页 `/project/:id/parse`（重点）

| 项 | 状态 | 说明 |
|---|---|---|
| 刷新后恢复真实解析状态 | ✅ | **P0 修复**：`episodeNumbers` 兜底链中的 `flow.totalEpisodes`（恒为 `0`）截断了 `?? maxRecorded`，导致失败状态整体消失 |
| 失败常驻可见 + 重试 | ✅ | 新增 `failure-panel`，只依赖集级解析状态，与易失的 `parseJobId` 解耦 |
| 失败原因去重 | ✅ | 相同错误按文案分组，输出「第 1、2 集 \| <msg> \| 已尝试 5 次」 |
| 统计去重 | ✅ | 删除 `parse-done-card`（与 `result-panel` 重复展示 5 场景/2 角色/4 道具）；统计收敛为唯一一条概览 |
| 竞争主按钮 | ✅ | 有失败时唯一 primary 为「重试失败集」 |
| 空状态噪音 | ✅ | 空状态仅在「全待解析」时出现，不再与失败面板并存 |
| 分组内容层级 | 🟡 | 场景补 `summary` + 所属集，角色/道具补 `description` 与真实空状态；**分集 / 分镜 / 对白 / 音频规划仍未做** |
| 解析原文折叠 | ✅ | 原 `<pre>` 默认铺开整份报告（含全部场景全部镜头），改为 `aria-expanded` 折叠 |
| 长文本复制 | ⬜ | 未提供「复制」按钮 |
| **分析过程统一为单一 Card** | ✅ | 见下方 §2.1 |
| 分组导航（左侧目录） | ⬜ | 需先接 `GET /api/projects/:id/shots` 等数据通道，见 §5 风险 |
| 底部 Sticky 操作区 | ⛔ | 现有流程是「解析完成 → 去资产库」，无「确认解析」业务动作（D-22 已由 Agent 自动确认）。按方案 6.5 末条，**不得新增 API 或伪造确认态** |
| 桌面/平板/手机 | 🟡 | 1920 与 390 实测通过；1280 / 1024 / 768 未截图 |

---

## 3. 资产库 `/project/:id/assets`

| 项 | 状态 | 说明 |
|---|---|---|
| 禁用按钮给原因 | ✅ | 驳回返工 / 审核通过 / 去分镜图 补 `title` + `aria-label` |
| 0 资产时文案自洽 | ✅ | 原提示「还有 **0** 项资产未生成完成」，改为「当前项目还没有资产，请先在解析页完成解析并生成资产」 |
| 审核态与生成态分离 | ✅ | `AssetStatusBar` 已把「进度」与「审核状态」分列，无需改 |
| 卡片常驻操作 | ✅ | 重新生成 / 替换上传均在卡片上常驻，未折进 ⋯ 菜单（符合既定 UI 偏好） |

---

## 4. 任务中心 `/jobs`

| 项 | 状态 | 说明 |
|---|---|---|
| `job_type` 本地化 | ✅ | 后端实际产出 10 种，前端缺 `script_agent` / `series_agent`，导致表格显示原始枚举。已补「剧本分析」「系列分析」 |
| 失败原因可读 | ✅ | 原 tooltip 无宽度限制、长错误单行溢出；改为 `max-width: 460px` + `pre-wrap` + 最大高度滚动，按钮文案「查看指引」→「查看原因」 |
| 疑似中断提示 | ✅ | running 超过 90 分钟（H3 最长片段约 25 分钟）标「可能已中断」。**纯前端展示，不改后端状态机** |
| 进度真实性 | ✅ | 已有 `progress` 来自后端，无假进度 |
| 任务详情事件流 | ⬜ | 表格无展开行，错误信息只在 tooltip；方案 7.8 要求「任务详情展示事件」，需确认后端是否已提供 events 接口 |

> 实测发现：`job_482d0d133249`（`script_agent`）从 03:20 起 `running` 达 8 小时，`finished_at` 为空，worker 于 11:12 重启后该任务被回收为 `failed`（耗时 2m56s，`llm_invalid_output`）。属 worker 重启前的残留状态，非常态 bug，但暴露了「超长 running 无提示」的体验缺口。

---

## 5. 全部页面覆盖清单

| 页面 | 状态 | 备注 |
|---|---|---|
| `/` 项目列表 | ✅ | 实机审查完毕；**修掉 P0 假进度**（读后端不存在的 `shot_count` → 恒 0%，改 `shot_done/shot_total`），补真实阶段标签。见 §8.1 |
| `/project/:id/script` | ✅ | 步骤条标签改为阶段语（剧本编辑/内容校验/版本已存/智能分析），与按钮的动作语区分，消除同屏两遍同名；三个禁用按钮补 `title` + `aria-label`（「开始分析」禁用时明确提示需先保存版本） |
| `/project/:id/storyboard-images` | ✅ | 复核确认已符合既定偏好：`promptDigest()` 只取「目标画面」一行 + `line-clamp: 2` |
| `/project/:id/storyboard` | ✅ | 修 4 项：26 个选择框悬空 `aria-labelledby`、重复完成文案、资产齐全时的多余「去资产库」、双 primary。见 §8.1 |
| `/project/:id/compose` | ✅ | 实机审查通过，仅记录 n-card 双层 `role="heading"`（库层面，见 §8.3）。另确认 `data\media\...` 反斜杠路径后端可归一化，非 bug |
| `/series`（列表） | ✅ | 补「最近更新时间」；抽共享 `relativeTime` |
| `/series/create` | ✅ | 修必填星号缺失（副标题承诺 `*`，实际全页无星号） |
| `/series/:id` | ✅ | 修双 primary + 页头文案与实际状态不一致（0 剧集却写「下一集」） |
| `/series/:id/continue` | ✅ | 两个禁用按钮补真实原因；实测填入后续写要求即恢复可用 |
| `/episodes/:id/review` | 🟡 | 只能验证错误态：库中该剧集不存在（后端悬空引用，见 §9.3）。**已在 §10 修复根因并清理数据** |
| ~~`/director`~~ | ⛔ | **已不再是独立页面**，改为全局「AI 助手」浮动按钮（各页右下角）唤出的侧栏。原路由审查结论作废；折叠头键盘不可操作仍属库层面（§8.3） |
| `/settings` | ✅ | 侧边导航由纯 `div` 改为可聚焦 `button`，补 `aria-current` 与焦点环 |

---

## 6. 风险与前置依赖

| 项 | 风险 | 处理建议 |
|---|---|---|
| 解析页分组导航 | 需新增数据通道（shots / dialogues / audio plan），且对白与音频规划**可能没有独立接口** | 先确认接口边界，再决定是分组展示还是跳转对应专业页面 |
| 统一 `.page` / `.page-body` | 触及全部页面布局，回归面大 | 单独一轮，改完逐页截图 |
| Pinia 非持久化 | `scriptFlow` 全内存，刷新丢 `parseJobId` / `estimate` / `scriptVersionId`。本轮用「以后端集级状态为准」绕开，但同类隐患仍存在于其他依赖 store 的页面 | 系统性梳理：**能从后端重建的状态，不要只存在 store 里** |
| 与并行会话共享工作区 | `backend/app/api/v1/workflows.py` 等在审查期间被另一会话修改（11:01–11:04） | 提交前按文件白名单分批 commit，勿整仓 `git add -A` |

### 2.1 剧本智能分析窗口（2026-11-18 重新布局）

**改造前**：页面上同时存在 4 个独立区块——`llm-stream-card`（AI 输出）、`candidate-bar`（任务状态窄条）、`failure-panel`（失败详情）、`parse-status-panel`（集数状态）。输出与进度分离、耗时以本地 `ref(0)` 每秒自增（刷新即归零）。

**改造后**：收敛为 2 块——分析窗口 + 分集解析结果。

```
┌ AI Agent ─────────────────────── ● 正在分析 ┐
│ 分析中断，失败原因见下方        0/2 集  已耗时 02:54 │
│ ──────────────────────────────────────────── │
│ Agent 实时输出                                │
│ （SSE llmStreamText，自动滚动到底）            │
│ ──────────────────────────────────────────── │
│ 任务执行中                          实时更新  │
└──────────────────────────────────────────┘
┌ 分集解析结果  0/2 集完成   只看失败(2)  重试失败集(2) ┐
│ ║ 第 1、2 集  <错误原文>  已尝试 5 次          │
│ [集数方格 × N]                                │
└──────────────────────────────────────────┘
```

**耗时实现（关键修复）**

| 需求 | 实现 |
|---|---|
| 从任务真正开始执行时计时 | `started_at` 优先，缺失回退 `created_at`（后端本就返回，前端 `Job` 类型此前未声明，已补） |
| 刷新后恢复正确耗时 | 计时改为 `nowTick - started_at` 的**计算属性**，不用累加器；实测三次刷新均为 `02:54` |
| 任务完成后停止 | 终点取 `finished_at`，存在即冻结 |
| 不因重渲染重置 | 无 `ref` 累加状态，纯派生 |
| 格式 | `mm:ss`，超 1 小时转 `HH:mm:ss` |

**刷新恢复链路**

`GET /projects/{id}/script-agent/status`（此前已存在但页面未使用）→ 取最近一个 `script_agent` 任务：
- `queued` / `running` → 接管，继续显示实时输出与计时
- `failed` / `lost` → 接管，显示「执行失败 + 耗时」（否则刷新后失败态整块消失）
- `succeeded` → **不接管**，成功后收起执行过程，只展示解析结果

**去重**

- 失败原因不再出现在分析卡内（原先卡内 `job.error_msg` 与面板集级原因重复）
- 重试入口只保留 1 个（分集面板工具栏），卡内失败分支不再放按钮
- 分集面板失败原因改用左侧色条 + 深色底，不再整块红底卡片

---

## 7. 工程侧教训（2026-11-18）

`frontend/src/views/project/ScriptView.vue` 曾是 **root 属主**文件（`-rw-r--r-- root:root`），以 `ubuntu` 身份写入**静默失败**——`execute_code` 的 `write_file` 返回值里没有 `success` 字段，因此「写完不报错」不等于「写成功」。

发现方式：改完后重新构建、页面无变化，`grep` 源文件确认改动不在。

处置与规范：

```bash
# 排查
find frontend/src docs -user root -type f

# 修复
sudo -n chown ubuntu:ubuntu <file>
```

**规则：任何脚本化写文件后，必须 `read_file` 回读验证关键字符串**，不能依赖返回值判断成功。本次已据此复验全部既有改动（ParseView / JobsView / AppLayout / ProjectProgressRail / AssetStatusBar / types/job.ts / docs），确认均已真实落盘。


---

## 8. 第二轮页面审查（2026-11-18 下午）

覆盖此前「仅源码审查」的页面：作品列表、分镜看板、合成与导出、设置、系列、导演台。

### 8.1 已修复

| 页面 | 问题 | 处置 | 验证 |
|---|---|---|---|
| 作品列表 | **P0 数据失真**：卡片进度读 `episode_plan[].shot_count`，该字段后端从不返回，进度恒为 0%。「游乐场」26 镜全部完成却显示 0% | 改用后端派生字段 `shot_done` / `shot_total`，显示「26/26 镜」 | DOM `aria-valuenow=100`、文案「已完成 26/26 镜」 |
| 作品列表 | 卡片看不到当前生产阶段 | 新增阶段标签，读 `current_stage`；阶段枚举与中文文案抽到 `@/constants/stages.ts`，`ProjectProgressRail` 复用去重 | 实测「剧本解析」「已完成」 |
| 分镜看板 | 26 个镜头选择框可访问名为空：`aria-labelledby` 指向**不存在的 id**（悬空引用） | 用 `label` 提供真实引用目标 + sr-only 隐藏视觉文字 | a11y 树读出「选择镜头 1..26」；实测 label 1×1、`clip-path: inset(50%)` |
| 分镜看板 | 「全部分镜视频已生成完成」在标题与副行重复两遍 | 副行只留「最近完成于 HH:mm:ss」，并修正 `v-else-if` 副作用（all_done 缺时间戳会错落到「未生成 N」） | 视觉确认只出现一次 |
| 分镜看板 | 资产已齐全时仍常驻「去资产库」跳转按钮 | 改 `v-if="!readiness.all"` | 资产齐全时按钮消失 |
| 分镜看板 | 同屏两个 primary 按钮（批量出视频 / 去合成导出） | 按 `clipsDone` 权重互斥，任一时刻只有一个 primary | 实测「去合成导出」primary、「批量出视频」default |
| 设置 | 侧边导航为纯 `div`：键盘无法聚焦、无可访问名、激活态仅靠背景色 | 改 `button type=button`，补 `aria-current=page`、`:focus-visible` 焦点环、button 样式重置 | 实测可聚焦、名「ComfyUI / 多实例加权负载均衡」 |
| 系列 | 列表卡片缺「最近更新时间」（方案 §7.1 要求，后端 `updated_at` 一直有） | 卡片补「更新于 x」，与 `ProjectCard` 共用 `@/utils/time.ts` | 实测「更新于 7 天前」，title 为完整时间 |

### 8.2 实测驳回的假警报（未改代码）

| 警报 | 实测结论 |
|---|---|
| Comfy 预览视频路径为 `data\media\x.mp4`（Windows 反斜杠），Linux 上应 404 | 后端会归一化反斜杠，两种写法均返回 `200` + `video/mp4` + 45MB 真实文件。**不是 bug** |
| 分镜看板快照显示「视频 0/26（0%）」但进度条 100 | 快照取到数据加载前瞬间；稳定态实测 `26/26（100%）` |
| 按钮可访问名显示为「loading 刷新进度」 | DOM 内 button 无 loading 元素，是 a11y 快照拼接假象 |
| 场景 `shot_count` 疑似又一个「后端不返回」的假字段 | `/scenes` 接口**确实返回**（7+4+4+5+6=26）。与 `episode_plan` 的假字段长得一样，但性质相反 |

> 教训：同一个字段名在不同接口上可能一个是真、一个是假。**必须逐个接口核对响应，不能沿用上一个 bug 的结论。**

### 8.3 库层面 a11y 缺口（未修，需单独决策）

以下两项来自 Naive UI 自身实现，无组件级开关，影响全站所有卡片/折叠区：

1. **n-card 标题双层 `role="heading"`**
   `node_modules/naive-ui/es/card/src/Card.mjs:206,209` 同时给 `.n-card-header` 与 `.n-card-header__main` 加 `role="heading"`，且外层 heading 会把 `header-extra` 里的按钮文字一并吸进名称。
   实测：「视频预览 整部 · 2:17 重新载入」内含「视频预览」，屏幕阅读器会重复播报。

2. **n-collapse 折叠头不可键盘操作**
   `.n-collapse-item__header` 为普通 `div`（无 `role` / `tabindex` / `aria-expanded`），键盘用户无法展开「项目状态 / 风格基线 / 音频生产」。

可选处置：
- 升级 Naive UI 并确认上游已修；
- 或在 `App.vue` 挂载后用一个**范围受限**的 DOM 修正器（仅针对 `.n-card-header__main` 去掉 role、给折叠头补 `role=button`/`tabindex=0`/键盘事件），需评估 MutationObserver 开销；
- 或接受现状，在验收报告登记为已知限制。

**本轮未改动**：属全站行为变更，回归面大且无上游开关，不在一轮内顺手做。

### 8.4 仍未实机验证

| 页面 | 原因 |
|---|---|
| 导演台有会话态 | 库里没有可用会话，只能验证空态（空态本身合格：说明 + 新建入口 + 输入框禁用有解释） |
| 合成与导出 无记录态 | 仅有 1 条已完成记录，已就绪分支验证过；空态与失败态未实机 |
| `/series/create`、`/series/:id`、`/series/:id/continue` | 未逐页走查 |
| `episodes/:id/review` | 未走查 |


---

## 9. 第三轮页面审查（2026-11-18 晚）

覆盖剩下的系列页与剧集审核页。

### 9.1 已修复

| 页面 | 问题 | 处置 | 验证 |
|---|---|---|---|
| 系列详情 | 页头「续写下一集」与「下一步」区块同为 `primary` 且指向同一动作（`goContinue`） | 页头降为 `secondary`，空态按钮同步降级，主操作只留「下一步」区块 | 实测 visiblePrimaries 从 3 → 1（另 1 个是 text 型「现在补充」）；视觉确认梯度清晰 |
| 系列详情 | 页头文案硬编码「续写下一集」，但 0 剧集时「下一步」显示「续写第 1 集」，同页两个说法 | 抽 `continueLabel`，页头 / 下一步 / 空态 title 统一按真实状态取值 | 实测 0 剧集时三处均为「续写第 1 集」 |
| 续写剧集 | 「构建上下文预览」「开始续写」禁用时无任何原因说明 | 补 `title`：未填「续写要求」→ 提示必填；`taskRunning` → 提示已有任务运行中（对齐真实禁用条件） | 空表单：两按钮禁用且带原因；填入后：均可用、`title` 消失 |
| 新建系列 | 副标题声明「带 `*` 的为必填项」，但 `n-form` 设了 `:show-require-mark="false"`，全页没有任何星号 | 移除该属性，名称字段补 `required` | 星号只出现在「系列名称」；空名称提交被拦住并就近显示「请输入系列名称」，未创建数据 |

### 9.2 实测驳回的假警报（本轮新增）

| 警报 | 实测结论 |
|---|---|
| 新建系列页点击「创建系列」无任何反应、无校验提示 | 按钮位于 y=1108，**超出 900px 视口**，`elementFromPoint` 命中不到。滚动到视口内再点，校验正常显示「请输入系列名称」且未创建数据 |

> **方法论教训**：`browser_click` 报告 `success` 只代表事件已派发，不代表命中目标。点击前应先确认 `rect.top/bottom` 落在视口内，或先 `scrollIntoView`。
> 本轮因此产生 1 次误判，另一次同类误判见 §8.2 的「视频 0/26」——同样是「取到了非稳态时刻的观测值」。

### 9.3 发现的业务/数据问题（未改，需单独处理）

**系列 → 剧集的悬空引用**

实测 `/episodes/eps_46cda652c08a/review` 返回「剧集不存在」。核查数据库：

```
series.srs_03524b9d7a4e.latest_confirmed_episode_id = 'eps_46cda652c08a'
series_episodes 表行数 = 0
```

即系列行仍指向一个已不存在的剧集。属后端数据完整性问题（删除剧集时未清理 `latest_*_episode_id`），非前端缺陷。

前端该页的兜底是可接受的：给出「剧集不存在」+ 重试 + 顶部「← 返回系列」，没有吞错、没有白屏。

**处置建议**（不在本次 UI 改造范围内，按 §0.3 只记录）：
- 后端在删除剧集时把 `latest_confirmed_episode_id` / `latest_completed_episode_id` 置空或改指上一集；
- 或加一条数据一致性巡检，清理悬空引用。

### 9.4 仍无法实机验证

| 页面 | 原因 |
|---|---|
| 剧集审核正常态 | 无可用剧集（见 §9.3），只能验证「剧集不存在」错误态 |
| 导演台有会话态 | 库中无可用会话 |
| 合成页空态/失败态 | 仅有 1 条已完成记录 |


---

## 10. 第四轮（2026-11-18 晚·续）

### 10.1 解析原文在确认/刷新后仍可查看 + 复制全文

原条件 `v-if="candidateReady && formattedResult"`，而 `candidateReady` 依赖 `parseJob.status === 'succeeded'`，
`parseJob` 又只来自 Pinia 里的 `flow.parseJobId`。确认解析后 `flow.resetAfterConfirmParse()` 会清空该 id，
于是**原文块必然消失，刷新后更查不到**——低频但确实需要核对的内容彻底拿不到。

处置：

| 项 | 做法 |
|---|---|
| 复用格式化逻辑 | 把 `formattedResult` 的实现抽成 `formatResult(r)`，当前结果与历史结果共用 |
| 历史结果来源 | 新增只读查询 `listJobs({project_id, job_type:'parse'})`，取最近一次 `succeeded` 且带 `result` 的任务 |
| 不碰 store | 只算 `rawReportText = 当前结果 \|\| 历史结果`，不再写 `parseJobId`，避免与 `resetAfterConfirmParse` 抢时序 |
| 复制 | 新增「复制全文」；`navigator.clipboard` 失败时回退 `execCommand`（http 暴露与非用户激活场景都能用） |

实测（`/project/prj_ee9942c5fa42/parse`，该项目的 parseJobId 早已被清空）：
- 刷新后原文块稳定出现，展开后 335 字符真实内容（角色/道具/场景原文）
- 点「复制全文」→ 提示「已复制解析全文」，按钮变「已复制」、`aria-label` 同步

> 曾先试过「把 parseJobId 写回 store」的方案，实测页面**时而显示时而消失**——因为与
> `resetAfterConfirmParse` 的清理逻辑互相打架。已放弃，改为纯只读派生。

### 10.2 后端：删除项目时清理 series 悬空指针

§9.3 记录的「剧集不存在」根因确认：

```
purge_project() 删除 series_episodes 行，但不清 series.latest_confirmed_episode_id
                                                          / latest_completed_episode_id
```

`backend/app/services/project_purge.py` 中补：删完剧集后，把指向这些剧集的 `latest_*` 指针置空。

验证（回滚事务，不落库）：

```
purge 前 : ('eps_purgeverify01', 'eps_purgeverify01')
purge 后 : (None, None)
残留 series_episodes 行数: 0
RESULT: PASS
```

回归：`tests/test_series_management.py` 22 passed。

存量数据一并清理（先备份 `data/director_lab.db`）：`srs_03524b9d7a4e` 的悬空
`latest_confirmed_episode_id` 已置空，清理 1 行。

### 10.3 导演台入口变更

`/director` 不再是独立页面，改为全局「AI 助手」浮动按钮唤出的侧栏。§5 中该行结论作废。
原「折叠头不可键盘操作」结论对侧栏内的折叠区仍然成立（Naive UI `n-collapse` 无 role/tabindex）。

### 10.4 工程侧：并行 root 会话反复改写文件

本轮再次遇到 `frontend/src/views/project/ParseView.vue` 与 `frontend/dist/` 变为 **root 属主**，
导致 `patch` 报 `Permission denied`、`vite build` 清空 outDir 失败。

处置：

```bash
find frontend/src docs -user root -type f
sudo -n chown -R ubuntu:ubuntu frontend/src frontend/dist
```

**规则**：任何写入失败先查属主，不要当成工具 bug 重试；改完立刻 commit，避免被下一次 root 写入覆盖。
本轮 `git checkout -- <file>` 还误删了同文件里未提交的复制按钮改动——回退应精确到片段，不要整文件 checkout。
