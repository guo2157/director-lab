# Director-Lab 前端现状审查报告（Phase 0）

> 审查时间：2026-11-18
> 仓库：`/home/ubuntu/director-lab`
> 分支：`release`（与 `origin/main` 同 SHA）
> Commit：`bb9799d4835d35ab59a33497998919a3ede64d59`
> 工作区状态：干净（`git status --porcelain` 无输出）

---

## 0. 审查方法与证据等级

本报告区分三类证据，**不把推测写成结论**：

| 等级 | 含义 | 标记 |
|---|---|---|
| 实证 | 浏览器实际加载页面、抓取 DOM/布局指标、核对接口响应 | 【实证】 |
| 源码 | 阅读组件/样式/API 源码得出，未在页面复现 | 【源码】 |
| 待验 | 疑似问题，需进一步验证 | 【待验】 |

桌面视口 1920×1080，移动视口 390×844（CDP `Emulation.setDeviceMetricsOverride`）。

**已实机走查的路由（6 条）**：`/`、`/project/:id/script`、`/project/:id/parse`（两个项目、成功态与失败态）、`/project/:id/assets`、`/project/:id/storyboard-images`、`/jobs`。

**仅源码审查（8 条）**：`/project/:id/storyboard`、`/project/:id/compose`、`/series/*`（4 条）、`/settings`、`/director`。这些页面本次**未做视觉验收**，其问题清单不作为改造依据。

---

## 1. 技术基线

| 项 | 实际情况 |
|---|---|
| 框架 | Vue 3.5 + TypeScript 5.6 + Vite 6 |
| UI 库 | Naive UI 2.40（`n-config-provider` + `themeOverrides`） |
| 状态 | Pinia 2.3（`stores/scriptFlow.ts` 等，**非持久化**，刷新即丢） |
| 数据 | TanStack Query 5.62，Query key 形如 `['parse-status', projectId]` |
| 图标 | `@vicons/ionicons5`（已统一，无混用） |
| 路由 | 14 条，全部 `() => import()` 路由级懒加载 |
| 测试 | 无前端单测；后端 pytest（上游文档记录 699 collected / 690 passed / 9 failed） |
| 设计门禁 | `check:colors:strict`（hex 必须 0）、`check:breakpoints:strict`、`check:contrast:strict` |
| 构建 | `npm run build` = `vue-tsc -b && vite build` |

### 1.1 路由与页面清单

| 路由 | 视图组件 | 职责 |
|---|---|---|
| `/` | `views/ProjectListView.vue` | 项目列表 |
| `/project/:id/script` | `views/project/ScriptView.vue` | 剧本编辑 / 校验 / 分析入口 |
| `/project/:id/parse` | `views/project/ParseView.vue` | 剧本解析结果检查 |
| `/project/:id/assets` | `views/project/AssetsView.vue` | 角色 / 场景 / 道具 / 音色资产 |
| `/project/:id/storyboard-images` | `views/project/StoryboardImagesView.vue` | 分镜图生成与重生成 |
| `/project/:id/storyboard` | `views/project/StoryboardView.vue` | 分镜看板（镜头编排） |
| `/project/:id/compose` | `views/project/ComposeView.vue` | 合成与导出 |
| `/series` | `views/series/SeriesListView.vue` | 系列列表 |
| `/series/create` | `views/series/SeriesCreateView.vue` | 新建系列 |
| `/series/:id` | `views/series/SeriesDetailView.vue` | 系列详情 |
| `/series/:id/continue` | `views/series/SeriesContinueView.vue` | 续写剧集 |
| `/episodes/:id/review` | `views/series/EpisodeReviewView.vue` | 剧集审核 |
| `/jobs` | `views/JobsView.vue` | 任务中心 |
| `/settings` | `views/SettingsView.vue` | 设置 |
| `/director` | `views/DirectorConsoleView.vue` | 导演台（Agent 会话） |

### 1.2 公共组件盘点（复用优先）

界面基础件（`components/ui/`）：`PageHeader`、`StatusTag`、`EmptyState`、`ErrorRetry`、`SkeletonList`、`ConfirmDialog`、`GlobalConfirmDialog`、`PromptEditor`。

业务件已按域分组：`assets/`、`script/`、`storyboard/`、`storyboard-images/`、`director/`、`projects/`。

