/**
 * 设计令牌桥接层 —— 让 JS（Naive UI themeOverrides）读同一份 CSS 令牌。
 *
 * 唯一事实来源：`frontend/src/styles/global.css`
 * 本文件不定义任何色值，只负责在运行时把 CSS 变量读出来交给 Naive UI，
 * 因此改主题只需要改 global.css 一处。
 *
 * 注意：必须在 global.css 已注入后调用（App 挂载时即满足）。
 */

import type { GlobalThemeOverrides } from 'naive-ui'

const cache = new Map<string, string>()

/** 读取 :root 上的 CSS 变量原始文本（十六进制或透明色均由 global.css 提供） */
export function token(name: `--${string}`): string {
  const cached = cache.get(name)
  if (cached !== undefined) return cached

  let value = ''
  if (typeof window !== 'undefined') {
    value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  }
  // 读不到时回退成 var() 表达式，交给浏览器解析，避免出现空色值
  if (!value) value = `var(${name})`

  cache.set(name, value)
  return value
}

/** 清空缓存 —— 仅在运行时切换主题令牌时需要（本项目为固定深色主题，一般用不到） */
export function resetTokenCache(): void {
  cache.clear()
}

/**
 * Naive UI 主题覆盖：全部来自 CSS 令牌，禁止在此写色值。
 * 覆盖范围与 global.css 的语义一一对应。
 */
export function buildThemeOverrides(): GlobalThemeOverrides {
  return {
    common: {
      fontFamily: token('--font-ui'),
      fontSize: '14px',
      borderRadius: token('--radius'),
      borderRadiusSmall: token('--radius-sm'),

      primaryColor: token('--brand-primary'),
      primaryColorHover: token('--brand-hover'),
      primaryColorPressed: token('--brand-deep'),
      primaryColorSuppl: token('--brand-primary'),
      infoColor: token('--brand-primary'),
      infoColorHover: token('--brand-hover'),
      infoColorPressed: token('--brand-deep'),
      successColor: token('--state-success'),
      successColorHover: token('--state-success'),
      successColorPressed: token('--state-success'),
      warningColor: token('--state-warning'),
      errorColor: token('--state-danger'),

      bodyColor: token('--bg-base'),
      cardColor: token('--bg-panel'),
      modalColor: token('--bg-elevated'),
      popoverColor: token('--bg-elevated'),
      tableColor: token('--bg-panel'),
      tableHeaderColor: token('--bg-panel'),
      inputColor: token('--bg-sunken'),
      actionColor: token('--bg-elevated'),

      borderColor: token('--border-subtle'),
      dividerColor: token('--border-subtle'),

      textColorBase: token('--text-primary'),
      textColor1: token('--text-primary'),
      textColor2: token('--text-secondary'),
      textColor3: token('--text-tertiary'),
      placeholderColor: token('--text-tertiary'),

      hoverColor: token('--overlay-hover')
    },
    Card: {
      borderColor: token('--border-subtle')
    },
    Button: {
      fontWeight: '500',
      fontWeightStrong: '600',
      /* 主按钮走品牌渐变底 + 深色字：品牌青当大面积填充时不能用浅字（对比仅 1.9:1） */
      colorPrimary: token('--brand-primary'),
      textColorPrimary: token('--text-on-brand'),
      textColorHoverPrimary: token('--text-on-brand'),
      textColorPressedPrimary: token('--text-on-brand'),
      textColorFocusPrimary: token('--text-on-brand'),
      borderPrimary: '1px solid transparent',
      borderHoverPrimary: '1px solid transparent',
      borderPressedPrimary: '1px solid transparent',
      borderFocusPrimary: '1px solid transparent',

      /* 禁用态（2026-11-21 修）：Naive UI 的 disabled 默认只把整体 opacity 压到
       * 0.38，于是「品牌青底 + --text-on-brand 深墨字」的对比度被等比缩到约
       * 1.4:1 —— 实测分镜图/资产库/分镜看板的批量按钮在未勾选时几乎看不出
       * 上面写的是什么（截图里就是一团半透明青色 + 隐约黑字）。
       *
       * 这里只把 opacity 拉回 1；底色与文字的禁用配色交给 global.css 的
       * `.n-button--disabled` 规则。
       *
       * 为什么不在这里写 colorDisabled / textColorDisabled：
       * Naive UI 的 Button 主题是**按 type 派生**的 —— primary 类型实际读取的是
       * colorDisabledPrimary / textColorDisabledPrimary / borderDisabledPrimary
       * （见 naive-ui/es/button/styles/light.mjs）。在 Button 覆盖对象里写通用键
       * 只会改默认类型的回退值，对 type="primary" 完全无效（实测改完仍是
       * 品牌青底 + 纯黑字）。而写 *DisabledPrimary 这类派生键又不被
       * ExtractThemeOverrides<ButtonTheme> 接受（TS2353）。
       * 走 CSS 类既绕开类型限制，也能一次覆盖 primary / warning / default 全部类型。 */
      opacityDisabled: '1',
    },
    Input: {
      border: `1px solid ${token('--border-strong')}`,
      borderHover: `1px solid ${token('--text-tertiary')}`,
      borderFocus: `1px solid ${token('--brand-primary')}`,
      boxShadowFocus: `0 0 0 2px ${token('--brand-soft')}`
    },
    Tag: {
      borderRadius: token('--radius-pill')
    },
    Modal: {
      color: token('--bg-elevated')
    },
    Dropdown: {
      color: token('--bg-elevated'),
      optionColorHover: token('--overlay-hover')
    },
    Progress: {
      railColor: token('--border-subtle')
    },
    Menu: {
      itemTextColor: token('--text-secondary')
    }
  }
}
