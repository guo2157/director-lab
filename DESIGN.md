# DESIGN — Director Lab 设计令牌规范

> 本文件与 `frontend/src/styles/global.css` 共同构成设计唯一事实来源。
> **`global.css` 是机器可执行的令牌定义；本文件解释每个令牌的语义与允许用法。**
> 两者冲突时以 `global.css` 为准，并立即修正本文件——不允许第三种说法存在。
>
> 任何外部设计建议（设计 skill、参考站点、第三方 UI 库）与本文件冲突时，
> **一律以本文件为准**：不得引入新色值、新字体、新圆角体系。

- 方向：暗房墨青底 + 青瓷工艺色
- 适用：长时间使用的深色创作工作台（漫剧生产：剧本 → 资产 → 分镜 → 成片）
- 不做浅色主题。不做"玻璃拟态 + 大圆角 + 彩色渐变"的 SaaS 落地页风格。

---

## 1. 色彩

### 1.1 背景层级

深色界面用**背景明度差 + 边框**建立层级，不用阴影。

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--bg-base` | `#0f1618` | 页面底色 |
| `--bg-panel` | `#151e21` | 卡片、面板、表格 |
| `--bg-elevated` | `#1d272b` | 弹窗、抽屉、浮层、悬停态抬升 |
| `--bg-sunken` | `#0a1113` | 输入框、代码块、滚动条槽、凹陷区 |

层级顺序：`sunken` < `base` < `panel` < `elevated`。**不允许出现第五个背景色**；
需要更亮的区域时，用 `--overlay-hover` / `--overlay-active` 叠加，而不是新增 hex。

### 1.2 边框

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--border-subtle` | `#243035` | 常规分隔、卡片描边、表格线 |
| `--border-strong` | `#37454c` | 焦点、选中、强调分隔 |

### 1.3 品牌（青瓷）

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--brand-primary` | `#35b3a6` | 主操作、选中、进度 |
| `--brand-hover` | `#4fd1c2` | 悬停、键盘焦点 |
| `--brand-deep` | `#1f7a71` | 按下态、渐变暗端 |
| `--brand-glow` | `rgba(53,179,166,0.18)` | 光晕（少量，仅焦点态或生成中） |
| `--brand-gradient` | `linear-gradient(135deg,#35b3a6,#1f7a71)` | 仅用于品牌标识与主 CTA 底 |
| `--text-on-brand` | `#06201d` | 品牌色底上的文字 |

### 1.4 语义状态

| 令牌 | 值 | 状态 |
| --- | --- | --- |
| `--state-success` | `#57c98a` | 成功、已完成、已确认 |
| `--state-warning` | `#d9a03c` | 待确认、待重做、排队中 |
| `--state-danger` / `--danger` | `#e04e39` | 失败、删除、危险操作 |
| `--state-generating` | `#ff8a72` | 生成中（配合脉冲动效） |
| `--accent-amber` | `#c79a3b` | 审核标记、评审意见 |

### 1.5 派生透明度层（替代手写 rgba）

| 令牌 | 用途 |
| --- | --- |
| `--brand-soft` / `--brand-line` | 生成中/选中的填充与描边 |
| `--success-soft` / `--success-line` | 成功态填充与描边 |
| `--warning-soft` / `--warning-line` | 警告态填充与描边 |
| `--danger-soft` / `--danger-line` | 失败态填充与描边 |
| `--neutral-soft` / `--neutral-line` | 锁定、禁用态 |
| `--muted-soft` / `--muted-line` | 失效（stale）、历史态 |
| `--overlay-hover` | 行/卡片悬停（叠在任意背景上） |
| `--overlay-active` | 按下、激活 |
| `--overlay-selected` | 列表选中行 |
| `--scrim` | 模态遮罩 |

**禁止**在 `.vue` 里写 `rgba(...)` / `rgb(...)` / `hsl(...)` 字面量——
包括 `rgba(53,179,166,0.1)` 这种"看起来像令牌"的复制。

### 1.6 文本

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--text-primary` | `#e6ecee` | 正文、标题 |
| `--text-secondary` | `#93a5ac` | 辅助说明、元信息 |
| `--text-tertiary` | `#7f929a` | 占位符、时间戳、次级元信息 |
| `--text-disabled` | `#5a6c74` | 禁用态（WCAG 豁免，不用于可读文字） |
| `--danger-text` | `#f07a68` | 小号错误文字；`--state-danger` 只用于描边、图标、大块填充 |

对比度要求（`npm run check:contrast` 实测）：全部文本令牌在四档背景（base / panel / elevated / sunken）上均 ≥ 4.5:1；`--text-disabled` 为禁用态豁免；`--state-danger` 属非文本用途，不参与文本门禁。原要求参考：
`text-tertiary` 仅用于非关键信息，不承载操作。

---

## 2. 字体

| 令牌 | 字体栈 | 用途 |
| --- | --- | --- |
| `--font-display` | Source Han Serif SC / Noto Serif SC / Songti SC / STSong / serif | 页面标题、品牌名、成片标题 |
| `--font-ui` | HarmonyOS Sans SC / PingFang SC / Noto Sans SC / Source Han Sans SC / Microsoft YaHei / system-ui | 全部界面文字 |
| `--font-mono` | JetBrains Mono / SFMono-Regular / Consolas / monospace | 提示词、ID、时间轴、代码 |

