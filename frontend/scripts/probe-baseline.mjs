#!/usr/bin/env node
/**
 * 现状快照 + 几何探针：为「Pixmax 参考式第一轮优化」建立改前基线。
 *
 * 与 uiux-shots.mjs 的差别：
 *   · 只跑 1440x900 + 390x844 两档，快（一轮 ~30s），适合改造过程中反复跑
 *   · 额外量「首屏内容起点」「页顶横块数量」「每块高度」，这是本轮要收敛的对象
 *   · 输出 JSON 便于改前/改后 diff
 *
 * 用法：node scripts/probe-baseline.mjs [outDir] [tag]
 */
import { chromium } from 'playwright-core'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'

const BASE = 'http://127.0.0.1'
const OUT = process.argv[2] ?? 'docs/uiux/screenshots/baseline'
const TAG = process.argv[3] ?? 'before'

const PRJ = 'prj_257406918e63'

const PAGES = [
  { key: 'home', path: '/', ready: '.project-card' },
  { key: 'script', path: `/project/${PRJ}/script`, ready: '.script-page' },
  { key: 'parse', path: `/project/${PRJ}/parse`, ready: '.parse-page, .result-panel' },
  { key: 'assets', path: `/project/${PRJ}/assets`, ready: 'article' },
  { key: 'storyboard', path: `/project/${PRJ}/storyboard`, ready: '.shot-card' },
  { key: 'sb-images', path: `/project/${PRJ}/storyboard-images`, ready: '.gen-bar, .stage-card' },
  { key: 'compose', path: `/project/${PRJ}/compose`, ready: '.compose-page' },
  { key: 'settings', path: '/settings', ready: '.settings-page' },
]

const VIEWPORTS = [
  { w: 1440, h: 900, tag: '1440x900' },
  { w: 390, h: 844, tag: '390x844' },
]

const MEASURE = () => {
  const de = document.documentElement
  const vw = window.innerWidth

  /** 主内容区根：优先 .page / main，退回 body */
  const root = document.querySelector('.page, main, .app-main') ?? document.body
  const rootTop = root.getBoundingClientRect().top + window.scrollY

  /**
   * 页顶横块探测：主内容区内、位于首屏、宽度 > 60% 容器宽、
   * 高度 20–160px 且不是卡片网格的直属块。
   * 目的是把「页头 / 状态条 / 说明句 / 工具栏」这些堆叠物数出来。
   */
  const containerW = root.getBoundingClientRect().width
  const blocks = []
  const walk = (el, depth) => {
    if (depth > 3) return
    for (const child of el.children) {
      const r = child.getBoundingClientRect()
      const cs = getComputedStyle(child)
      if (r.height === 0 || cs.display === 'none') continue
      const isWide = r.width > containerW * 0.6
      const isBand = r.height >= 18 && r.height <= 200
      if (isWide && isBand && r.top < window.innerHeight) {
        blocks.push({
          cls: (child.className || '').toString().slice(0, 60),
          tag: child.tagName.toLowerCase(),
          top: Math.round(r.top),
          h: Math.round(r.height),
          text: (child.innerText || '').replace(/\s+/g, ' ').slice(0, 70),
        })
      }
      walk(child, depth + 1)
    }
  }
  walk(root, 0)

  /** 首屏内容起点：第一个「卡片 / 网格 / 编辑器」的 top */
  const CONTENT_SEL = [
    '.project-grid', '.asset-grid', '.shot-card', '.result-panel',
    '.script-editor', '.compose-page', '.stage-card', '.asset-tabs',
    '.gen-bar', '.settings-page',
  ]
  let contentTop = null
  let contentSel = null
  for (const sel of CONTENT_SEL) {
    const el = document.querySelector(sel)
    if (!el) continue
    const r = el.getBoundingClientRect()
    if (r.height === 0) continue
    const t = Math.round(r.top)
    if (contentTop === null || t < contentTop) { contentTop = t; contentSel = sel }
  }

  const bodyText = (document.body.innerText || '')
  const errHits = ['接口不存在', '加载失败', '请求失败', 'Network Error', 'Cannot read propert']
    .filter((s) => bodyText.includes(s))

  return {
    vw,
    vh: window.innerHeight,
    overflowX: de.scrollWidth > de.clientWidth + 1,
    scrollW: de.scrollWidth,
    clientW: de.clientWidth,
    rootTop: Math.round(rootTop),
    blocks,
    blockCount: blocks.length,
    blocksTotalH: blocks.reduce((a, b) => a + b.h, 0),
    contentTop,
    contentSel,
    docH: de.scrollHeight,
    textLen: bodyText.replace(/\s+/g, '').length,
    errHits,
  }
}

async function main() {
  await mkdir(OUT, { recursive: true })
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const report = []
  for (const vp of VIEWPORTS) {
    const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h }, deviceScaleFactor: 1 })
    const page = await ctx.newPage()
    await page.route('**/api/events*', (r) => r.abort())
    for (const p of PAGES) {
      try {
        await page.goto(BASE + p.path, { waitUntil: 'domcontentloaded', timeout: 45000 })
        await page.waitForSelector(p.ready, { timeout: 15000 }).catch(() => {})
        await page.waitForTimeout(900)
        const m = await page.evaluate(MEASURE)
        const file = path.join(OUT, `${TAG}__${p.key}__${vp.tag}.png`)
        await page.screenshot({ path: file, fullPage: false })
        report.push({ page: p.key, viewport: vp.tag, file, ...m })
        console.log(
          `${m.overflowX || m.errHits.length ? 'FAIL' : 'ok  '} ${p.key.padEnd(12)} ${vp.tag.padEnd(10)} ` +
            `blocks=${String(m.blockCount).padEnd(2)} bandH=${String(m.blocksTotalH).padEnd(4)} ` +
            `contentTop=${m.contentTop ?? '-'}(${m.contentSel ?? '-'}) docH=${m.docH}` +
            (m.errHits.length ? ` ERR[${m.errHits}]` : ''),
        )
        if (vp.w === 1440) {
          for (const b of m.blocks) console.log(`      · ${String(b.h).padStart(3)}px ${b.tag}.${b.cls}  «${b.text}»`)
        }
      } catch (e) {
        console.log(`ERR  ${p.key} ${vp.tag} ${String(e).slice(0, 100)}`)
        report.push({ page: p.key, viewport: vp.tag, error: String(e).slice(0, 200) })
      }
    }
    await ctx.close()
  }
  await browser.close()
  const jsonPath = path.join(OUT, `_report-${TAG}.json`)
  await writeFile(jsonPath, JSON.stringify(report, null, 2))
  console.log(`\n报告：${jsonPath}`)
}

main()