**结论**：公共件覆盖度足够，本方案不需要新建组件库，只需在既有件上收敛用法。

---

## 2. 设计令牌现状（关键决策点）

`frontend/src/styles/global.css` 已是一套**成熟的设计令牌 v2**：

- 深色「暗房墨青」背景层级（`--bg-base/#0f1618` → `--bg-elevated`）
- 品牌色青瓷 `--brand-primary: #35b3a6`
- **语义色派生层**：每个状态色都配了 `-soft` / `-line` 低透明度变体，页面禁止手写 `rgba()`
- 圆角刻意收紧：`--radius-sm: 2px` / `--radius: 3px` / `--radius-lg: 4px`
- 阴影全部为 `none`，**层级由边框表达**
- 间距刻度 `--sp-1..--sp-16`（4/8/12/16/20/24/32/40/48/64）
- 焦点环 `--focus-ring`、`::selection`、`prefers-reduced-motion` 全局兜底
- `--danger-text` 单独拆分：解决小号红字在深底上的对比度

`App.vue` 通过 `src/styles/tokens.ts` **运行时读取 CSS 变量**再喂给 Naive UI `themeOverrides`（而不是把 `var()` 写进 themeOverrides）。

### 2.1 决策：不替换主题

方案 3.1 给出的浅色蓝色基准（`#4f6bed` / 浅灰底 / 6–14px 圆角 / 弹窗阴影）与本项目现状**方向相反**。按方案 2.1「最终以项目现有品牌和主题兼容性为准」与 3.6「保留现有主题行为」，本次**保留深色青瓷体系**。

理由：
1. 该体系已有三道自动化门禁（颜色 / 断点 / 对比度），推翻等于让门禁失效。
2. 深色体系是为「看分镜图、看视频成片」的工作台场景选的，浅底会干扰画面判断。
3. 方案的核心诉求是信息架构与状态完整性，不是换皮；换皮会带来大面积回归风险，违反 0.3「不得为了统一组件而强行替换已稳定组件」。

**改造落到**：把散落的硬编码数值收敛回令牌；把重复信息、竞争性操作、丢失状态修掉。

---

## 3. 问题台账

### P0（业务状态展示错误 / 数据误导）

#### P0-1 解析失败状态在刷新后整体消失【实证】

- 页面：`/project/:id/parse`
- 现象：项目 `prj_de813f07bc04` 后端 `GET /api/projects/:id/parse/status` 返回 2 条 `status: "failed"`（`attempt_count: 5`，错误 `第 1-2 集: 语言模型输出非合法 JSON`）。但前端刷新后整页只剩一句 `Agent 将自动完成评估与解析，请稍候`，集数面板、失败原因、重试入口**全部不存在**。
- 根因：`ParseView.vue` 的集数兜底链

  ```ts
  const total =
    flow.estimate?.estimated_episodes ??
    parseStatus.value?.total_episodes ??
    flow.totalEpisodes ??      // ← 恒为 0，截断兜底
    maxRecorded                // ← 永不生效
  ```

  `flow.totalEpisodes` 定义为 `estimate.value?.estimated_episodes ?? 0`。Pinia **非持久化**，刷新后 `estimate` 为 null → `flow.totalEpisodes === 0`；`??` 只对 `null/undefined` 生效，`0` 会直接通过，于是 `maxRecorded`（= 2）永远取不到。
- 影响：用户看到「请稍候」，实际任务早已失败且不会自行推进。违反方案 0.3「不得让页面刷新后丢失任务状态」。
- 处置：**已修复**（移除该兜底项并加防回归注释）。

#### P0-2 失败原因在页面上不可见（即使不刷新）【实证】

- 现象：`candidate-bar` 的失败分支依赖 `flow.parseJobId`，而 `parseJobId` 同样是非持久化 store 字段。任何一次刷新或从任务中心跳回，失败原因即丢失。
- 处置：**已修复**——新增常驻 `failure-panel`，其可见性只依赖集级解析状态（`failedEpisodes.length && !parseJobRunning`），与 jobId 解耦。

### P1（信息架构 / 重复信息 / 核心操作难找）

#### P1-1 完成态统计重复展示两次【实证】

