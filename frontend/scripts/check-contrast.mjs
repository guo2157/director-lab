#!/usr/bin/env node
/**
 * 对比度检查器 —— 设计令牌可读性验收工具
 *
 * 规则来源：DESIGN.md / frontend/src/styles/global.css（唯一事实来源）
 *
 * 用法：
 *   node scripts/check-contrast.mjs           # 输出对比度矩阵
 *   node scripts/check-contrast.mjs --json    # 机器可读，便于 CI 与归档
 *   node scripts/check-contrast.mjs --fail    # 任一层级低于 4.5:1 时退出码 1
 *
 * 口径：
 *   - 只读取 global.css 的 :root 令牌，不在脚本里写死任何色值
 *   - 文本类前景要求 >= 4.5:1；禁用态（--text-disabled）按 WCAG 豁免
 *   - 非文本用途（描边 / 图标 / 大块填充）不参与门禁，仅打印参考值
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const TOKEN_FILE = join(ROOT, 'src', 'styles', 'global.css')

const args = process.argv.slice(2)
const asJson = args.includes('--json')
const shouldFail = args.includes('--fail')

/** 文本类前景：参与门禁 */
const TEXT_FG = [
  '--text-primary',
  '--text-secondary',
  '--text-tertiary',
  '--brand-primary',
  '--state-success',
  '--state-warning',
  '--danger-text',
  '--state-generating',
  '--accent-amber'
]
/** 禁用态：WCAG 豁免，仅打印 */
const EXEMPT_FG = ['--text-disabled']
/** 非文本用途（描边 / 图标 / 大块填充）：不参与门禁，仅打印参考值 */
const NON_TEXT_FG = ['--state-danger']
const BACKGROUNDS = ['--bg-base', '--bg-panel', '--bg-elevated', '--bg-sunken']

/** 解析 :root 里的 CSS 变量 */
function parseTokens(css) {
  const rootMatch = css.match(/:root\s*\{([\s\S]*?)\n\}/)
  if (!rootMatch) throw new Error('未在 global.css 中找到 :root 令牌块')
  const body = rootMatch[1].replace(/\/\*[\s\S]*?\*\//g, '')
  const map = new Map()
  const re = /(--[a-z0-9-]+)\s*:\s*([^;]+);/gi
  let m
  while ((m = re.exec(body))) map.set(m[1], m[2].trim())
  return map
}

/** 解析 var(--x) 引用链 */
function resolve(name, map, depth = 0) {
  if (depth > 8) return null
  const raw = map.get(name)
  if (!raw) return null
  const ref = raw.match(/^var\(\s*(--[a-z0-9-]+)\s*\)$/i)
  if (ref) return resolve(ref[1], map, depth + 1)
  return raw
}

/** #rgb / #rrggbb -> [r,g,b] */
function toRgb(value) {
  if (!value) return null
  const v = value.trim()
  let hex = null
  if (/^#[0-9a-f]{3}$/i.test(v)) {
    hex = v.slice(1).split('').map((c) => c + c).join('')
  } else if (/^#[0-9a-f]{6}$/i.test(v)) {
    hex = v.slice(1)
  }
  if (!hex) return null
  return [
    parseInt(hex.slice(0, 2), 16),
    parseInt(hex.slice(2, 4), 16),
    parseInt(hex.slice(4, 6), 16)
  ]
}

function luminance([r, g, b]) {
  const f = (c) => {
    const s = c / 255
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4)
  }
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}

function contrast(a, b) {
  const la = luminance(a)
  const lb = luminance(b)
  const hi = Math.max(la, lb)
  const lo = Math.min(la, lb)
  return (hi + 0.05) / (lo + 0.05)
}

const tokens = parseTokens(readFileSync(TOKEN_FILE, 'utf8'))
const bgRgb = BACKGROUNDS.map((name) => ({ name, rgb: toRgb(resolve(name, tokens)) }))
const fgList = [...TEXT_FG, ...EXEMPT_FG, ...NON_TEXT_FG]

const rows = []
for (const fg of fgList) {
  const rgb = toRgb(resolve(fg, tokens))
  if (!rgb) continue
  const exempt = EXEMPT_FG.includes(fg) || NON_TEXT_FG.includes(fg)
  const row = { token: fg, word: resolve(fg, tokens), values: {}, min: Infinity, exempt }
  for (const bg of bgRgb) {
    if (!bg.rgb) continue
    const ratio = Number(contrast(rgb, bg.rgb).toFixed(2))
    row.values[bg.name] = ratio
    if (!row.exempt) row.min = Math.min(row.min, ratio)
  }
  rows.push(row)
}

// 品牌底上的深色文字
const onBrand = toRgb(resolve('--text-on-brand', tokens))
const brandRgb = toRgb(resolve('--brand-primary', tokens))
const onBrandRatio = onBrand && brandRgb ? Number(contrast(onBrand, brandRgb).toFixed(2)) : null

const failures = rows.filter((r) => !r.exempt && r.min < 4.5)
const warns = rows.filter((r) => !r.exempt && r.min >= 4.5 && r.min < 7)

if (asJson) {
  console.log(JSON.stringify({
    generatedAt: new Date().toISOString(),
    tokenFile: 'src/styles/global.css',
    backgrounds: BACKGROUNDS,
    rows,
    textOnBrand: onBrandRatio,
    failures: failures.map((r) => ({ token: r.token, min: r.min })),
    warnings: warns.map((r) => ({ token: r.token, min: r.min }))
  }, null, 2))
} else {
  console.log('对比度矩阵（WCAG 2.1，越大越好；正文要求 >= 4.5:1，禁用态豁免）\n')
  const head = ['前景令牌', ...BACKGROUNDS.map((b) => b.replace('--bg-', '')), '最低']
  const table = rows.map((r) => [
    r.token,
    ...BACKGROUNDS.map((b) => (r.values[b] != null ? r.values[b].toFixed(2) : '-')),
    r.exempt ? '豁免' : r.min.toFixed(2)
  ])
  const widths = head.map((h, i) => Math.max(h.length, ...table.map((t) => String(t[i]).length)))
  const fmt = (cells) => cells.map((c, i) => String(c).padEnd(widths[i])).join('  ')
  console.log(fmt(head))
  console.log(widths.map((w) => '-'.repeat(w)).join('  '))
  for (const row of table) console.log(fmt(row))
  if (onBrandRatio !== null) console.log(`\n--text-on-brand 在 --brand-primary 上：${onBrandRatio.toFixed(2)}`)
  if (warns.length) console.log(`\n提示：${warns.map((w) => w.token).join('、')} 低于 7:1，可作次级信息但不宜承载长段落。`)
  console.log('')
}

if (failures.length) {
  console.error(`✗ ${failures.length} 个文本令牌低于 4.5:1：${failures.map((f) => `${f.token}=${f.min.toFixed(2)}`).join('，')}`)
  if (shouldFail) process.exit(1)
} else {
  console.log('✓ 全部文本令牌在四档背景上均 >= 4.5:1')
}
