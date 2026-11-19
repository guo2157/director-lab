#!/usr/bin/env node
/**
 * 排查「DOM 有文本但截图空白」：量测 .job-card-error 的几何、颜色、是否被遮挡。
 * 注意：DOM 里 innerText 有值 ≠ 用户能看见（可能是 height:0、被 clip、色值等于背景）。
 */
import { chromium } from 'playwright-core'

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } })
const page = await ctx.newPage()

/**
 * 屏蔽 SSE：后端 events.py 的生成器在 yield 期间一直持有 DB 连接，
 * 连接池只有 15 个，多开几个页面就会把池占满、全站请求排队卡死。
 * 视觉验证不需要实时推送，直接拦掉 /api/events。
 */
await page.route('**/api/events*', (route) => route.abort())

await page.goto('http://127.0.0.1/jobs', { waitUntil: 'domcontentloaded' })
await page.waitForSelector('.job-card', { timeout: 20000 })
await page.waitForTimeout(1500)

// 展开第一张失败卡的「查看原因」
const btn = await page.$('.job-card button:has-text("查看原因")')
if (!btn) { console.log('NO 查看原因 button'); }
else {
  await btn.scrollIntoViewIfNeeded()
  await btn.click()
  await page.waitForTimeout(500)
}

const r = await page.evaluate(() => {
  const errs = [...document.querySelectorAll('.job-card-error')]
  return errs.map((e) => {
    const b = e.getBoundingClientRect()
    const cs = getComputedStyle(e)
    return {
      cls: e.className,
      text: e.innerText.slice(0, 60),
      textLen: e.innerText.length,
      top: Math.round(b.top),
      h: Math.round(b.height),
      w: Math.round(b.width),
      color: cs.color,
      bg: cs.backgroundColor,
      fontSize: cs.fontSize,
      lineHeight: cs.lineHeight,
      display: cs.display,
      visibility: cs.visibility,
      opacity: cs.opacity,
      overflow: cs.overflow,
      // 是否在视口内
      inViewport: b.top >= 0 && b.top < window.innerHeight,
      // 是否有元素盖住它（取中心点命中测试）
      hit: (() => {
        if (b.width === 0 || b.height === 0) return null
        const el = document.elementFromPoint(b.left + b.width / 2, b.top + Math.min(b.height / 2, 20))
        return el ? el.className?.toString().slice(0, 40) || el.tagName : null
      })(),
    }
  })
})
console.log(JSON.stringify(r, null, 2))

// 把第一张失败卡滚到视口中间再截（之前截首屏，错误块在 top 3597 视口外，
// 所以视觉复核看到的是「空白」——实际是没截到，不是没渲染）
const card = await page.$('.job-card-error')
if (card) {
  await card.scrollIntoViewIfNeeded()
  await page.waitForTimeout(500)
  const vis = await page.evaluate(() => {
    const e = document.querySelector('.job-card-error')
    const b = e.getBoundingClientRect()
    return { top: Math.round(b.top), h: Math.round(b.height), inViewport: b.top >= 0 && b.bottom <= window.innerHeight }
  })
  console.log('after scroll:', JSON.stringify(vis))
  await page.screenshot({ path: '../docs/uiux/screenshots/round6/_verify_error_inline.png' })
}
await browser.close()
