# DESIGN — Director Lab 设计令牌规范

> 本文件与 `frontend/src/styles/global.css` 共同构成设计唯一事实来源。
> **`global.css` 是机器可执行的令牌定义；本文件解释每个令牌的语义与允许用法。**
> 两者冲突时以 `global.css` 为准，并立即修正本文件——不允许第三种说法存在。
>
> 任何外部设计建议（设计 skill、参考站点、第三方 UI 库）与本文件冲突时，
> **一律以本文件为准**：不得引入新色值、新字体、新圆角体系。

- 方向：**暗场监看室** —— 近纯黑底 + 电青→薄荷渐变，服务漫剧制作台
- 适用：长时间使用的深色创作工作台（漫剧生产：剧本 → 资产 → 分镜 → 成片）
- 不做浅色主题。
- 参考语汇（v3 引入）：pixmax.cn 的暗黑科技语言——近黑底、大圆角胶囊、顶部渐变发丝线、
  品牌青当主 CTA 填充。**只借结构，不借色值**：所有色值仍由本文件单点定义。

---

## 1. 色彩

### 1.1 背景层级

深色界面用**背景明度差 + 边框 + 顶缘内高光**建立层级，不用投影。

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--bg-base` | `#080809` | 页面底色 |
| `--bg-panel` | `#111113` | 卡片、面板、表格、悬浮导航条 |
| `--bg-elevated` | `#1a1a1d` | 弹窗、抽屉、浮层、悬停态抬升 |
| `--bg-sunken` | `#050506` | 输入框、代码块、滚动条槽、凹陷区、侧栏 |

层级顺序：`sunken` < `base` < `panel` < `elevated`。**不允许出现第五个背景色**；
需要更亮的区域时，用 `--overlay-hover` / `--overlay-active` 叠加，而不是新增 hex。

### 1.2 边框

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--border-subtle` | `#232328` | 常规分隔、卡片描边、表格线 |
| `--border-strong` | `#34343c` | 焦点、选中、强调分隔、输入框边界 |

### 1.3 品牌（电青 → 薄荷）

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--brand-primary` | `#05cefe` | 主操作、选中、进度填充、图标 |
| `--brand-hover` | `#4ce2f3` | 悬停、键盘焦点 |
| `--brand-deep` | `#0a7f9e` | 按下态、已完成刻度线 |
| `--brand-mint` | `#bffee1` | 渐变亮端、强调高光 |
| `--brand-glow` | `rgba(5,206,254,0.22)` | 光晕（少量，仅焦点态、悬停或生成中） |
| `--brand-gradient` | `linear-gradient(135deg,#05cefe,#bffee1)` | 仅用于品牌标识、主 CTA 底、进度竖条 |
| `--brand-gradient-line` | `linear-gradient(90deg,#05cefe,#bffee1)` | 顶部发丝线、渐变描边容器 |
| `--text-on-brand` | `#04191f` | 品牌色底上的文字（9.68:1） |

**规则**：品牌色当**文字色/描边色**永远安全（≥9.3:1）；当**大面积填充底**时必须配
`--text-on-brand`，绝不能用 `--text-primary`（青底浅字仅 1.9:1，`check:contrast` 查不出来）。

### 1.4 语义状态

| 令牌 | 值 | 状态 |
| --- | --- | --- |
| `--state-success` | `#3ddc97` | 成功、已完成、已确认 |
| `--state-warning` | `#f0b429` | 待确认、待重做、排队中 |
| `--state-danger` / `--danger` | `#ff5c47` | 失败、删除、危险操作 |
| `--state-generating` | `#ff8a72` | 生成中（配合脉冲动效） |
| `--accent-amber` | `#d9a33c` | 审核标记、评审意见 |

### 1.5 派生透明度层（替代手写 rgba）

| 令牌 | 用途 |
| --- | --- |
| `--brand-soft` / `--brand-line` | 生成中/选中的填充与描边 |
| `--brand-mint-soft` | 薄荷色低透明填充（强调区渐变端点） |
| `--success-soft` / `--success-line` | 成功态填充与描边 |
| `--warning-soft` / `--warning-line` | 警告态填充与描边 |
| `--danger-soft` / `--danger-line` | 失败态填充与描边 |
| `--neutral-soft` / `--neutral-line` | 锁定、禁用、阶段 chip、次轴选中态 |
| `--muted-soft` / `--muted-line` | 失效（stale）、历史态 |
| `--overlay-hover` | 行/卡片/导航项悬停（叠在任意背景上） |
| `--overlay-active` | 按下、激活 |
| `--overlay-selected` | 列表选中行 |
| `--scrim` | 模态遮罩 |

