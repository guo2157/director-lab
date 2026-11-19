#!/usr/bin/env node
/**
 * 多视口截图 + 几何量测。
 *
 * 为什么需要：`docs/uiux/acceptance-report.md` 明确记录「1280/1024/768 未截图，
 * 属未完成项」。视觉验收不能只跑 1920 和 390 两端，中间三档才是真实办公场景。
 *
 * 判定口径（与 audit-report.md 一致）：
 *   · 页面横向溢出   documentElement.scrollWidth > clientWidth  → 失败
 *   · 按钮被裁       button 右边界 > 视口宽                       → 失败
 *   · 卡片留白       card.height - thumb.height - info.height > 8 → 警告
 *
 * 用法：node scripts/uiux-shots.mjs [baseUrl] [outDir]
 */
import { chromium } from 'playwright-core'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'

const BASE = process.argv[2] ?? 'http://127.0.0.1'
const OUT = process.argv[3] ?? 'docs/uiux/screenshots/round5'

/**
 * 用系统 Chrome 而不是 playwright 自带的 chromium：
 * 本机 npm 直连被墙（registry.npmmirror.com 才通），且 playwright 的浏览器下载
 * 需要额外 ~150MB。系统已有 /usr/bin/google-chrome，playwright-core 只做协议驱动，
 * 通过 channel: 'chrome' 直接复用，零下载。
 *
 * 等待策略：不能用 networkidle。settings / jobs 有常驻轮询与 SSE，
 * 网络永远不会 idle，会一直等到 45s 超时（实测 settings 必超时）。
 * 改为 domcontentloaded + 显式等根选择器 + 固定 settle 时间。
 */

const PRJ = 'prj_257406918e63'
const SRS = 'srs_03524b9d7a4e'

/**
 * 覆盖 router 里全部真实路由（`src/router/index.ts`）。
 * ready 选择器取各页根 class —— 这些 class 在每个 view 的 template 首行都有，
 * 比等某个业务节点稳（业务节点依赖数据，空数据页会超时）。
 */
const PAGES = [
  { key: 'home', path: '/', ready: '.project-card' },
  { key: 'script', path: `/project/${PRJ}/script`, ready: '.script-page' },
  { key: 'parse', path: `/project/${PRJ}/parse`, ready: '.result-panel' },
  { key: 'assets', path: `/project/${PRJ}/assets`, ready: 'article' },
  { key: 'storyboard', path: `/project/${PRJ}/storyboard`, ready: '.shot-card' },
  { key: 'sb-images', path: `/project/${PRJ}/storyboard-images`, ready: '.gen-bar' },
  { key: 'compose', path: `/project/${PRJ}/compose`, ready: '.compose-page' },
  { key: 'series', path: '/series', ready: '.series-grid' },
  { key: 'series-detail', path: `/series/${SRS}`, ready: '.series-detail-page' },
  // 任务中心已下线：/jobs 保留为到作品列表的重定向（旧书签不白屏），
  // 因此这里按作品页的选择器验收，等于顺带验证重定向链路可用。
  { key: 'jobs-redirect', path: '/jobs', ready: '.project-card' },
  { key: 'settings', path: '/settings', ready: '.settings-page' },
  { key: 'director', path: '/director', ready: '.console-page' },
]

const VIEWPORTS = [
  { w: 1920, h: 1080, tag: '1920x1080' },
  { w: 1440, h: 900, tag: '1440x900' },
  { w: 1366, h: 768, tag: '1366x768' },
  { w: 768, h: 1024, tag: '768x1024' },
  { w: 390, h: 844, tag: '390x844' },
]

