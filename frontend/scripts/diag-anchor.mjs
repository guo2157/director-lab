#!/usr/bin/env node
/** 诊断：每页首屏锚点上方到底堆了什么，用来给预算值找依据 */
import { chromium } from 'playwright-core'

const BASE = process.argv[2] ?? 'http://127.0.0.1'
const PROJECT = process.argv[3] ?? 'prj_257406918e63'

const PAGES = [
  { key: 'script', path: 'script', anchor: '.script-body' },
  { key: 'assets', path: 'assets', anchor: '.assets-tabs' },
  { key: 'storyboard-images', path: 'storyboard-images', anchor: '.gen-strip' },
  { key: 'storyboard', path: 'storyboard', anchor: '.episode-bar' },
  { key: 'compose', path: 'compose', anchor: '.compose-grid' },
]

const DIAG = ({ anchorSel }) => {
  const R = (e) => (e ? e.getBoundingClientRect() : null)
  const anchor = document.querySelector(anchorSel)
  const anchorTop = anchor ? Math.round(R(anchor).top) : null

  // 收集锚点上方所有可见块级元素（只保留最外层，避免父子重复计数）
  const above = []
  const walk = (root) => {
    for (const el of root.children) {
      const r = R(el)
      if (!r || r.height < 1) continue
      if (r.top >= anchorTop) continue
      const cs = getComputedStyle(el)
      if (cs.display === 'none' || cs.visibility === 'hidden') continue
      above.push({
        tag: el.tagName.toLowerCase(),
        cls: (el.className || '').toString().split(/\s+/).filter(Boolean).slice(0, 3).join('.'),
        top: Math.round(r.top),
        h: Math.round(r.height),
        text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40),
      })
      // 只下钻高度较大的容器，避免把每个 span 都列出来
      if (r.height > 60 && el.children.length && r.height < anchorTop) walk(el)
    }
  }
  const main = document.querySelector('.content-main') || document.body
  walk(main)

  // 页头元素自身高度
  const headerSel = ['.page-header', '.script-header', '.compose-bar']
  const header = headerSel.map((s) => document.querySelector(s)).find(Boolean)

  return {
    anchorTop,
    headerH: header ? Math.round(R(header).height) : null,
    headerSel: header ? header.className.toString() : null,
    above: above.slice(0, 14),
  }
}

const browser = await chromium.launch({ channel: 'chrome', headless: true })
for (const vp of [{ w: 1440, h: 900 }, { w: 390, h: 844 }]) {
  const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h } })
  const page = await ctx.newPage()
  await page.route('**/api/events*', (r) => r.abort())
  for (const p of PAGES) {
    await page.goto(`${BASE}/project/${PROJECT}/${p.path}`, { waitUntil: 'domcontentloaded', timeout: 45000 })
    await page.waitForSelector(p.anchor, { timeout: 15000 }).catch(() => {})
    await page.waitForTimeout(700)
    const d = await page.evaluate(DIAG, { anchorSel: p.anchor })
    console.log(`\n=== ${p.key} @ ${vp.w} === anchorTop=${d.anchorTop} headerH=${d.headerH} [${d.headerSel}]`)
    for (const a of d.above) console.log(`   +${String(a.top).padStart(3)} h=${String(a.h).padStart(3)}  ${a.tag}.${a.cls}  "${a.text}"`)
  }
  await ctx.close()
}
await browser.close()