**禁止**在 `.vue` 里写 `rgba(...)` / `rgb(...)` / `hsl(...)` 字面量——
包括 `rgba(5,206,254,0.1)` 这种"看起来像令牌"的复制。

### 1.6 卡片内高光（v3 新增）

深色卡面顶缘的一道极淡亮线，替代投影表达「抬起」：

| 令牌 | 用途 |
| --- | --- |
| `--card-sheen` | 静置态卡面顶缘（`inset 0 1px 0 rgba(255,255,255,.045)`） |
| `--card-sheen-strong` | 悬停/激活态卡面顶缘 |

`.panel-card`、`.n-card`、悬浮导航条、`.project-card` 都带 `--card-sheen`。

### 1.7 文本

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--text-primary` | `#f2f5f6` | 正文、标题 |
| `--text-secondary` | `#a3aeb4` | 辅助说明、元信息 |
| `--text-tertiary` | `#8a959b` | 占位符、时间戳、次级元信息 |
| `--text-disabled` | `#5a6165` | 禁用态（WCAG 豁免，不用于可读文字） |
| `--danger-text` | `#ff7a66` | 小号错误文字；`--state-danger` 只用于描边、图标、大块填充 |

对比度要求（`npm run check:contrast:strict` 实测）：全部文本令牌在四档背景
（base / panel / elevated / sunken）上均 ≥ 4.5:1，最低为 `--text-tertiary` 5.67；
`--text-disabled` 为禁用态豁免；`--state-danger` 属非文本用途，不参与文本门禁。

---

## 2. 字体

| 令牌 | 字体栈 | 用途 |
| --- | --- | --- |
| `--font-display` | Inter / HarmonyOS Sans SC / PingFang SC / Noto Sans SC / Source Han Sans SC / Microsoft YaHei / system-ui | 页面标题、卡片标题、品牌名、区块标题 |
| `--font-ui` | 同上（与 display 同族） | 全部界面文字 |
| `--font-mono` | JetBrains Mono / SFMono-Regular / Consolas / monospace | 提示词、ID、时间轴、画幅角标、步骤序号 |

**v3 变更**：`--font-display` 由衬线（Source Han Serif）改为与 ui 同族的无衬线。
理由：参考站点的视觉重音来自**无衬线特粗 + 负字距**，而不是衬线对撞；同一族内靠
字重（400 / 500 / 600 / 700）与字距（`-0.008em` ~ `-0.028em`）拉层级，比两套字体更稳。

规则：**只有这三种字体栈**。禁止引入第三种无衬线字体或图标字体替代 SVG 图标。

## 3. 字号与行高

| 角色 | 字号 | 行高 | 字重 | 字体 |
| --- | --- | --- | --- | --- |
| 列表页大标题（`.list-title`，**已停用**） | ~~34px~~ | 1.15 | 700 | display |
| 页面标题（`.page-title`） | 30px（≤768px: 23px） | 1.2 | 700 | display |
| 区块标题（`.section-title`） | 16px | 1.5 | 600 | display |
| **顶栏导航项（`.nav-item`）** | **15.5px（≤768px: 14.5px）** | — | 500 / 激活 600 | display |
| 卡片标题 | 15.5px | 22px | 600 | display |
| 正文 | 14px | 1.6 | 400 | ui |
| 辅助说明（`.page-subtitle`） | 13px | 1.55 | 400 | ui |
| 标签/角标（`.status-tag`） | 12px | 18px | 500 | ui |
| 阶段 chip | 11.5px | 18px | 400 | ui |
| 等宽数据 | 10.5–12px | 1.5 | 400 | mono |

负字距档位：大标题 `-0.028em`、页面标题 `-0.022em`、区块/卡片标题 `-0.008em`。
±1px 的微调允许（如 13.5px 导航文字），**不新增字号档位**。

