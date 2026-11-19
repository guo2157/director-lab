#!/usr/bin/env node
/**
 * 作品列表单行工具栏规格验证（2026-11-19 改版）。
 *
 * 背景：页头三行（「共 N 个作品」+ 搜索/新建、筛选 chips、排序分段）压成一行。
 * 删除项：「共 N 个作品」（与 chip 的「全部 N」重复）、排序轴（只有两个选项且
 * 默认就是最近更新，固定倒序后功能等价）。
 *
 * 判定口径：
 *   A. 桌面（>=1100）：筛选 chips / 搜索框 / 新建按钮 三者同一行且中心线对齐
 *   B. 窄屏（<=768）：chips 独占一行、搜索+新建第二行，工具栏 <= 2 行高
 *   C. 排序控件已不存在（.sort-group / .sort-btn 计数为 0）
 *   D. 「共 N 个作品」文案已不存在
 *   E. 新建按钮图标与文字间距 >= 4px（Naive 的 .n-button__content 包裹坑）
 *   F. 无横向溢出、无按钮被裁
 *
 * 用法：node scripts/verify-toolbar.mjs [baseUrl]
 */
import { chromium } from 'playwright-core'
import { mkdir } from 'node:fs/promises'

const BASE = process.argv[2] ?? 'http://127.0.0.1'
const OUT = '../docs/uiux/screenshots/round10'

const VIEWPORTS = [
  { w: 1440, h: 900, tag: '1440x900', singleRow: true },
  { w: 1100, h: 900, tag: '1100x900', singleRow: true },
  { w: 768, h: 1024, tag: '768x1024', singleRow: false },
  { w: 390, h: 844, tag: '390x844', singleRow: false },
]

const MEASURE = () => {
  const R = (e) => e.getBoundingClientRect()
  const vw = window.innerWidth
  const chips = [...document.querySelectorAll('.filter-chip')]
  const input = document.querySelector('.search-input .n-input') || document.querySelector('.search-input')
  const btn = document.querySelector('.new-btn')
  const toolbar = document.querySelector('.list-toolbar')
  const cy = (e) => (e ? Math.round(R(e).y + R(e).height / 2) : null)

  const icon = btn?.querySelector('.n-icon')
  const text = btn?.querySelector('.new-text')

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
    chipRows: [...new Set(chips.map((c) => Math.round(R(c).y)))].length,
    chipCY: cy(chips[0]),
    inputCY: cy(input),
    btnCY: cy(btn),
    toolbarH: toolbar ? Math.round(R(toolbar).height) : null,
    toolbarW: toolbar ? Math.round(R(toolbar).width) : null,
    sortControls: document.querySelectorAll('.sort-group, .sort-btn').length,
    hasCountText: /共\s*\d+\s*个作品/.test(document.body.innerText || ''),
    iconGap: icon && text ? Math.round(R(text).x - R(icon).right) : null,
    hasNewBtn: !!btn,
    overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    clippedButtons: clipped,
    chipCount: chips.length,
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
  await page.waitForSelector('.project-card', { timeout: 20000 }).catch(() => {})
  await page.waitForTimeout(900)

  const m = await page.evaluate(MEASURE)
  await (await page.$('.list-toolbar')).screenshot({ path: `${OUT}/toolbar__${vp.tag}.png` })

  const reasons = []
  if (m.chipCount !== 4) reasons.push(`chips=${m.chipCount}`)
  if (m.sortControls !== 0) reasons.push(`sort controls still present: ${m.sortControls}`)
  if (m.hasCountText) reasons.push('「共 N 个作品」still rendered')
  if (!m.hasNewBtn) reasons.push('new button missing')
  if (m.overflowX) reasons.push('overflowX')
  if (m.clippedButtons > 0) reasons.push(`clipped=${m.clippedButtons}`)

  if (vp.singleRow) {
    // 桌面：三者中心线必须一致（±2px 容忍 Naive 的 34/36 高度差）
    const cys = [m.chipCY, m.inputCY, m.btnCY].filter((v) => v != null)
    if (Math.max(...cys) - Math.min(...cys) > 2) reasons.push(`centers misaligned: ${cys.join('/')}`)
    if (m.toolbarH > 44) reasons.push(`toolbarH=${m.toolbarH} (should be single row)`)
  } else {
    // 窄屏：chips 一行 + 搜索/新建一行，总高 <= 2 行
    if (m.toolbarH > 90) reasons.push(`toolbarH=${m.toolbarH} (>2 rows on narrow)`)
  }
  // 图标与文字间距（Naive .n-button__content 包裹坑）
  if (m.iconGap != null && m.iconGap < 4) reasons.push(`icon gap=${m.iconGap}px`)

  const bad = reasons.length > 0
  if (bad) fails++
  console.log(
    `${bad ? 'FAIL' : 'PASS'}  ${vp.tag.padEnd(10)} ` +
      `chips=${m.chipCount} rows=${m.chipRows} h=${m.toolbarH} ` +
      `cy=${m.chipCY}/${m.inputCY}/${m.btnCY} iconGap=${m.iconGap} ` +
      `sort=${m.sortControls} countText=${m.hasCountText} ` +
      `overflowX=${m.overflowX} clipped=${m.clippedButtons}` +
      (bad ? `  ← ${reasons.join('; ')}` : ''),
  )
  await ctx.close()
}
await browser.close()
console.log(`\n合计 ${VIEWPORTS.length} 组，FAIL ${fails} 组`)
process.exit(fails ? 1 : 0)
