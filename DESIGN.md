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
| 列表页大标题（`.list-title`） | 34px（≤768px: 26px） | 1.15 | 700 | display |
| 页面标题（`.page-title`） | 30px（≤768px: 23px） | 1.2 | 700 | display |
| 区块标题（`.section-title`） | 16px | 1.5 | 600 | display |
| 卡片标题 | 15.5px | 22px | 600 | display |
| 正文 | 14px | 1.6 | 400 | ui |
| 辅助说明（`.page-subtitle`） | 13px | 1.55 | 400 | ui |
| 标签/角标（`.status-tag`） | 12px | 18px | 500 | ui |
| 阶段 chip | 11.5px | 18px | 400 | ui |
| 等宽数据 | 10.5–12px | 1.5 | 400 | mono |

负字距档位：大标题 `-0.028em`、页面标题 `-0.022em`、区块/卡片标题 `-0.008em`。
±1px 的微调允许（如 13.5px 导航文字），**不新增字号档位**。

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

## 9. Naive UI 对齐

`styles/tokens.ts` 的 `buildThemeOverrides()` 是令牌到组件库的唯一映射点：

- 颜色字段用 `token('--xxx')` 运行时读取 `:root`（`global.css` 注入后调用），
  **不允许在此写色值**，也不允许把 `var()` 直接写进 `themeOverrides`
- 圆角字段对齐 `--radius` / `--radius-sm`
- Button 的 `textColorPrimary` 系列必须指向 `--text-on-brand`，否则主按钮变青底浅字
- Input 的 `border` / `borderHover` / `borderFocus` 分别指向
  `--border-strong` / `--text-tertiary` / `--brand-primary`
- 不允许在单个页面里再写一套 `themeOverrides`

## 10. 验收

### 门禁命令（每次提交前必须通过）
```bash
cd frontend
npm run check:colors:strict       # 硬编码色值：hex / rgba 字面量必须为 0
npm run check:breakpoints:strict  # 响应式断点：只允许 1100 / 768 / 480（min-width 可用 769 / 1101）
npm run check:contrast:strict     # 对比度：全部文本令牌在四档背景上 >= 4.5:1（禁用态豁免）
npm run build                     # vue-tsc 类型检查 + 产物构建
```

### 当前状态（2026-11-19 核对，v3）
- 硬编码色值：hex 0 处 / rgba 0 处（基线曾为 hex 400 / rgba 165，25 个文件）
- 断点：已收敛至 3 档（1100 / 768 / 480），`check:breakpoints` 全绿
- 对比度：`--text-tertiary` 最低 5.67、`--danger-text` 最低 6.80，全部达标
- 布局对齐实测：四张作品卡 `height=[246,246,246,246]`、同排 `y=230`、
  `progress-head.x=41/393/745/1097`、`progress-track.y` 全 `447`
- 确认框：危险操作统一走 `components/ui/ConfirmDialog.vue`（命令式封装
  `composables/useConfirmDialog.ts`，App.vue 挂载 `GlobalConfirmDialog`），禁止直接调 `useDialog`
- 图标：统一使用 `@vicons/ionicons5`（尺寸 13/14/16/22，颜色继承 `currentColor`）；
  禁止 emoji 当图标；图标按钮必须带 `aria-label`
- 图片：列表缩略图一律 `loading="lazy"`，内容图必须有有意义的 `alt`；
  列表视频 `preload="metadata"`
- 顶栏入口：作品 / 任务中心 / 设置；导演台入口有意移除，经右侧 AI 助手面板进入
- 顶栏形态：sticky 悬浮胶囊（`--radius-xl` + `--card-sheen`），
  `grid-template-columns: 1fr auto 1fr` 保证导航落在几何中心
