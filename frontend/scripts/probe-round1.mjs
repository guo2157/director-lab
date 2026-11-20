#!/usr/bin/env node
/** 针对本轮 6 个具体缺陷的精确几何量测。改前 / 改后各跑一次。 */
import { chromium } from 'playwright-core'

const BASE = 'http://127.0.0.1'
const PRJ = 'prj_257406918e63'
const TAG = process.argv[2] ?? 'before'

const probe = async (page, name, fn, arg) => {
  try {
    const r = await page.evaluate(fn, arg)
    console.log(`\n[${name}]`)
    console.log(JSON.stringify(r, null, 1))
  } catch (e) {
    console.log(`\n[${name}] ERR ${String(e).slice(0, 120)}`)
  }
}

const main = async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await ctx.newPage()
  await page.route('**/api/events*', (r) => r.abort())

  /* ---------- 1. 作品列表 ---------- */
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.project-card', { timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(900)
  await probe(page, `${TAG} home: 卡片高度 / 封面高度 / 比例 / 新建入口数`, () => {
    const cards = [...document.querySelectorAll('.project-card')]
    const rows = new Map()
    cards.forEach((c) => {
      const t = Math.round(c.getBoundingClientRect().top)
      rows.set(t, (rows.get(t) ?? 0) + 1)
    })
    return {
      cardCount: cards.length,
      cardHeights: cards.map((c) => Math.round(c.getBoundingClientRect().height)),
      cardWidths: cards.map((c) => Math.round(c.getBoundingClientRect().width)),
      coverHeights: cards.map((c) => {
        const cov = c.querySelector('.cover')
        return cov ? Math.round(cov.getBoundingClientRect().height) : null
      }),
      coverRatios: cards.map((c) => {
        const cov = c.querySelector('.cover')
        if (!cov) return null
        const r = cov.getBoundingClientRect()
        return +(r.width / r.height).toFixed(2)
      }),
      gridCols: (() => {
        const g = document.querySelector('.project-grid')
        return g ? getComputedStyle(g).gridTemplateColumns.split(' ').length : null
      })(),
      createEntries: {
        headerBtn: document.querySelectorAll('.new-btn').length,
        tailStrip: document.querySelectorAll('.launch-strip').length,
      },
      gridBottom: (() => {
        const g = document.querySelector('.project-grid')
        return g ? Math.round(g.getBoundingClientRect().bottom) : null
      })(),
      docH: document.documentElement.scrollHeight,
    }
  })

  /* ---------- 2. 剧本页 ---------- */
  await page.goto(`${BASE}/project/${PRJ}/script`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.script-page', { timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(1200)
  await probe(page, `${TAG} script: 页顶横带清单 / 编辑器可视高度`, () => {
    const page_ = document.querySelector('.script-page')
    const bands = []
    if (page_) {
      for (const el of page_.children) {
        const r = el.getBoundingClientRect()
        if (r.height === 0) continue
        bands.push({
          cls: (el.className || '').toString().slice(0, 40),
          h: Math.round(r.height),
          text: (el.innerText || '').replace(/\s+/g, ' ').slice(0, 60),
        })
      }
    }
    const editor = document.querySelector('.editor-pane textarea, textarea')
    const reader = document.querySelector('.reader')
    return {
      bands,
      bandsTotalH: bands.reduce((a, b) => a + b.h, 0),
      editorH: editor ? Math.round(editor.getBoundingClientRect().height) : null,
      readerH: reader ? Math.round(reader.getBoundingClientRect().height) : null,
      stageHint: document.querySelectorAll('.stage-hint').length,
      h1: (document.querySelector('h1')?.textContent ?? '').trim(),
      h1Visible: (() => {
        const h = document.querySelector('h1')
        if (!h) return null
        const r = h.getBoundingClientRect()
        return r.width > 2 || r.height > 2
      })(),
    }
  })

  /* ---------- 3. 解析页 ---------- */
  await page.goto(`${BASE}/project/${PRJ}/parse`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.parse-page', { timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(1200)
  await probe(page, `${TAG} parse: 完成态文案出现次数 / 首屏结果起点`, () => {
    const txt = document.body.innerText
    const count = (s) => (txt.match(new RegExp(s, 'g')) || []).length
    return {
      headerH: (() => {
        const h = document.querySelector('.parse-header')
        return h ? Math.round(h.getBoundingClientRect().height) : null
      })(),
      bands: [...document.querySelectorAll('.parse-body > *')].map((el) => ({
        cls: (el.className || '').toString().slice(0, 36),
        h: Math.round(el.getBoundingClientRect().height),
      })),
      '“1 集已完成”': count('1 集已完成'),
      '“1/1 集完成”': count('1/1 集完成'),
      '“已完成 1”': count('已完成 1'),
      '“解析完成”': count('解析完成'),
      resultTop: (() => {
        const el = document.querySelector('.result-panel')
        return el ? Math.round(el.getBoundingClientRect().top) : null
      })(),
      legendItems: document.querySelectorAll('.legend-item').length,
      statusPanelH: (() => {
        const el = document.querySelector('.parse-status-panel')
        return el ? Math.round(el.getBoundingClientRect().height) : 0
      })(),
      legendText: [...document.querySelectorAll('.legend-item')].map((e) =>
        (e.textContent || '').replace(/\s+/g, ' ').trim(),
      ),
    }
  })

  /* ---------- 4. 资产库 / 分镜图 网格 ---------- */
  for (const [key, path, gridSel] of [
    ['assets', `/project/${PRJ}/assets`, '.card-grid'],
    ['sb-images', `/project/${PRJ}/storyboard-images`, '.shots-grid'],
  ]) {
    await page.goto(BASE + path, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1500)
    await probe(page, `${TAG} ${key}: 网格列数 / 卡片宽度 / 右侧留白`, (sel) => {
      const g = document.querySelector(sel)
      if (!g) return { grid: null }
      const gr = g.getBoundingClientRect()
      const cards = [...g.children].filter((c) => c.getBoundingClientRect().height > 0)
      const cardRights = cards.map((c) => Math.round(c.getBoundingClientRect().right))
      const cs = getComputedStyle(g)
      return {
        cols: cs.gridTemplateColumns,
        colCount: cs.gridTemplateColumns.split(' ').length,
        gridW: Math.round(gr.width),
        cardCount: cards.length,
        cardW: cards.length ? Math.round(cards[0].getBoundingClientRect().width) : null,
        maxCardRight: cardRights.length ? Math.max(...cardRights) : null,
        rightGap: cardRights.length ? Math.round(gr.right - Math.max(...cardRights)) : null,
      }
    }, gridSel)
  }

  await browser.close()
}
main()