修复前 `/project/prj_257406918e63/parse` 的 DOM 实测：

```
parse-done-card   : "解析完成 | 1 集 | 5 场景 | 2 角色 | 4 道具 | 去资产库 →"
result-panel      : "解析结果 | 5 场景 · 26 镜 · 2 角色 · 4 道具 | 去分镜图 →"
```

同一组数字（5 场景 / 2 角色 / 4 道具）出现两次，并挂两个互相竞争的前进入口。违反方案 2.2「同一信息不得在标题、Banner、Card 和列表中重复展示」。
- 处置：**已修复**——删除 `parse-done-card`，统计收敛为结果区唯一一条紧凑统计条；状态改为结果区头部单个状态标签。

#### P1-2 同一「重试」动作出现 4 处【实证】

顶部工具栏「重试失败集」按钮 + 每个失败集格右上角悬浮图标 + 「只看失败」展开后的每集「重试」+ 错误卡片内「重试」。
- 处置：**已修复**——收敛为失败面板内唯一入口；删除集格悬浮按钮（20px，低于 40px 触控基线）与重复的 `failed-error-list`。

#### P1-3 失败原因逐集刷屏【实证】

2 个失败集的错误文案完全相同（同一个 LLM 输出解析错误），却渲染 2 行。
- 处置：**已修复**——按错误文案分组，输出「第 1、2 集 | <msg> | 已尝试 5 次」。

#### P1-4 空状态与真实状态并存【实证】

修复前，失败项目页面上同时存在失败面板、集数面板**和**一句 `请点击『解析选中』提交任务` 的 `NEmpty`。空状态在「有数据（失败记录）」时出现，属噪音且误导。
- 处置：**已修复**——空状态仅在 `statusSummary.pending === statusTotalEpisodes`（全待解析）时出现。

#### P1-5 解析结果无分组层级【实证】

`result-panel` 只有三列：场景（标题 + 镜数）、角色（仅名字 chips）、道具（仅名字 chips）。方案 6.3E 要求的角色简介、道具描述、场景所属集、分镜/对白/音频分组均缺失。
- 处置：**部分修复**——场景补 `summary` + 所属集，角色/道具补 `description`；「未解析出角色」等真实空状态替代裸 `无`。
- **未完成**：分集 / 分镜 / 对白 / 音频规划分组。原因见 §5 遗留项 A。

#### P1-6 三个同级高权重按钮竞争【实证】

解析页头部同时出现「返回剧本」「继续分析（N 集未完成）」「解析选中（N 集）」，其中两个为 `type="primary"`。方案 4.1 要求「同一页面避免多个视觉权重相同的主按钮」。
- 处置：**已修复**——有失败集时，唯一 primary 是「重试失败集」；「继续分析」「解析选中」降为次级。

#### P1-7 资产库禁用按钮无原因【实证】

`/project/prj_de813f07bc04/assets` 实测 4 个禁用按钮（驳回返工 / 审核通过 / 去分镜图 / 批量生成），全部静默 disabled。方案 8.1 要求 Disabled 说明不可操作原因。
- 处置：**已修复**——补 `title` + `aria-label`，说明「还有 N 项未生成完成」「审核通过后才能进入分镜图」等。

### P2（细节）

| 编号 | 问题 | 证据 | 状态 |
|---|---|---|---|
| P2-1 | 解析页硬编码圆角 `8px/10px/12px/14px/999px`，绕过 `--radius-*` 令牌 | 【源码】 | 已修：全部改为令牌 |
| P2-2 | 内联样式 `style="background:var(--brand-soft);color:var(--brand-primary,var(--brand-primary))"` | 【源码】 | 已修：改 `.is-selected` 类。注意原写法 `var(--brand-primary,var(--brand-primary))` 是自我回退，无意义 |
| P2-3 | 解析原文用 `<pre>` 一次性铺开整份报告（含全部场景全部镜头描述），`max-height:600px` 内部滚动 | 【源码】 | 已修：默认折叠，`aria-expanded` 控制 |
| P2-4 | 「只看失败（0）」在 0 失败时仍渲染为禁用按钮 | 【实证】 | 已修：`v-if` 隐藏 |
| P2-5 | 移动端流程轨 `.rail-steps` 内容宽 572px > 视口 390px | 【实证】 | **不是 bug**：容器已 `overflow-x:auto`，`document.scrollWidth === 390` 无页面溢出。缺的只是滚动提示，列为 P2 待优化 |
| P2-6 | 任务中心任务类型显示原始枚举 `script_agent` | 【实证】 | 未修（见 §5 遗留项 B） |
| P2-7 | 任务中心失败任务在表格内看不到 `error_msg`，需进详情 | 【实证】 | 未修 |

