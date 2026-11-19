#!/usr/bin/env node
/**
 * 量测 390px 下 jobs 页各区块的纵向占用，找出把任务列表挤出首屏的元凶。
 * 用 domcontentloaded 而不是 networkidle（该页有 SSE + 5s 轮询，永远不 idle）。
 */
import { chromium } from 'playwright-core'

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } })
const page = await ctx.newPage()

await page.goto('http://127.0.0.1/jobs', { waitUntil: 'domcontentloaded', timeout: 45000 })
await page.waitForSelector('.job-card', { timeout: 15000 }).catch(() => {})
await page.waitForTimeout(1200)

const r = await page.evaluate(() => {
  const vh = window.innerHeight
  const blocks = [
    ['header', '.jobs-head'],
    ['stats', '.stats-row'],
    ['filters', '.wf-toolbar'],
    ['card容器', '.panel-card'],
    ['首张任务卡', '.job-card'],
  ].map(([label, sel]) => {
    const n = document.querySelector(sel)
    if (!n) return { label, missing: true }
    const b = n.getBoundingClientRect()
    return {
      label,
      top: Math.round(b.top),
      h: Math.round(b.height),
      bottom: Math.round(b.bottom),
      inFirstScreen: b.top < vh,
      fullyVisible: b.bottom <= vh,
    }
  })
  // 筛选按钮数量与行高
  const tabs = [...document.querySelectorAll('.status-tab')].map((t) => {
    const b = t.getBoundingClientRect()
    return { text: t.innerText.trim(), top: Math.round(b.top), h: Math.round(b.height) }
  })
  const rows = [...new Set(tabs.map((t) => t.top))].length
  return { vh, blocks, filterRows: rows, tabCount: tabs.length, docH: document.documentElement.scrollHeight }
})

console.log(JSON.stringify(r, null, 2))
await page.screenshot({ path: '../docs/uiux/screenshots/round6/_diag_jobs390.png' })
await browser.close()
