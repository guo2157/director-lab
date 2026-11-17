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

/** 读取 :root 上的 CSS 变量原始文本（如 `#35b3a6`、`rgba(53, 179, 166, 0.1)`） */
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
      borderRadius: '3px',
      borderRadiusSmall: '2px',

      primaryColor: token('--brand-primary'),
      primaryColorHover: token('--brand-hover'),
      primaryColorPressed: token('--brand-deep'),
      primaryColorSuppl: token('--brand-primary'),
      infoColor: token('--brand-primary'),
      infoColorHover: token('--brand-hover'),
      infoColorPressed: token('--brand-deep'),
      successColor: token('--state-success'),
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
      fontWeightStrong: '600'
    },
    Menu: {
      itemTextColor: token('--text-secondary')
    }
  }
}