### 3.1 已复核并**排除**的误报

诚实记录：上一轮基于无障碍快照曾判定「分镜图卡片铺开 3–4 行提示词全文」，**复核后不成立**。

`ShotCard.vue` 已实现 `promptDigest()`，摘要只取【目标画面】一行，`.sb-summary` 带 `-webkit-line-clamp: 2`；快照里看到的长文本来自 `:title` 属性（a11y 树会暴露 title 全文），不是可见内容。**该问题不存在，不做改动。**

---

## 4. 改造影响面与风险

| 改动 | 影响面 | 风险 | 缓解 |
|---|---|---|---|
| `ParseView.vue` 集数兜底链 | 集数面板可见性 | 低。仅在 `estimate` 为 null 时改变结果，从「错误地取 0」变为「取后端记录最大集号」 | 失败态/成功态两个项目实测 |
| 删除 `parseDone` / `allEpisodesSucceeded` | 解析完成态判定 | 中。原有两个状态源，现统一为集级解析状态 | `parseAllSucceeded` 与原逻辑等价（都是「全部集 succeeded」）；已 tsc 通过并在成功项目复测 |
| 新增 `failure-panel` | 解析页布局 | 低。纯新增区块 | 失败项目实测 |
| 删除集格重试图标 | 单集重试 | 低。`retryParse` 本身就是「重试全部失败集」的接口，无单集粒度，原图标语义本就不精确 | 已确认 API 无 per-episode 参数 |
| `AssetStatusBar` 加 title | 资产库 | 极低。仅加属性 | — |
| 移动端 header 布局 | 解析页 <768px | 低 | 390px 实测无溢出 |

**未触碰**：任何 API 路径/参数/响应结构、后端代码、数据库、枚举、状态机、SSE 通道、Query key。

---

## 5. 遗留项（按优先级）

> **2026-11-19 第五轮更新**：本节 A、B 已闭环，详见 `round5-data-and-content.md`。
> A 项的分组导航已实现（场景/角色/道具/分镜/台词/音频规划），且修掉了「分镜恒为 0」的真实数据缺陷
> —— 根因是只查 `job_type='parse'`，而该项目解析任务全落在 `script_agent`。
> B 项 `job_type` 本地化已补 `script_agent` / `series_agent`，并同步了 `types/job.ts` 的类型定义。

**A. 解析页分组导航（方案 6.3D）—— ✅ 已完成（第五轮）**
需要「分集 / 分镜 / 对白 / 音频规划」数据，但 `ParseView.vue` 当前只 query 了 `scenes` / `characters` / `props`。补齐需要接 `GET /api/projects/:id/shots` 等接口，属于新增数据通道，风险等级高于本轮 UI 改造，建议单独一轮做，并先确认对白/音频规划是否有独立接口。

**B. 任务中心类型枚举未本地化 —— ✅ 已完成（第三轮 + 第五轮补类型）**
`script_agent` 等原始值直接渲染。修复需要一张「枚举 → 中文」映射表，应放在 `constants/` 统一维护，避免各页各写一份。

**C. 其他页面未做视觉验收**
`/storyboard`、`/compose`、`/series/*`、`/settings`、`/director` 仅源码审查过，本报告不对其下结论。

**D. Pinia 非持久化是 P0-1/P0-2 类问题的共同根因**
`scriptFlow` 纯内存，任何刷新都会丢 `parseJobId` / `estimate` / `scriptVersionId`。本轮用「以后端集级状态为准」绕过了它，但同类隐患在其他依赖 store 的页面同样存在，建议后续系统性梳理：**凡是能从后端重建的状态，就不要只存在 store 里。**

---

## 6. 验证结论

