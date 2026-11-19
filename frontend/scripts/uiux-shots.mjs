#!/usr/bin/env node
/**
 * 多视口截图 + 几何量测。
 *
 * 为什么需要：`docs/uiux/acceptance-report.md` 明确记录「1280/1024/768 未截图，
 * 属未完成项」。视觉验收不能只跑 1920 和 390 两端，中间三档才是真实办公场景。
 *
 * 判定口径（与 audit-report.md 一致）：
 *   · 页面横向溢出   documentElement.scrollWidth > clientWidth  → 失败
 *   · 按钮被裁       button 右边界 > 视口宽                       → 失败
 *   · 卡片留白       card.height - thumb.height - info.height > 8 → 警告
 *
 * 用法：node scripts/uiux-shots.mjs [baseUrl] [outDir]
 */
import { chromium } from 'playwright-core'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'

const BASE = process.argv[2] ?? 'http://127.0.0.1'
const OUT = process.argv[3] ?? 'docs/uiux/screenshots/round5'

/**
 * 用系统 Chrome 而不是 playwright 自带的 chromium：
 * 本机 npm 直连被墙（registry.npmmirror.com 才通），且 playwright 的浏览器下载
 * 需要额外 ~150MB。系统已有 /usr/bin/google-chrome，playwright-core 只做协议驱动，
 * 通过 channel: 'chrome' 直接复用，零下载。
 */

const PAGES = [
  { key: 'home', path: '/', ready: '.project-card' },
  { key: 'parse', path: '/project/prj_257406918e63/parse', ready: '.result-panel' },
  { key: 'assets', path: '/project/prj_257406918e63/assets', ready: 'article' },
  { key: 'storyboard', path: '/project/prj_257406918e63/storyboard', ready: '.shot-card' },
]

const VIEWPORTS = [
  { w: 1920, h: 1080, tag: '1920x1080' },
  { w: 1440, h: 900, tag: '1440x900' },
  { w: 1366, h: 768, tag: '1366x768' },
  { w: 768, h: 1024, tag: '768x1024' },
  { w: 390, h: 844, tag: '390x844' },
]

const MEASURE = () => {
  const de = document.documentElement
  const vw = window.innerWidth
  const overflowX = de.scrollWidth > de.clientWidth + 1
  const clippedButtons = [...document.querySelectorAll('button')].filter((b) => {
    const r = b.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) return false
    return r.right > vw + 1
  }).length
  const card = document.querySelector('.shot-card')
  let cardSlack = null
  if (card) {
    const thumb = card.querySelector('.shot-thumb')
    const info = card.querySelector('.shot-info')
    if (thumb && info) {
      cardSlack = Math.round(
        card.getBoundingClientRect().height -
          thumb.getBoundingClientRect().height -
          info.getBoundingClientRect().height,
      )
    }
  }
  const coverImgs = [...document.querySelectorAll('.cover-img')]
    .filter((i) => i.offsetParent !== null).length
  const placeholders = document.querySelectorAll('.cover-char').length
  const gridCols = (() => {
    const g = document.querySelector('.project-grid')
    if (!g) return null
    return getComputedStyle(g).gridTemplateColumns.split(' ').length
  })()
  return {
    vw,
    vh: window.innerHeight,
    scrollW: de.scrollWidth,
    clientW: de.clientWidth,
    overflowX,
    clippedButtons,
    cardSlack,
    coverImgs,
    placeholders,
    gridCols,
  }
}

async function main() {
  await mkdir(OUT, { recursive: true })
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const report = []

  for (const vp of VIEWPORTS) {
    const ctx = await browser.newContext({
      viewport: { width: vp.w, height: vp.h },
      deviceScaleFactor: 1,
    })
    const page = await ctx.newPage()
    for (const p of PAGES) {
      const url = BASE + p.path
      try {
        await page.goto(url, { waitUntil: 'networkidle', timeout: 45000 })
        await page.waitForSelector(p.ready, { timeout: 15000 }).catch(() => {})
        // 等封面图解码完成，避免截到空白
        await page.waitForTimeout(700)
        const m = await page.evaluate(MEASURE)
        const file = path.join(OUT, `${p.key}__${vp.tag}.png`)
        await page.screenshot({ path: file, fullPage: false })
        report.push({ page: p.key, viewport: vp.tag, file, ...m })
        const flag =
          m.overflowX || m.clippedButtons > 0 ? 'FAIL' : m.cardSlack > 8 ? 'WARN' : 'PASS'
        console.log(
          `${flag}  ${p.key.padEnd(11)} ${vp.tag.padEnd(10)} ` +
            `scrollW=${m.scrollW}/${m.clientW} clipped=${m.clippedButtons} ` +
            `slack=${m.cardSlack ?? '-'} covers=${m.coverImgs} cols=${m.gridCols ?? '-'}`,
        )
      } catch (e) {
        console.log(`ERR   ${p.key.padEnd(11)} ${vp.tag.padEnd(10)} ${String(e).slice(0, 90)}`)
        report.push({ page: p.key, viewport: vp.tag, error: String(e).slice(0, 200) })
      }
    }
    await ctx.close()
  }

  await browser.close()
  await writeFile(path.join(OUT, '_measure.json'), JSON.stringify(report, null, 2))
  const fails = report.filter((r) => r.overflowX || (r.clippedButtons ?? 0) > 0)
  console.log(`\n合计 ${report.length} 张，FAIL ${fails.length} 张`)
  process.exit(fails.length ? 1 : 0)
}

main()
