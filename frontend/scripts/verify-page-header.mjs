#!/usr/bin/env node
/**
 * 项目内页顶部结构验证（2026-11-19 v7）。
 *
 * 演进过程：
 *   v4/v6 —— 页内不重复导航已表达的标题，H1 降为 .visually-hidden；
 *   v7   —— 顶部从「多个独立横幅堆叠」收敛为两张 StageCard：
 *           卡一「执行进度」只读（状态/数字/进度条/刷新/去下一步），
 *           卡二「批量操作」可写（工作流/全选/批量生成）。
 *
 * 判定口径（每视口 × 每页）：
 *   A. 页内 H1 不可见但仍在 DOM 且文本正确 —— 读屏与大纲不能丢
 *   B. 左侧流程栏高亮的步骤名 == 该页声明的 railLabel
 *   C. 页头区域内没有可见的 .page-title / .detail-title
 *   D. 顶部恰好 2 张 .stage-card，标签依次为「执行进度」+ 批量卡名
 *   E. 首屏内容起点 <= 预算（顶部不再堆横幅的直接度量）
 *   F. 无横向溢出、无按钮被裁出视口
 *   G. 操作区按钮不竖排：同一张卡内 .stage-actions 子按钮的 y 坐标种类数
 *      桌面必须 == 1；窄屏允许 == 2（390px 下 4 个按钮共需 342px，内容区仅 324px，
 *      折成两行是合理的响应式行为）。曾出现 grid 跨列导致操作列塌缩到 60px、
 *      桌面端 4 个按钮被迫竖排（4 行）的回归，这条断言就是守它。
 *
 * 用法：node scripts/verify-page-header.mjs [baseUrl] [projectId]
 */
import { chromium } from 'playwright-core'
import { mkdir } from 'node:fs/promises'

const BASE = process.argv[2] ?? 'http://127.0.0.1'
const OUT = '../docs/uiux/screenshots/round12'

const VIEWPORTS = [
  { w: 1440, h: 900, tag: '1440x900' },
  { w: 1024, h: 900, tag: '1024x900' },
  { w: 768, h: 1024, tag: '768x1024' },
  { w: 390, h: 844, tag: '390x844' },
]

/**
 * 每页配置：
 *   railLabel    左侧流程栏应高亮的步骤名（解析是剧本阶段的子步骤，高亮「剧本」）
 *   cards        期望的 StageCard 标签，顺序敏感
 *   contentSel   首屏内容锚点
 *   contentMax   锚点 top 预算（桌面）
 *   contentMaxSm 锚点 top 预算（<=768px）。资产库窄屏最重：4 个审核按钮折两行 +
 *                4 项分项状态折三行，实测 510px 已是该信息量的下界，不是缺陷。
 */
const PAGES = [
  {
    key: 'assets',
    path: 'assets',
    title: '资产库',
    railLabel: '资产库',
    cards: ['执行进度', '批量生成'],
    contentSel: '.assets-tabs',
    contentMax: 280,
    contentMaxSm: 520,
  },
  {
    key: 'storyboard-images',
    path: 'storyboard-images',
    title: '分镜图',
    railLabel: '分镜图',
    cards: ['执行进度', '批量出图'],
    contentSel: '.scene-group',
    contentMax: 300,
    contentMaxSm: 480,
  },
  {
    key: 'storyboard',
    path: 'storyboard',
    title: '分镜看板',
    railLabel: '分镜看板',
    cards: ['执行进度', '批量出视频'],
    contentSel: '.scene-group',
    contentMax: 320,
    contentMaxSm: 460,
  },
]

/** 每个页面用哪个项目：优先选该页数据最全的项目 */
const PROJECTS = {
  assets: 'prj_de813f07bc04',
  'storyboard-images': 'prj_ee9942c5fa42',
  storyboard: 'prj_257406918e63',
}