| 检查 | 命令 | 结果 |
|---|---|---|
| 类型检查 | `npx vue-tsc -b` | 通过 |
| 生产构建 | `npm run build` | 通过（ParseView 24.02 kB / gzip 8.31 kB） |
| 颜色门禁 | `npm run check:colors:strict` | 待随 Phase 5 统一跑 |
| 失败态实测 | `/project/prj_de813f07bc04/parse` | 失败原因、尝试次数、重试入口、集数网格均正确显示 |
| 成功态实测 | `/project/prj_257406918e63/parse` | 统计仅一处、状态标签唯一、无重复卡片 |
| 390px 实测 | CDP 视口覆盖 | `scrollWidth === clientWidth === 390` |

> 注：构建前需确保 `frontend/dist` 无 root 属主文件（历史 root 构建残留会导致 `rimraf` EACCES）。

---

## 7. 第二轮补充（移动端与全局框架）

首轮改完在 390×844 下复验时，又暴露 4 个此前未记录的问题，已一并修复：

| 编号 | 级别 | 问题 | 证据 | 处置 |
|---|---|---|---|---|
| P1-8 | P1 | 「解析结果」标题在窄屏被 flex 压成**竖排**（4 字纵向堆叠） | 【实证】390px 下 `.result-title` 宽度塌陷 | `flex: none` + `white-space: nowrap`；`.result-head` 加 `flex-wrap` |
| P1-9 | P1 | 资产库 0 资产时，禁用提示显示「还有 **0** 项资产未生成完成」——自相矛盾 | 【实证】`title` 实测文本 | `total === 0` 分支改为「当前项目还没有资产，请先在解析页完成解析并生成资产」 |
| P2-8 | P2 | 顶部主导航在 390px 溢出 17px（scrollWidth 170 > clientWidth 153），「设置」被裁 | 【实证】`.top-nav` 实测 | 移动端 `header-inner` gap 14→8、`nav-item` padding 10→7；修复后 169 = 169 |
| P2-9 | P2 | 流程轨第 4、5 步（分镜看板 / 合成与导出）在 390px 被裁，虽可横滑但无任何提示 | 【实证】步右边界 77/157/236/327/**431** | 移动端隐藏「制作流程」标题与 01–05 序号，缩 padding；修复后 44/116/188/272/**368** 全部入屏 |

### 7.1 复验数据（390×844）

```
documentElement.scrollWidth === clientWidth === 390   // 无页面横向溢出
button 右边界越界数                           === 0    // 无被裁按钮
.rail-steps  scrollWidth > clientWidth        === false
.result-title 尺寸                            === 56 × 22  // 单行，非竖排
.overview-stats gridTemplateColumns           === 86px 86px 86px  // 3 列
```

### 7.2 静态检查（全部通过）

```
npx vue-tsc -b                    → 0 error
npm run build                     → ✓ built in 9.48s
npm run check:colors:strict       → hex 0 / rgba 0
npm run check:breakpoints:strict  → 全部为 1100/768/480/min-769/min-1101
npm run check:contrast:strict     → 全部文本令牌 ≥ 4.5:1
```

> 断点门禁曾拦下我引入的 1 处 `max-width: 1199px`（本项目标准档是 1100），已改正 —— 说明门禁确实在起作用，属正向约束。

---

## 8. 改造边界更正（2026-11-18 用户确认）

原始方案 7.6 写有「TTS 与 H3 解耦，TTS 音频不进入 H3 参考槽位」。**该表述与代码现状及产品决策相反，本次改造不得据此执行**，已记入 `docs/DECISIONS.md` D-25：

- H3 Ref2VA 工作流存在 `ref_audios.ref_audio_0/1/2` 三个音频参考槽；
- `backend/app/worker/executor.py::_shot_ref_audios` 按说话人为每个角色注入一条 `ref_audio_N`（≤3 条），并用参考音频总时长反推 `length` 帧数；
- 项目级音频策略默认 `native`：成片音轨优先取 H3 内嵌音轨，TTS 只作**音色/口型参考**，不作最终音轨（与 D-06「H3 音画同源为主链路、TTS 仅作补救」一致）。

因此：**TTS 参考音频进 H3 是正确的耦合，不是待清理的技术债**。前端任何音频/视频页面改造，都不得以减少「音画耦合」为由移除参考音频注入或相关 UI 提示。

