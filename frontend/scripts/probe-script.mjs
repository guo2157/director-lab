#!/usr/bin/env node
/** 探测 script 页头部几何：为什么 flex-wrap 生效了还溢出。 */
import { chromium } from 'playwright-core'

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } })
const page = await ctx.newPage()
await page.goto('http://127.0.0.1/project/prj_257406918e63/script', {
  waitUntil: 'networkidle',
  timeout: 45000,
})
await page.waitForTimeout(800)

const r = await page.evaluate(() => {
  const dump = (el, label) => {
    if (!el) return { label, missing: true }
    const b = el.getBoundingClientRect()
    const cs = getComputedStyle(el)
    return {
      label,
      cls: el.className?.toString().slice(0, 50),
      left: Math.round(b.left),
      right: Math.round(b.right),
      w: Math.round(b.width),
      scrollW: el.scrollWidth,
      clientW: el.clientWidth,
      display: cs.display,
      flexWrap: cs.flexWrap,
      flexShrink: cs.flexShrink,
      minWidth: cs.minWidth,
      overflowX: cs.overflowX,
    }
  }
  const ha = document.querySelector('.header-actions')
  const sh = ha?.closest('.script-header') || ha?.parentElement
  const btns = [...(ha?.querySelectorAll('button') || [])].map((b) => {
    const bb = b.getBoundingClientRect()
    return {
      text: (b.innerText || '').trim().slice(0, 12),
      left: Math.round(bb.left),
      right: Math.round(bb.right),
      w: Math.round(bb.width),
      top: Math.round(bb.top),
    }
  })
  return {
    page: dump(document.querySelector('.script-page'), 'script-page'),
    header: dump(sh, 'script-header'),
    actions: dump(ha, 'header-actions'),
    btns,
    vw: window.innerWidth,
    mq768: window.matchMedia('(max-width: 768px)').matches,
  }
})
console.log(JSON.stringify(r, null, 2))
await browser.close()
