#!/usr/bin/env node
/**
 * 定位 390px 下被裁按钮：打印按钮文本、右边界、以及它最近的可滚动祖先。
 * 为什么单独写：uiux-shots.mjs 只报数量，改之前必须知道是谁、为什么被裁。
 */
import { chromium } from 'playwright-core'

const BASE = 'http://127.0.0.1'
const PAGES = [
  { key: 'script', path: '/project/prj_257406918e63/script' },
  { key: 'settings', path: '/settings' },
]

const PROBE = () => {
  const vw = window.innerWidth
  const out = []
  for (const b of document.querySelectorAll('button')) {
    const r = b.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) continue
    if (r.right <= vw + 1) continue
    // 向上找最近的可横向滚动祖先
    let p = b.parentElement
    let scroller = null
    while (p) {
      const cs = getComputedStyle(p)
      if (/auto|scroll/.test(cs.overflowX)) {
        scroller = {
          cls: p.className?.toString().slice(0, 60),
          scrollW: p.scrollWidth,
          clientW: p.clientWidth,
        }
        break
      }
      p = p.parentElement
    }
    out.push({
      text: (b.innerText || b.getAttribute('aria-label') || '').trim().slice(0, 24),
      cls: b.className?.toString().slice(0, 50),
      right: Math.round(r.right),
      width: Math.round(r.width),
      over: Math.round(r.right - vw),
      scroller,
      parentCls: b.parentElement?.className?.toString().slice(0, 50),
    })
  }
  return { vw, clipped: out }
}

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } })
const page = await ctx.newPage()
for (const p of PAGES) {
  await page.goto(BASE + p.path, { waitUntil: 'networkidle', timeout: 45000 })
  await page.waitForTimeout(800)
  const r = await page.evaluate(PROBE)
  console.log(`\n===== ${p.key} (vw=${r.vw}) clipped=${r.clipped.length} =====`)
  for (const c of r.clipped) console.log(JSON.stringify(c))
}
await browser.close()