**v5 变更（2026-11-19）——顶栏导航放大为分段控件**：任务中心下线后顶栏只剩
作品 / 设置两项，原 13.5px 字 + 34px 裸胶囊在 52px 容器里读成「一个按钮 + 一个标签」
（未激活项几乎没有视觉重量）。改为：**分段控件**（`.top-nav` 轨道底 `--bg-sunken` +
4px padding）+ **15.5px 字号**（窄屏 14.5px，不再掉到 13px）+ 两项等宽（padding 0 22px）。

配套约束：
- 轨道必须比容器**更深**（`--bg-sunken` 在 `--bg-panel` 上），凹陷语义；比容器亮的轨道会读成浮起的按钮块
- 未激活字色 `--text-secondary`：实测在 `--bg-sunken` 上 **8.99:1**（AA 4.5 有余），
  比 `--text-primary`（18.59:1）低一档，不与激活态青字抢焦点
- `.header-pill` 网格必须写 `minmax(0, 1fr) auto minmax(0, 1fr)`，**不能写 `1fr auto 1fr`**：
  `1fr` 的隐含最小值是 `auto`，窄屏下品牌名固有宽度会撑宽左列、压窄右列，
  中列随之右移（实测 768 偏 2px、390 偏 27px）。锁 `minmax(0,1fr)` 后三档视口偏移均为 0
- 窄屏（≤768px）品牌名 `display: none` 只留图标：390 视口左列仅 96px，
  「图标 + Director Lab」需 124px，保留文字只能截成「Director La...」
- 窄屏 `.header-pill` padding 必须左右对称（原 `0 8px 0 12px` 再引入 2px 偏移）

**v4 变更（2026-11-19）——页内不重复顶栏标题**：顶栏胶囊导航已经是「当前在哪一页」的唯一
权威表达（品牌 soft 底 + 品牌字 + 内描边三重编码）。列表型一级页（作品列表、任务中心）
若在内容区再画一遍同名的 34px H1 + 副标题，等于同一信息说两遍，实测吃掉首屏 47–88px。
因此这两页的页头改为**单行工具条**（左侧一行规模数据，右侧操作），H1 降为
`.visually-hidden` 保留语义。34px 档位随之停用，需要页面级大标题的详情页统一走 30px
`.page-title`（如系列列表）。判据：**顶栏高亮了本页名 → 页内不再出现同名可视标题。**

**v6 变更（2026-11-19）——判据扩展到项目内页**：v4 只处理了顶栏一级页。项目内的
5 个步骤页（剧本 / 解析 / 资产库 / 分镜图 / 分镜看板 / 合成与导出）由左侧「制作流程」
竖列承担「当前在哪一步」，且该竖列用三重编码（品牌 soft 底 + 品牌字 + 内描边）高亮，
比顶栏胶囊更强。页内再画同名 22–30px 标题，等于同一信息说第三遍。

处置：全部降为 `.visually-hidden`（**不能写 `display:none`**，那会把 H1 一并从无障碍树摘掉），
`PageHeader` 增加 `compact` 模式把页头收敛为单行操作条。判据：
**左侧流程栏高亮了本阶段 → 页内不再出现同名可视标题。**

实测收益（`scripts/verify-page-header.mjs` 的 shadow-restore 断言，动态还原旧结构再量）：

| 页面 | 桌面锚点上移 | 1024 | 390 |
| --- | --- | --- | --- |
| 资产库 | +94px | +130px | +138px |
| 分镜图 | +92px | +92px | +92px |
| 剧本 | +4px | +4px | +44px |
| 解析 | +2px | +2px | +44px |

剧本/解析两页收益小，是因为旧标题只有 22px 且无副标题、无 eyebrow——这两个页面的
问题不是占空间，是信息重复。

配套删除的「完成态复述」（同类问题，不是标题）：

- 分镜图页审核闸门：审核通过后整条隐藏。通过后仍在说「已通过人工审核，可生成分镜图」，
  而用户此刻已经在分镜图页，剩余价值接近零。
- 分镜看板页资产闸门：齐全后整条隐藏。「资产已齐全，可生成分镜图与分镜视频」同理。
  闸门回答「能不能做」，与之并列的「全选 + 工作流选择 + 批量出视频」拆为独立
  `.select-bar`——它回答「做哪些」，语义不同，不该共用一个容器。
