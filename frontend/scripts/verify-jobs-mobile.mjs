#!/usr/bin/env node
/**
 * 390px 下功能验证：
 *  1. 卡片视图确实生效（不是表格）
 *  2. 「查看原因」能就地展开错误原文
 *  3. 筛选轴能横向滚动到末尾
 *  4. 分页器存在且可点
 */
import { chromium } from 'playwright-core'

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } })
const page = await ctx.newPage()
await page.goto('http://127.0.0.1/jobs', { waitUntil: 'domcontentloaded' })
await page.waitForSelector('.job-card', { timeout: 15000 })
await page.waitForTimeout(1200)

const r1 = await page.evaluate(() => ({
  hasTable: !!document.querySelector('.n-data-table'),
  hasCards: !!document.querySelector('.job-cards'),
  cardCount: document.querySelectorAll('.job-card').length,
  hasPager: !!document.querySelector('.card-pager'),
}))

// 找「查看原因」按钮
const reasonBtn = await page.$('.job-card-actions button:has-text("查看原因")')
let expandResult = 'no-button'
if (reasonBtn) {
  await reasonBtn.click()
  await page.waitForTimeout(300)
  expandResult = await page.evaluate(() => {
    const err = document.querySelector('.job-card-error')
    if (!err) return 'not-expanded'
    return { text: err.innerText.slice(0, 80), visible: err.getBoundingClientRect().height > 0 }
  })
  await page.screenshot({ path: '../docs/uiux/screenshots/round6/_fix_jobs-error-expanded.png' })
}

// 筛选轴滚到末尾
const scrollResult = await page.evaluate(() => {
  const t = document.querySelector('.type-tabs')
  if (!t) return 'no-tabs'
  const before = t.scrollLeft
  t.scrollLeft = t.scrollWidth
  return { before, after: t.scrollLeft, max: t.scrollWidth - t.clientWidth }
})

console.log(JSON.stringify({ ...r1, expandResult, scrollResult }, null, 2))
await page.screenshot({ path: '../docs/uiux/screenshots/round6/_fix_jobs390-top.png' })
await browser.close()
