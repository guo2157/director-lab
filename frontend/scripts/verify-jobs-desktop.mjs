#!/usr/bin/env node
/**
 * 桌面宽度下验证表格视图的失败任务操作列。
 * 修复前：isEnvError(=retryable) 二选一 → 只显示「重试」，看不到失败原因。
 * 修复后：retryable 的给「重试」+「查看原因」，不可重试的只给「查看原因」。
 */
import { chromium } from 'playwright-core'

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await ctx.newPage()
await page.route('**/api/events*', (r) => r.abort())
await page.goto('http://127.0.0.1/jobs', { waitUntil: 'domcontentloaded' })
await page.waitForSelector('.n-data-table', { timeout: 20000 })
await page.waitForTimeout(1500)

const r = await page.evaluate(() => {
  const rows = [...document.querySelectorAll('.n-data-table-tbody .n-data-table-tr')]
  const failed = rows.filter((tr) => tr.innerText.includes('失败') || tr.innerText.includes('丢失'))
  const sample = failed.slice(0, 5).map((tr) => {
    const act = tr.querySelector('.cell-actions')
    return {
      text: tr.innerText.replace(/\s+/g, ' ').slice(0, 60),
      buttons: act ? [...act.querySelectorAll('button')].map((b) => b.innerText.trim()) : 'no-cell-actions',
      hasActionsWrap: !!act,
    }
  })
  // 表格是否横向溢出
  const de = document.documentElement
  return {
    rowCount: rows.length,
    failedCount: failed.length,
    sample,
    overflowX: de.scrollWidth > de.clientWidth + 1,
    scrollW: de.scrollWidth,
    clientW: de.clientWidth,
  }
})
console.log(JSON.stringify(r, null, 2))
await page.screenshot({ path: '../docs/uiux/screenshots/round6/jobs__1440x900.png' })
await browser.close()