- 资产库页：生成状态句从页头移入 `.status-meta` 行（页头只留数字/进度条/审核标签，
  保证单行不折行）；审核说明仅在未通过时出现。

**v7 变更（2026-11-19）——步骤页顶部收敛为两卡**：v6 之后顶部仍散落 3–4 个同权重的横条
（状态行 / 闸门横幅 / 说明句 / 批量工具栏），层级相同、信息互相重复，读起来是「一叠横幅」。
改为两张 `StageCard`，按**观察 / 操作**分卡：

| 卡 | 语义 | 内容 |
| --- | --- | --- |
| 执行进度 | 只读：做到了哪 | 状态句、X/Y、进度条、审核标签、刷新、去下一步 |
| 批量操作 | 可写：接下来做什么 | 工作流选择、全选、已选计数、批量生成（审核） |

判据：**同一信息不在两张卡里各说一遍；观察与操作不共用一个容器。**

配套约束：

- **左侧固定列标签，不用卡片标题行**。标题行会在每张卡上方多一条 12px 的字，
  两张卡就是两行新 eyebrow——正是本次要消除的噪音。左侧列同时给两张卡建立共同对齐轴。
- **布局用 flex，不用 `grid-template-columns: 60px 1fr auto`**。meta 用 `grid-column: 2/-1`
  跨列时会与第三列争抢宽度，操作列塌缩到 60px（比它自己的 76px 按钮还窄），
  桌面端 4 个按钮被迫竖排、卡片被撑到 223px。改 flex + `flex: 0 0 auto` 后降到 83px。
- **操作区按钮桌面必须单行**。390px 下 4 个按钮共需 342px 而内容区仅 324px，折两行是
  合理的响应式行为；但桌面端竖排一律视为 bug。
- **删页头时 H1 必须留下**。资产库页删掉 `PageHeader` 时曾把 H1 一并丢掉，
  导致该页在无障碍树里没有任何标题层级——`.visually-hidden` 的 H1 是硬要求。

实测收益（首屏内容起点，Playwright 几何测量）：

| 页面 | 1440 | 1024 | 768 | 390 |
| --- | --- | --- | --- | --- |
| 资产库 | 387→**247** | 274 | 445 | 510 |
| 分镜图 | 400→**264** | 289 | 435 | 464 |
| 分镜看板 | 351→**241** | 255 | 400 | 428 |

资产库窄屏 510px 已是该信息量的下界（4 个审核按钮折两行 + 4 项分项状态折三行），不是缺陷。

## 4. 间距

`--sp-1…--sp-16` = 4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 / 48 / 64。

- 页面内边距：`var(--sp-6) var(--sp-8) var(--sp-16)`（`.page`）
- 卡片内边距：`var(--sp-4)`（列表卡）/ `var(--sp-5)`（`.panel-card`）
- 区块间距：`var(--sp-6)`
- 控件内间距：`var(--sp-2)` / `var(--sp-3)`
- 列表栅格间距：`var(--sp-4)`

禁止 5px、7px、10px、18px 这类自由值；需要视觉微调时改用 `gap` + 令牌组合。