规则：**只有这三种字体栈**。标题用衬线（display），界面文字用无衬线（ui），
提示词与 ID 用等宽（mono）。禁止引入第三种无衬线字体或图标字体替代 SVG 图标。

## 3. 字号与行高

| 角色 | 字号 | 行高 | 字重 | 字体 |
| --- | --- | --- | --- | --- |
| 页面标题（`.page-title`） | 27px | 1.25 | 600 | display |
| 页面标题（≤900px） | 22px | 1.25 | 600 | display |
| 区块标题（`.section-title`） | 15px | 1.5 | 600 | ui |
| 卡片标题 | 14–15px | 1.5 | 600 | ui |
| 正文 | 14px | 1.6 | 400 | ui |
| 辅助说明（`.page-subtitle`） | 13px | 1.5 | 400 | ui |
| 标签/角标（`.status-tag`） | 12px | 19px | 500 | ui |
| 等宽数据 | 12–13px | 1.5 | 400 | mono |

±1px 的微调允许（如 13px 按钮文字），**不新增字号档位**。

## 4. 间距

`--sp-1…--sp-16` = 4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 / 48 / 64。

- 页面内边距：`var(--sp-6) var(--sp-8) var(--sp-16)`（`.page`）
- 卡片内边距：`var(--sp-5)`（`.panel-card`）
- 区块间距：`var(--sp-6)`
- 控件内间距：`var(--sp-2)` / `var(--sp-3)`

禁止 5px、7px、10px、18px 这类自由值；需要视觉微调时改用 `gap` + 令牌组合。

## 5. 圆角与阴影

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--radius-sm` | 2px | 标签、角标、小方标 |
| `--radius` | 3px | 按钮、输入框、下拉 |
| `--radius-lg` | 4px | 卡片、面板、表格容器 |
| `--radius-xl` | 6px | 弹窗、抽屉、图片容器 |
| `--radius-pill` | 2px | 刻意"非胶囊"，状态标签保持方标 | 

阴影：`--shadow-xs/sm/card` 全部为 `none`。只有 `--shadow-lg`（弹窗）与
`--shadow-lift`（拖拽中）可用。**卡片层级靠边框，不靠阴影。**

## 6. 动效

- `--transition-fast` 150ms / `--transition-base` 220ms，缓动 `--ease-out`
- 只做透明度、位移 ≤ 4px、颜色过渡；不做弹跳与旋转
- 生成中用 `status-tag-pulse`（1.6s 透明度呼吸），不用骨架屏闪烁
- 必须尊重 `prefers-reduced-motion`

## 7. 焦点与可访问性

- 键盘焦点统一 `var(--focus-ring)`（2px 底色环 + 2px 品牌色环）
- 禁止 `outline: none` 而不提供替代焦点样式
- 可点击区域最小 28×28px；纯图标按钮必须带 `aria-label`
- 状态不能只靠颜色表达：颜色 + 文字/图标（状态标签已内置）

## 8. 禁止清单

| 禁止 | 替代 |
| --- | --- |
| `.vue` 中出现 `#xxxxxx` | 引用 `var(--token)` |
| `.vue` 中出现 `rgba()/rgb()/hsl()` 字面量 | `--brand-soft` / `--overlay-hover` 等 |
| 新增字体、字体 CDN、彩色渐变背景 | `--font-ui` / `--brand-gradient` |
| 卡片加阴影或大圆角（>6px） | `--radius-lg` + `--border-subtle` |
| 内联 `style="color:#..."` | 语义类名 + 令牌 |
| 页面里堆叠 3 种以上背景层级 | 收进 `--bg-*` 四档 |
| Naive UI 自定义色值散落在组件里 | 统一在 `App.vue` 的 `themeOverrides` 里映射到令牌 |

## 9. Naive UI 对齐

`App.vue` 的 `themeOverrides` 是令牌到组件库的唯一映射点：

- 颜色字段用 `var(--token)` 字符串（Naive UI 输出 CSS 变量，`var()` 可用）
- 圆角字段对齐 `--radius` / `--radius-lg`
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

### 当前状态（2026-11-17 核对）
- 硬编码色值：hex 0 处 / rgba 0 处（基线曾为 hex 400 / rgba 165，25 个文件）
- 断点：已收敛至 3 档（1100 / 768 / 480），`check:breakpoints` 全绿
- 对比度：`--text-tertiary` 最低 4.71、`--danger-text` 最低 5.58，全部达标
- 确认框：危险操作统一走 `components/ui/ConfirmDialog.vue`（命令式封装 `composables/useConfirmDialog.ts`，App.vue 挂载 `GlobalConfirmDialog`），禁止直接调 `useDialog`
- 图标：统一使用 `@vicons/ionicons5`（尺寸 13/14/16/22，颜色继承 `currentColor`）；禁止 emoji / 文字符号当图标；图标按钮必须带 `aria-label`
- 图片：列表缩略图一律 `loading="lazy"`，内容图必须有有意义的 `alt`；列表视频 `preload="metadata"`
- 顶栏入口：作品 / 任务中心 / 设置；导演台入口有意移除，经右侧 AI 助手面板进入
