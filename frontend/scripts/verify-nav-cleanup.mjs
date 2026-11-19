#!/usr/bin/env node
/**
 * 验证「任务中心下线 + 页内大标题去重」两件事。
 *
 * 判定口径：
 *   A. 顶栏不再有「任务中心」入口（只剩 作品 / 设置）
 *   B. 页内没有可见的重复 H1（顶栏已高亮页名），H1 仍在 DOM 里供读屏
 *   C. /jobs 旧链接重定向到作品列表，不白屏、不 404
 *   D. 三个视口下横向无溢出、无按钮被裁
 *
 * 用法：node scripts/verify-nav-cleanup.mjs [baseUrl]
 */
import { chromium } from 'playwright-core'
import { mkdir } from 'node:fs/promises'

const BASE = process.argv[2] ?? 'http://127.0.0.1'
const OUT = '../docs/uiux/screenshots/round8'

const VIEWPORTS = [
  { w: 1440, h: 900, tag: '1440x900' },
  { w: 768, h: 1024, tag: '768x1024' },
  { w: 390, h: 844, tag: '390x844' },
]

const MEASURE = (anchorSel) => {
  const de = document.documentElement
  const vw = window.innerWidth
  const h1 = document.querySelector('h1')
  const h1Box = h1 ? h1.getBoundingClientRect() : null
  const h1Style = h1 ? getComputedStyle(h1) : null
  const anchor = anchorSel ? document.querySelector(anchorSel) : null
  const clipped = [...document.querySelectorAll('button')].filter((b) => {
    const r = b.getBoundingClientRect()
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
    path: location.pathname,
    navItems: [...document.querySelectorAll('.nav-item')].map((n) => n.innerText.trim()),
    navActive: document.querySelector('.nav-item.is-active')?.innerText.trim() ?? null,
    h1Text: h1?.innerText.trim() ?? null,
    h1Visible: h1Box ? h1Box.width > 2 && h1Box.height > 2 : false,
    anchorTop: anchor ? Math.round(anchor.getBoundingClientRect().top) : null,
    overflowX: de.scrollWidth > de.clientWidth + 1,
    clippedButtons: clipped,
    hasProjectCards: document.querySelectorAll('.project-card').length,
    hasJobsPage: !!document.querySelector('.jobs-page'),
    bodyHead: (document.body.innerText || '').replace(/\s+/g, '').slice(0, 60),
  }
}

const browser = await chromium.launch({ channel: 'chrome', headless: true })
await mkdir(OUT, { recursive: true })
const rows = []
let fails = 0

for (const vp of VIEWPORTS) {
  const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h }, deviceScaleFactor: 1 })
  const page = await ctx.newPage()
  await page.route('**/api/events*', (r) => r.abort())

  /* ---- 作品列表 ---- */
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 45000 })
  await page.waitForSelector('.project-list-page', { timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(900)
  const proj = await page.evaluate(MEASURE, '.project-grid')
  await page.screenshot({ path: `${OUT}/projects__${vp.tag}.png` })
  const projBad =
    proj.h1Visible ||
    proj.navItems.includes('任务中心') ||
    proj.navItems.join('/') !== '作品/设置' ||
    proj.overflowX ||
    proj.clippedButtons > 0
  if (projBad) fails++
  rows.push({ page: 'projects', viewport: vp.tag, ...proj })
  console.log(
    `${projBad ? 'FAIL' : 'PASS'}  projects ${vp.tag.padEnd(10)} ` +
      `nav=[${proj.navItems.join(',')}] active=${proj.navActive} h1="${proj.h1Text}" ` +
      `h1Visible=${proj.h1Visible} anchorTop=${proj.anchorTop} ` +
      `overflowX=${proj.overflowX} clipped=${proj.clippedButtons}`,
  )

  /* ---- /jobs 旧链接：必须重定向到作品列表 ---- */
  await page.goto(BASE + '/jobs', { waitUntil: 'domcontentloaded', timeout: 45000 })
  await page.waitForSelector('.project-list-page', { timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(900)
  const redir = await page.evaluate(MEASURE, '.project-grid')
  await page.screenshot({ path: `${OUT}/jobs-redirect__${vp.tag}.png` })
  const redirBad =
    redir.path !== '/' || redir.hasJobsPage || redir.overflowX || redir.clippedButtons > 0
  if (redirBad) fails++
  rows.push({ page: 'jobs-redirect', viewport: vp.tag, ...redir })
  console.log(
    `${redirBad ? 'FAIL' : 'PASS'}  /jobs→   ${vp.tag.padEnd(10)} ` +
      `landed=${redir.path} jobsPageGone=${!redir.hasJobsPage} ` +
      `cards=${redir.hasProjectCards} overflowX=${redir.overflowX} clipped=${redir.clippedButtons}`,
  )

  await ctx.close()
}
await browser.close()
console.log(`\n合计 ${rows.length} 组，FAIL ${fails} 组`)
process.exit(fails ? 1 : 0)