## 5. 圆角与阴影

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--radius-sm` | 8px | 角标、画幅 chip、步骤序号方块 |
| `--radius` | 10px | 按钮、输入框、下拉（Naive UI 同步） |
| `--radius-md` | 12px | 侧栏步骤行、提示横幅 |
| `--radius-lg` | 14px | 卡片、面板、表格容器 |
| `--radius-xl` | 20px | 列表卡、悬浮导航条、弹窗、渐变描边容器 |
| `--radius-pill` | 999px | 状态徽标、筛选 chip、排序分段、阶段 chip |

**v3 变更**：圆角由 2/3/4/6px 锐角体系改为 8–20px + 胶囊。原因：v2 的 2–3px 在
近黑底上读起来像渲染未完成，且与胶囊状状态标签并存造成「四套圆角语言」。
新体系只有三档实体圆角 + 胶囊，且**同一层级控件用同一档**：
容器 20px / 面板 14px / 控件 10px / 角标 8px / 胶囊类一律 `--radius-pill`。

阴影：`--shadow-xs/sm/card` 全部为 `none`。只有 `--shadow-lg`（弹窗）、
`--shadow-lift`（拖拽中）、`--shadow-glow`（悬停发光）可用。
**卡片静置态层级靠边框 + `--card-sheen`，不靠阴影。**

## 6. 动效

- `--transition-fast` 160ms / `--transition-base` 240ms，缓动 `--ease-out`
- 只做透明度、位移 ≤ 4px、颜色过渡；不做弹跳与旋转
- 卡片入场：`card-fade-up` 0.4s（`translateY(10px)` + 透明度），错峰 `0.045s × index`（上限 12 张）
- 悬停抬升：`translateY(-2px)` + `--shadow-glow`，只给列表卡与尾部新建条
- 生成中用 `status-tag-pulse`（1.6s 透明度呼吸），不用骨架屏闪烁
- 必须尊重 `prefers-reduced-motion`（入场与抬升一律关掉）

## 7. 焦点与可访问性

- 键盘焦点统一 `var(--focus-ring)`（2px 底色环 + 2px 品牌色环）
- 禁止 `outline: none` 而不提供替代焦点样式
- 可点击区域最小 28×28px；纯图标按钮必须带 `aria-label`
- 状态不能只靠颜色表达：颜色 + 圆点 + 文字（`.status-tag` 已内置 `::before` 圆点）
- 深色主题下「当前项」至少要有**两种编码**（色块 / 竖条 / 加粗 / 勾选 / 反色编号），
  单靠背景明度差一律视为不够

## 8. 禁止清单

| 禁止 | 替代 |
| --- | --- |
| `.vue` 中出现 `#xxxxxx` | 引用 `var(--token)` |
| `.vue` 中出现 `rgba()/rgb()/hsl()` 字面量 | `--brand-soft` / `--overlay-hover` 等 |
| 新增字体、字体 CDN、彩色渐变背景 | `--font-ui` / `--brand-gradient` |
| 卡片加投影表达层级 | `--card-sheen` + `--border-subtle` |
| 内联 `style="color:#..."` | 语义类名 + 令牌 |
| 页面里堆叠 3 种以上背景层级 | 收进 `--bg-*` 四档 |
| 品牌色填充底 + 浅色文字 | `--text-on-brand`（`#04191f`） |
| `--overlay-*` 当文字色 | `--text-*` 或 `--border-strong`（水印档） |
| Naive UI 自定义色值散落在组件里 | 统一在 `styles/tokens.ts` 的 `buildThemeOverrides` 里映射到令牌 |
| 辅助入口用实心品牌色 + 同色光晕 + 胶囊圆角 | 次级按钮：`--bg-elevated` + `--border-strong`，`--brand-primary` 仅用于图标 |
| 用 `+` `×` `⋯` 等文字符号当图标 | `@vicons/ionicons5` 图标 + `aria-label` |

例外：`.launch-strip`（尾部新建条）的 `+` 是排版符号而非图标，与相邻文案同基线对齐，
保留文字形式；卡片「⋯」更多菜单仍为文字符号（v2 遗留，待图标化）。

**视觉权重排序（不可倒置）**：页面主 CTA（如「新建作品」「开始分析」）> 页面次级操作
> 全局辅助入口（AI 助手等）。辅助入口的饱和度、面积、阴影都必须低于主 CTA——
它是随时可用的工具，不是当前页面要你做的事。

**两条筛选轴分级**：主轴（类型/状态筛选 chip）用品牌 soft + 品牌字；次轴（排序分段）
用中性 soft + 正文色 + 内描边。两轴同时实心高亮 = 没有主次。

**（2026-11-19 补充）能删掉一整个轴就别保留**：作品列表原有「状态 chips + 排序分段」
两条轴，但排序只有两个选项、默认就是最近更新，几乎无人切换——它占着工具栏右侧一整块
宽度却不产生决策。删掉后固定按 `updated_at` 倒序，功能等价、工具栏收回一整段空间。
判据：**一个控件若 90% 时间停留在默认值，它就不是控件，是装饰。**

## 9. Naive UI 对齐

`styles/tokens.ts` 的 `buildThemeOverrides()` 是令牌到组件库的唯一映射点：

