# Design System 规范（现状固化 + 差距清单）

> 本文件**不是**新规范，而是对既有事实的固化与用法约束。
> 权威源：`frontend/src/styles/global.css`（机器可执行）+ `DESIGN.md`（语义）+ `docs/FRONTEND.md`（页面与交互）。
> 冲突时以上述三者为准；本文件只负责「怎么用、别踩什么坑」。

---

## 1. 为什么不重新定义令牌

方案原文建议一套浅色蓝 SaaS 令牌（`#4f6bed` / 浅灰底 / 6–14px 圆角 / 弹窗阴影），与本项目方向相反：

| 维度 | 方案建议 | 项目现状与依据 |
|---|---|---|
| 主题 | 浅色 `#f6f7f9` | **深色为默认**（`FRONTEND.md` §2：长时间使用 + 大量预览图像视频，深色降低画面干扰） |
| 品牌色 | 蓝 `#4f6bed` | 青瓷 `#35b3a6`（已用于全站主操作与选中态） |
| 圆角 | 6/10/14px | 2/3/4px（刻意收紧，避免「圆润 SaaS」观感） |
| 层级 | `--shadow-popover` 等阴影 | 阴影全为 `none`，**层级由边框与背景明度差表达** |

因此本次改造的定位是**收敛**（把散落硬编码收回令牌），不是**换皮**。

---

## 2. 令牌清单（摘要，值以 global.css 为准）

### 2.1 背景层级（4 级，不可自行新增）

```
--bg-sunken   #0a1113   输入框 / 代码块 / 凹陷区 / 顶栏
--bg-base     #0f1618   页面底色
--bg-panel    #151e21   卡片 / 面板 / 表格
--bg-elevated #1d272b   弹窗 / 抽屉 / 浮层
```

### 2.2 边框（2 级）

```
--border-subtle  #243035   常规分隔
--border-strong  #37454c   焦点 / 选中
```

### 2.3 语义色 + 派生层

每个状态色都配 `-soft`（低透明度填充）与 `-line`（描边）：

| 语义 | 主色 | 派生 |
|---|---|---|
| 品牌 / 主操作 | `--brand-primary` `#35b3a6` | `--brand-soft` / `--brand-line` |
| 成功 | `--state-success` `#57c98a` | `--success-soft` / `--success-line` |
| 警告 | `--state-warning` `#d9a03c` | `--warning-soft` / `--warning-line` |
| 失败 | `--danger` `#e04e39` | `--danger-soft` / `--danger-line` |
| 生成中 | `--state-generating` `#ff8a72` | 复用 danger 派生 |
| 中性 | — | `--neutral-soft` / `--neutral-line` |
| 禁用 | — | `--muted-soft` / `--muted-line` |

**小号红字必须用 `--danger-text`（`#f07a68`）**，不是 `--danger`：后者在深底上对比度不足，仅用于描边/图标/填充。

### 2.4 文本层级

```
--text-primary    #e6ecee   正文
--text-secondary  #93a5ac   辅助说明
--text-tertiary   #7f929a   占位、时间戳、次要元信息
--text-disabled   #5a6c74   仅禁用态（WCAG 豁免）
```

### 2.5 间距（4 的倍数，禁止魔数）

`--sp-1..--sp-16` = 4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 / 48 / 64

### 2.6 圆角

`--radius-sm: 2px` / `--radius: 3px` / `--radius-lg: 4px` / `--radius-xl: 6px` / `--radius-pill: 2px`

> 本项是本次改造中**最常被违反**的令牌。首轮 `ParseView.vue` 里散落着 `8px / 10px / 12px / 14px / 999px`，已全部收回。

### 2.7 交互与动效

```
--overlay-hover / --overlay-active / --overlay-selected   叠加层，不引入新底色
--transition-fast  150ms
--transition-base  220ms
--ease-out         cubic-bezier(0.16, 1, 0.3, 1)
--focus-ring       键盘焦点环，页面不得自定义 outline 颜色
```

`prefers-reduced-motion: reduce` 已在 global.css 全局兜底，无需页面重复实现。

---

## 3. 三条硬规则

### 规则 1：页面与组件不得出现色值字面量

```bash
npm run check:colors:strict    # 必须 hex 0 / rgba 0
```

需要更亮/更暗的区域时**叠加 `--overlay-*`**，不改底色。需要半透明时用现成的 `-soft` / `-line`，不手写 `rgba()`。

### 规则 2：断点只允许四档

```bash
npm run check:breakpoints:strict
```

标准档：`max-width: 1100 / 768 / 480`，配套 `min-width: 769 / 1101`。

> 本次改造曾写入 `max-width: 1199px`（照抄方案 9.1 的断点建议）被门禁拦下。**方案的断点与项目不一致，以门禁为准。**

### 规则 3：文本令牌对比度 ≥ 4.5:1

```bash
npm run check:contrast:strict
```

`--text-secondary` / `--text-tertiary` / `--brand-primary` 等在 7:1 以下，**可作次级信息但不宜承载长段落**。

---

## 4. 组件用法约定（基于既有 `components/ui/`）

| 场景 | 用什么 | 不要用什么 |
|---|---|---|
| 页面标题 + 操作 | `PageHeader.vue` | 每页手写 `<h1>` + 按钮行 |
| 状态徽标 | `StatusTag.vue` + `.status-tag.is-*` | 每页自定义彩色 span |
| 空数据 | `EmptyState.vue` | 裸 `<NEmpty>` 且无下一步入口 |
| 加载/错误 | `SkeletonList.vue` / `ErrorRetry.vue` | 只显示一个转圈 |
| 危险确认 | `useConfirmDialog` / `ConfirmDialog.vue` | `window.confirm` |
| 长文本编辑 | `PromptEditor.vue` | 裸 textarea |

### 4.1 主按钮唯一性

同一屏**只允许一个 `type="primary"`**。首轮解析页曾有「继续分析」「解析选中」两个 primary 竞争，已收敛为：有失败时唯一 primary 是「重试失败集」。

### 4.2 禁用必须给原因

`disabled` 的按钮需带 `title` + `aria-label` 说明不可用原因，例如：

```html
:title="!allDone ? `还有 ${total - done} 项资产未生成完成` : undefined"
```

注意边界：`total === 0` 时若仍用 `total - done` 会输出「还有 **0** 项未完成」这种自相矛盾的文案，必须单独分支。

### 4.3 同一动作只出现一次

重试/删除这类动作，全屏只保留一个主入口。首轮解析页「重试」出现 4 次（工具栏 + 每个失败集格 + 失败卡片 + 空状态），已收敛为 1 次。

---

## 5. 已知差距（未做）

| 差距 | 影响 | 建议 |
|---|---|---|
| Naive UI `themeOverrides` 部分组件未完全对齐（`n-data-table` 行高、`n-tag` 圆角） | 表格与标签存在细微不一致 | 在 `styles/tokens.ts` 补齐，勿在页面覆盖 |
| 无亮色主题 | — | **不建议新增**：`FRONTEND.md` 明确深色为默认，半成品亮色反而制造维护负担 |
| `check:contrast` 只覆盖文本令牌，未覆盖状态标签背景上的文字 | 极端情况可能不足 | 扩展脚本覆盖 `.status-tag` 组合 |
| 动效无统一封装 | 各组件 transition 写法略有差异 | 低优先，`--transition-*` 已足够 |
