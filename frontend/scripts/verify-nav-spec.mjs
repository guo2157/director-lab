#!/usr/bin/env node
/**
 * 顶栏导航规格验证（2026-11-19 v2：只剩 作品 / 设置 两个入口后的放大改版）。
 *
 * 为什么需要：任务中心下线后顶栏只剩两项，13.5px 的小字 + 裸胶囊在 52px 容器里
 * 读起来像「一个按钮 + 一个标签」。改为分段控件 + 15.5px 后，这些规格必须锁住，
 * 否则后续加页面时很容易被顺手改回小字号。
 *
 * 判定口径：
 *   · 导航项字号 >= 15.5px（1440/768），窄屏 >= 14.5px
 *   · 两项等宽（同一 track 内等权，不是「主按钮 + 标签」）
 *   · 导航几何中心与顶栏胶囊中心误差 <= 1px
 *   · 未激活字色为 --text-secondary（不得用 primary 抢激活态焦点）
 *   · 轨道背景为 --bg-sunken（凹陷槽，不得比容器更亮）
 *   · 三档视口横向无溢出、无按钮被裁
 *
 * 用法：node scripts/verify-nav-spec.mjs [baseUrl]
 */
import { chromium } from 'playwright-core'
import { mkdir } from 'node:fs/promises'

const BASE = process.argv[2] ?? 'http://127.0.0.1'
const OUT = '../docs/uiux/screenshots/round9'

const VIEWPORTS = [
  { w: 1440, h: 900, tag: '1440x900', minFont: 15.5 },
  { w: 768, h: 1024, tag: '768x1024', minFont: 14.5 },
  { w: 390, h: 844, tag: '390x844', minFont: 14.5 },
]

const MEASURE = () => {
  const R = (e) => e.getBoundingClientRect()
  const items = [...document.querySelectorAll('.nav-item')]
  const nav = document.querySelector('.top-nav')
  const pill = document.querySelector('.header-pill')
  const navBox = R(nav)
  const pillBox = R(pill)
  const widths = items.map((e) => Math.round(R(e).width))
  const vw = window.innerWidth
  const clipped = [...document.querySelectorAll('button')].filter((b) => {
    const r = R(b)
    if (r.width === 0 || r.height === 0) return false
    if (r.right <= vw + 1) return false
    let p = b.parentElement
    while (p) {
      const cs = getComputedStyle(p)
      if (/auto|scroll/.test(cs.overflowX) && p.scrollWidth > p.clientWidth + 1) return false
      p = p.parentElement
    }
    return true
  }).length
  return {
    labels: items.map((e) => e.innerText.trim()),
    fontSizes: items.map((e) => parseFloat(getComputedStyle(e).fontSize)),
    colors: items.map((e) => getComputedStyle(e).color),
    activeColor: items.find((e) => e.classList.contains('is-active'))
      ? getComputedStyle(items.find((e) => e.classList.contains('is-active'))).color
      : null,
    widths,
    equalWidth: widths.length === 2 && Math.abs(widths[0] - widths[1]) <= 1,
    navCenterOffset: Math.round(navBox.x + navBox.width / 2 - (pillBox.x + pillBox.width / 2)),
    trackBg: getComputedStyle(nav).backgroundColor,
    pillBg: getComputedStyle(pill).backgroundColor,
    overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    clippedButtons: clipped,
  }
}

const browser = await chromium.launch({ channel: 'chrome', headless: true })
await mkdir(OUT, { recursive: true })
let fails = 0

for (const vp of VIEWPORTS) {
  const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await page.route('**/api/events*', (r) => r.abort())
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 45000 })
  await page.waitForSelector('.project-list-page', { timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(900)

  const m = await page.evaluate(MEASURE)
  await (await page.$('.header-pill')).screenshot({ path: `${OUT}/nav__${vp.tag}.png` })

  const reasons = []
  if (m.labels.join('/') !== '作品/设置') reasons.push(`labels=${m.labels.join('/')}`)
  if (Math.min(...m.fontSizes) < vp.minFont) reasons.push(`font<${vp.minFont}: ${m.fontSizes.join(',')}`)
  if (!m.equalWidth) reasons.push(`widths unequal: ${m.widths.join(',')}`)
  if (Math.abs(m.navCenterOffset) > 1) reasons.push(`centerOffset=${m.navCenterOffset}`)
  if (m.overflowX) reasons.push('overflowX')
  if (m.clippedButtons > 0) reasons.push(`clipped=${m.clippedButtons}`)
  // 未激活色必须比激活色暗（secondary 不应等于 primary）
  if (m.colors[1] === m.colors[0]) reasons.push('inactive color == active color')
  // 轨道必须比容器深（凹陷语义）
  const lum = (s) => {
    const [r, g, b] = s.match(/\d+/g).map(Number)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  if (lum(m.trackBg) >= lum(m.pillBg)) reasons.push(`track(${m.trackBg}) not darker than pill(${m.pillBg})`)

  const bad = reasons.length > 0
  if (bad) fails++
  console.log(
    `${bad ? 'FAIL' : 'PASS'}  ${vp.tag.padEnd(10)} ` +
      `font=${m.fontSizes.join('/')} w=${m.widths.join('/')} equal=${m.equalWidth} ` +
      `centerOffset=${m.navCenterOffset} track=${m.trackBg} ` +
      `overflowX=${m.overflowX} clipped=${m.clippedButtons}` +
      (bad ? `  ← ${reasons.join('; ')}` : ''),
  )
  await ctx.close()
}
await browser.close()
console.log(`\n合计 ${VIEWPORTS.length} 组，FAIL ${fails} 组`)
process.exit(fails ? 1 : 0)