- 颜色字段用 `token('--xxx')` 运行时读取 `:root`（`global.css` 注入后调用），
  **不允许在此写色值**，也不允许把 `var()` 直接写进 `themeOverrides`
- 圆角字段对齐 `--radius` / `--radius-sm`
- Button 的 `textColorPrimary` 系列必须指向 `--text-on-brand`，否则主按钮变青底浅字
- Input 的 `border` / `borderHover` / `borderFocus` 分别指向
  `--border-strong` / `--text-tertiary` / `--brand-primary`
- 不允许在单个页面里再写一套 `themeOverrides`

## 10. 响应式断点

只允许三档，`npm run check:breakpoints:strict` 强制校验：

| 断点 | 语义 | 典型动作 |
| --- | --- | --- |
| `1100px` | 桌面收窄 | 侧栏/助手面板退让、网格降列、分栏转单列 |
| `768px` | 平板 | 布局转单列、紧凑内边距、工具栏换行、触控放大 |
| `480px` | 手机 | 最小可达点击区域、极端窄屏文字折行 |

配套的 `min-width: 769px` / `min-width: 1101px` 仅用于与上档互补的桌面条件（`ScriptView` 分栏判定）。

- 禁止 900 / 960 / 980 / 1020 / 1024 / 1440 等历史断点值
- 新断点必须先加进本表并同步 `check-breakpoints.mjs` 白名单，否则 CI 失败
- 桌面优先：默认样式即最宽档，`@media` 只写降级

## 11. 图标

统一使用 `@vicons/ionicons5`，通过 Naive UI 的 `n-icon` 渲染：

```vue
<n-icon :component="Checkmark" :size="14" />
```

| 尺寸 | 用途 |
| --- | --- |
| `13px` | 行内状态、标签内、按钮内嵌 |
| `14px` | 列表操作按钮、工具栏 |
| `16px` | 卡片标题、区块标识 |
| `22px` | 创建类型卡片等大图标 |

- 颜色不单独设置，继承 `currentColor`，随文字令牌走
- 纯装饰图标加 `aria-hidden="true"`；承担点击的图标按钮必须带 `aria-label`
- **禁止 emoji（🎬🔍🔒）与文字符号（✓ ✗ → ↻ ▶）当图标**；日志文本、`<kbd>` 按键提示、注释中的箭头不受此限
- 图标不单独承载语义，必须与文字或标签并存

## 12. 按钮

| 层级 | Naive UI | 使用场景 |
| --- | --- | --- |
| 主操作 | `type="primary"` | 每屏最多一个，如「开始分析」「去分镜图」 |
| 次操作 | `secondary` / `quaternary` | 刷新、重试、返回、跳转 |
| 危险操作 | `ConfirmDialog` + `danger: true` | 删除、覆盖、强制替换 |

- 尺寸固定 `small` / `tiny` 两档，不写自定义 padding
- 文案动词开头，2–6 字，如「重新解析」「采用优化稿」；不用「确定」「提交」这类空词
- 危险按钮必须写明作用对象，如「删除作品」而非「删除」
- 异步操作必须 `:loading`，防止重复提交
- 同一行内最多一个 primary

## 13. 表单

- 一律用 `n-form` + `n-form-item`，标签在上（`label-placement="top"`），不写自适应左标签
- 必填项在标签后附红色 `*`，由 `n-form-item` 的 `required` 提供，不手写字符
- 校验信息走 `n-form-item` 的 `feedback`，不弹 toast
- 输入框宽度默认撑满容器，短字段用 `n-input-number` 并限制 `min` / `max`
- 保存按钮置于表单右下，取消在左侧；未改动时保存按钮 `disabled`

## 14. 卡片

- 结构：标题区（`h3` + 状态标签）→ 内容区 → 操作区，三段间距统一 `var(--sp-4)`
- 容器 `background: var(--bg-elevated)` + `1px solid var(--border-subtle)` + `var(--radius-lg)`
- **不加阴影、不加 >6px 圆角**，层级靠边框区分
- 整卡可点击时，hover 只改变边框色与背景，不位移、不缩放
- 卡内图片统一 `aspect-ratio` 占位，避免加载时撑跳布局

## 15. 状态与组件状态