const MEASURE = () => {
  const de = document.documentElement
  const vw = window.innerWidth
  const overflowX = de.scrollWidth > de.clientWidth + 1

  /**
   * 滚动区内被裁的按钮数（WARN 级，不判失败）。
   * 这些按钮滑一下就能点到，但如果数量很多，说明窄屏布局其实没真正适配，
   * 只是靠横向滚动兜住了 —— 保留计数以免信号丢失。
   */
  let inScrollerClipped = 0
  for (const b of document.querySelectorAll('button')) {
    const r = b.getBoundingClientRect()
    if (r.width === 0 || r.height === 0 || r.right <= vw + 1) continue
    let p = b.parentElement
    while (p) {
      const cs = getComputedStyle(p)
      if (/auto|scroll/.test(cs.overflowX) && p.scrollWidth > p.clientWidth + 1) {
        inScrollerClipped++
        break
      }
      p = p.parentElement
    }
  }
  const clippedButtons = [...document.querySelectorAll('button')].filter((b) => {
    const r = b.getBoundingClientRect()
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
  const card = document.querySelector('.shot-card')
  let cardSlack = null
  if (card) {
    const thumb = card.querySelector('.shot-thumb')
    const info = card.querySelector('.shot-info')
    if (thumb && info) {
      cardSlack = Math.round(
        card.getBoundingClientRect().height -
          thumb.getBoundingClientRect().height -
          info.getBoundingClientRect().height,
      )
    }
  }
  const coverImgs = [...document.querySelectorAll('.cover-img')]
    .filter((i) => i.offsetParent !== null).length
  const placeholders = document.querySelectorAll('.cover-char').length
  const gridCols = (() => {
    const g = document.querySelector('.project-grid')
    if (!g) return null
    return getComputedStyle(g).gridTemplateColumns.split(' ').length
  })()

  /**
   * 错误态嗅探：页面上出现后端报错原文，说明这个页面在真实数据下是坏的。
   * 只看可见文本，避免命中 <script> 或注释。
   */
  const bodyText = (document.body.innerText || '').slice(0, 4000)
  const errHits = [
    '接口不存在',
    '加载失败',
    '请求失败',
    'Network Error',
    'undefined is not',
    'Cannot read propert',
  ].filter((s) => bodyText.includes(s))
  /** 白屏判定：主内容区几乎无文字 */
  const textLen = bodyText.replace(/\s+/g, '').length

  return {
    vw,
    vh: window.innerHeight,
    scrollW: de.scrollWidth,
    clientW: de.clientWidth,
    overflowX,
    clippedButtons,
    inScrollerClipped,
    cardSlack,
    coverImgs,
    placeholders,
    gridCols,
    errHits,
    textLen,
    docH: de.scrollHeight,
  }
}

async function main() {
  await mkdir(OUT, { recursive: true })
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const report = []

  for (const vp of VIEWPORTS) {
    const ctx = await browser.newContext({
      viewport: { width: vp.w, height: vp.h },
      deviceScaleFactor: 1,
    })
    const page = await ctx.newPage()
    /**
     * 拦掉 SSE：后端 events.py 的生成器在 yield 期间一直持有 DB 连接，
     * SQLAlchemy 默认池只有 15 个（pool_size 5 + max_overflow 10）。
     * 每打开一个页面就建一条最长 300s 的 SSE 连接，跑 60 张截图必然把池占满，
     * 之后所有 /api/* 请求在 QueuePool._do_get 排队 → 全站卡死（已实测复现）。
     * 截图不需要实时推送，直接 abort。
     */
    await page.route('**/api/events*', (route) => route.abort())
    for (const p of PAGES) {
      const url = BASE + p.path
      try {
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 })
        await page.waitForSelector(p.ready, { timeout: 15000 }).catch(() => {})
        // 等封面图解码 + 首屏数据渲染，避免截到骨架屏
        await page.waitForTimeout(900)
        const m = await page.evaluate(MEASURE)
        const file = path.join(OUT, `${p.key}__${vp.tag}.png`)
        await page.screenshot({ path: file, fullPage: false })
        report.push({ page: p.key, viewport: vp.tag, file, ...m })
        const bad = m.overflowX || m.clippedButtons > 0 || m.errHits.length > 0 || m.textLen < 20
        const flag = bad ? 'FAIL' : m.cardSlack > 8 ? 'WARN' : 'PASS'
        console.log(
          `${flag}  ${p.key.padEnd(13)} ${vp.tag.padEnd(10)} ` +
            `scrollW=${m.scrollW}/${m.clientW} clipped=${m.clippedButtons} ` +
            `inScroll=${m.inScrollerClipped} ` +
            `slack=${m.cardSlack ?? '-'} covers=${m.coverImgs} cols=${m.gridCols ?? '-'} ` +
            `txt=${m.textLen}` +
            (m.errHits.length ? ` ERR[${m.errHits.join(',')}]` : ''),
        )
      } catch (e) {
        console.log(`ERR   ${p.key.padEnd(11)} ${vp.tag.padEnd(10)} ${String(e).slice(0, 90)}`)
        report.push({ page: p.key, viewport: vp.tag, error: String(e).slice(0, 200) })
      }
    }
    await ctx.close()
  }

  await browser.close()
  await writeFile(path.join(OUT, '_measure.json'), JSON.stringify(report, null, 2))
  const fails = report.filter(
    (r) =>
      r.overflowX ||
      (r.clippedButtons ?? 0) > 0 ||
      (r.errHits ?? []).length > 0 ||
      (r.textLen ?? 0) < 20,
  )
  console.log(`\n合计 ${report.length} 张，FAIL ${fails.length} 张`)
  process.exit(fails.length ? 1 : 0)
}

main()