const MEASURE = ({ contentSel }) => {
  const R = (e) => (e ? e.getBoundingClientRect() : null)
  const de = document.documentElement
  const vw = window.innerWidth

  const h1 = document.querySelector('h1')
  const h1Box = R(h1)
  const content = contentSel ? document.querySelector(contentSel) : null

  const visibleBigTitles = [...document.querySelectorAll('.page-title, .detail-title')]
    .filter((e) => {
      const r = R(e)
      const cs = getComputedStyle(e)
      return r.width > 2 && r.height > 2 && cs.visibility !== 'hidden' && cs.display !== 'none'
    })
    .map((e) => e.innerText.trim())

  const cards = [...document.querySelectorAll('.stage-card')].map((c) => ({
    label: c.querySelector('.stage-label')?.innerText.trim() ?? '',
    h: Math.round(R(c).height),
    // 操作区按钮是否横排：统计子按钮 top 的取值种类数
    actionRows: (() => {
      const btns = [...c.querySelectorAll('.stage-actions > button, .stage-actions > .n-button')]
      if (btns.length < 2) return 1
      return new Set(btns.map((b) => Math.round(R(b).top))).size
    })(),
    actionW: (() => {
      const a = c.querySelector('.stage-actions')
      return a ? Math.round(R(a).width) : null
    })(),
  }))

  const clipped = [...document.querySelectorAll('button')].filter((b) => {
    const r = R(b)
    if (r.width === 0 || r.height === 0) return false
    if (r.right <= vw + 1) return false
    let p = b.parentElement
    while (p) {
      const cs = getComputedStyle(p)
      if (/auto|scroll/.test(cs.overflowX) && p.scrollWidth > p.clientWidth + 1) return false
      p = p.parentElement
    }
    return true
  }).length

  return {
    path: location.pathname,
    h1Text: h1?.innerText.trim() ?? null,
    h1Visible: h1Box ? h1Box.width > 2 && h1Box.height > 2 : false,
    railActive: document.querySelector('.rail-step.is-active .step-label')?.innerText.trim() ?? null,
    visibleBigTitles,
    cards,
    contentTop: content ? Math.round(R(content).top) : null,
    overflowX: de.scrollWidth > de.clientWidth + 1,
    clippedButtons: clipped,
  }
}

const browser = await chromium.launch({ channel: 'chrome', headless: true })
await mkdir(OUT, { recursive: true })
const rows = []
let fails = 0

for (const vp of VIEWPORTS) {
  const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h }, deviceScaleFactor: 1 })
  const page = await ctx.newPage()
  await page.route('**/api/events*', (r) => r.abort())

  for (const p of PAGES) {
    const projectId = PROJECTS[p.key]
    await page.goto(`${BASE}/project/${projectId}/${p.path}`, { waitUntil: 'domcontentloaded', timeout: 45000 })
    await page.waitForSelector('.progress-rail', { timeout: 15000 }).catch(() => {})
    await page.waitForSelector(p.contentSel, { timeout: 15000 }).catch(() => {})
    await page.waitForTimeout(800)

    const m = await page.evaluate(MEASURE, { contentSel: p.contentSel })
    await page.screenshot({ path: `${OUT}/${p.key}__${vp.tag}.png` })

    const reasons = []
    if (m.h1Text !== p.title) reasons.push(`h1=${m.h1Text}`)
    if (m.h1Visible) reasons.push('h1Visible')
    if (m.railActive !== p.railLabel) reasons.push(`rail=${m.railActive}≠${p.railLabel}`)
    if (m.visibleBigTitles.length) reasons.push(`bigTitles=[${m.visibleBigTitles.join(',')}]`)

    // D. 卡片数量与标签
    const labels = m.cards.map((c) => c.label)
    if (m.cards.length !== 2) reasons.push(`cards=${m.cards.length}`)
    else if (labels.join('/') !== p.cards.join('/')) reasons.push(`labels=[${labels.join(',')}]`)

    // G. 操作区按钮不得竖排：桌面 1 行，窄屏最多 2 行
    const maxActionRows = vp.w <= 768 ? 2 : 1
    for (const c of m.cards) {
      if (c.actionRows > maxActionRows) reasons.push(`"${c.label}"actionRows=${c.actionRows}>${maxActionRows}`)
    }

    // E. 首屏内容起点预算：窄屏按页面自己的窄屏预算
    const budget = vp.w <= 768 ? p.contentMaxSm : p.contentMax
    if (m.contentTop === null) reasons.push('contentMissing')
    else if (m.contentTop > budget) reasons.push(`contentTop=${m.contentTop}>${budget}`)

    if (m.overflowX) reasons.push('overflowX')
    if (m.clippedButtons > 0) reasons.push(`clipped=${m.clippedButtons}`)

    const bad = reasons.length > 0
    if (bad) fails++
    rows.push({ page: p.key, viewport: vp.tag, ...m, reasons })

    const cardStr = m.cards.map((c) => `${c.label}:${c.h}px/act${c.actionRows}行`).join(' + ')
    console.log(
      `${bad ? 'FAIL' : 'PASS'}  ${p.key.padEnd(18)} ${vp.tag.padEnd(10)} ` +
        `h1=${m.h1Text}(vis=${m.h1Visible}) rail=${m.railActive} ` +
        `[${cardStr}] contentTop=${m.contentTop}<=${budget} ` +
        `overflowX=${m.overflowX} clipped=${m.clippedButtons}` +
        (bad ? `  << ${reasons.join(' ')}` : ''),
    )
  }
  await ctx.close()
}

await browser.close()
console.log(`\n合计 ${rows.length} 组，FAIL ${fails} 组`)
process.exit(fails ? 1 : 0)