### 15.1 页面与区块状态矩阵

| 状态 | 表现 | 组件 |
| --- | --- | --- |
| Loading | 骨架行/卡片占位，不用转圈遮罩 | `SkeletonList` |
| Empty | 说明文字 + 一个引导动作 | `EmptyState` |
| Error | 失败原因 + 重试按钮 | `ErrorRetry` |
| Success | 结果就地展示，不弹全局提示 | 页面内区块 |
| Disabled | `--text-disabled` 文字 + 保留容器骨架 | 原生 `disabled` |

### 15.2 任务状态矩阵

| 状态 | 令牌 | 呈现 |
| --- | --- | --- |
| Pending | `--text-tertiary` | 排队中 + 时间图标 |
| Processing | `--brand-primary` | 进行中 + 百分比 + 进度条 |
| Completed | `--state-success` | 已完成 + Checkmark |
| Failed | `--danger-text` | 失败 + 错误码 + 重试入口 |
| Cancelled | `--text-secondary` | 已取消，不提供重试 |

### 15.3 交互态矩阵

| 态 | 视觉 |
| --- | --- |
| Hover | 背景提亮一档（`--overlay-hover`），不改字号与位移 |
| Active | 背景再提亮一档（`--overlay-active`） |
| Focus | `var(--focus-ring)`，键盘可达必备 |
| Disabled | 透明度或 `--text-disabled`，同时禁用指针事件 |

## 15.4 层级（z-index）

| 层 | 值 | 使用位置 |
| --- | --- | --- |
| 卡片内浮层 | `2` | `ShotCard` 角标、`ChatComposer` 吸底 |
| 列表批量条 | `4` | `AssetBatchToolbar` / `BatchToolbar` |
| 面板内吸顶 | `5` | `DirectorAgentPanel` 头部 |
| 页面级浮层 | `20` | `DirectorConsoleView` |
| 助手面板 | `150` | `DirectorAgentPanel` 抽屉 |
| 模态层 | `200` | 全局弹窗（`ConfirmDialog` 等，走 Naive UI 默认） |

新增层级必须写入本表；不允许出现未登记的自定义 z-index 值。

## 15.5 文案规范

| 场景 | 规则 | 示例 |
| --- | --- | --- |
| 标题 | ≤ 30 字，名词短语 | 剧本解析、资产库 |
| 按钮 | 动词开头，写明对象 | 重新解析、删除作品 |
| 错误 | 发生了什么 + 怎么办 | 无法获取任务状态，请返回剧本页重新发起 |
| 空状态 | 说明 + 一个动作 | 尚无分镜图，先去资产库生成角色图 |
| 危险确认 | 写明不可逆后果与影响范围 | 删除后场景、镜头、资产与任务记录一并清除 |
| 一致性 | 同一概念全站同名 | 统一用「分镜图」，不与「关键帧」混用 |

- 不用「出错了」「操作失败」这类无信息文案
- 不用感叹号与营销语气
- 数字与单位之间留半角空格：`3 集`、`12 个分镜`

## 16. 验收

### 门禁命令（每次提交前必须通过）
```bash
cd frontend
npm run check:colors:strict       # 硬编码色值：hex / rgba 字面量必须为 0
npm run check:breakpoints:strict  # 响应式断点：只允许 1100 / 768 / 480（min-width 可用 769 / 1101）
npm run check:contrast:strict     # 对比度：全部文本令牌在四档背景上 >= 4.5:1（禁用态豁免）
npm run build                     # vue-tsc 类型检查 + 产物构建
```

### 当前状态（2026-11-19 核对，v5）
- 硬编码色值：hex 0 处 / rgba 0 处（基线曾为 hex 400 / rgba 165，25 个文件）
- 断点：已收敛至 3 档（1100 / 768 / 480），`check:breakpoints` 除
  `components/assets/AssetBatchToolbar.vue:149`（`max-width: 640px`，并行改动引入，待收敛）外全绿
- 对比度：`--text-tertiary` 最低 5.67、`--danger-text` 最低 6.80，全部达标
- 顶栏导航规格实测（`frontend/scripts/verify-nav-spec.mjs`，三档全 PASS）：
  字号 15.5 / 14.5 / 14.5px；两项等宽 75/75（1440）、57/57（768·390）；
  导航几何中心偏移 **0px**（修复前 768 偏 2px、390 偏 27px，根因 `1fr` 的 `auto` 最小值）；
  轨道 `rgb(5,5,6)` 深于容器 `rgb(17,17,19)`；横向溢出 0、被裁按钮 0
- 作品列表单行工具栏实测（`frontend/scripts/verify-toolbar.mjs`，四档全 PASS）：
  1440/1100 单行 `h=36`，chips/搜索/新建中心线同为 `104`；768/390 折两行 `h=78`
  （chips 89 / 搜索+新建 132）；`sort-group` 计数 0（排序轴已删）、
  「共 N 个作品」文案已不存在；新建按钮图标间距 6px（Naive `.n-button__content` 坑已修）；
  390 下工具栏与卡片网格右边界同为 374，距视口 16px 对称留白，chips 无截断
  （`scrollWidth == clientWidth == 358`）
- 页头去重 + 任务中心下线实测（`frontend/scripts/verify-nav-cleanup.mjs`，三档视口 6 组全 PASS）：
  顶栏仅剩 `[作品, 设置]` 且几何居中误差 0px；作品列表内容区起点 1440 下 230→188、
  768 下 256→206、390 下 300→250；`/jobs` 旧链接重定向到 `/` 并正常渲染 4 张卡片；
  H1 均为 `clip=rect(0,0,0,0)`（语义在、视觉不占位），横向溢出 0、被裁按钮 0
- 产物变化：`JobsView` 分块消失，naive-ui 主包 771.77 kB → 690.41 kB（`n-data-table` 仅它使用）
- 布局对齐实测（1440×900，封面图加载后）：四张作品卡 `height=[381,381,381,381]`、
  同排 `y=188`、`progress-head.x=41/393/745/1097`、`progress-track.y` 全 `540`。
  卡片高度由封面图比例决定（1366 宽下为 367），故该数值随视口列宽变化，不是固定值
- 确认框：危险操作统一走 `components/ui/ConfirmDialog.vue`（命令式封装
  `composables/useConfirmDialog.ts`，App.vue 挂载 `GlobalConfirmDialog`），禁止直接调 `useDialog`
- 图标：统一使用 `@vicons/ionicons5`（尺寸 13/14/16/22，颜色继承 `currentColor`）；
  禁止 emoji 当图标；图标按钮必须带 `aria-label`
- 图片：列表缩略图一律 `loading="lazy"`，内容图必须有有意义的 `alt`；
  列表视频 `preload="metadata"`
- 顶栏入口：作品 / 设置（**任务中心已于 2026-11-19 下线**，任务进度改在分镜图/资产/解析页内联展示）；
  导演台入口有意移除，经右侧 AI 助手面板进入
- 顶栏右侧 ComfyUI 快捷入口（**2026-11-24 新增，当日二次改版**）：把设置页配好的实例地址变成一次点击可达的外部链接，
  按启用状态过滤并去重；**单实例点击直接在新标签页打开**，**多实例展开下拉由用户选**（下拉项 = 序号徽章 + 等宽地址），
  零实例不留死按钮——提示并跳设置页。
  规格：**品牌渐变标记 + 分色 wordmark**——`.comfy-mark`（`--brand-gradient` 圆角方块，内含两个输入节点
  → 一个输出节点的直角连线，是 ComfyUI 的 node graph 语义；不用通用 `n-icon`，也不与「分享」图标撞形）、
  文字 17px/700 大于品牌名 15px（需求「字体可以大一点」）、`Comfy` 走 `--text-primary` + `UI` 走
  `--brand-primary`（分色是「彩色」的全部来源，不加整块品牌填充）。容器 40px 高、
  `--bg-elevated` 底 + `--border-strong` 描边，悬停转 `--brand-line` + 1px 品牌描边环。
  **窄屏必须保留 ComfyUI 文字**：≤480 降为 13px/700 + 20px 标记（导航内距 14→10 让出空间），
  任何视口都不允许 `display:none` 藏掉 wordmark。
- 顶栏形态：sticky 悬浮胶囊（`--radius-xl` + `--card-sheen`），
  `grid-template-columns: 1fr auto 1fr` 保证导航落在几何中心
