#!/usr/bin/env node
/**
 * 顶栏 ComfyUI 快捷入口验证（2026-11-24）。
 *
 * 需求：首页顶栏在「作品 / 设置」右侧增加一个打开 ComfyUI 的按钮。
 *   · 设置页只配了 1 个地址 → 点击直接在新标签页打开该地址
 *   · 配了多个地址 → 点击展开下拉，由用户选择要打开哪个
 *   · 没配地址 → 不留死按钮，提示并跳到设置页
 *   · 视觉：品牌渐变标记 + 分色 wordmark（Comfy 白 / UI 青），字号 17px 大于品牌名 15px
 *
 * 判定口径：
 *   A. 各档视口下 .comfy-entry 存在、可见、未被裁切、横向无溢出
 *   B. 顶栏导航几何中心偏移仍 <= 1px（右列加宽不得推偏中列）
 *   C. 单实例：点击后 popup 的 URL 等于配置地址
 *   D. 多实例：点击出现下拉，选项数 = 地址数；选中第 2 项后 popup URL 正确
 *   E. 零实例：点击不弹窗，落到设置页
 *   F. 视觉权重：入口底色比导航激活态更弱（不是品牌实心填充）
 *   G. **wordmark 在任何视口都必须可见**（用户明确要求「要有文字 ComfyUI」），
 *      窄屏靠缩字号换空间，不允许 display:none
 *   H. 品牌标记是渐变方块 SVG（.comfy-mark），不是通用 n-icon
 *   I. 分色：.comfy-label-accent（UI 两字母）必须是 --brand-primary
 *
 * 用法：node scripts/verify-comfy-entry.mjs [baseUrl]
 */
import { chromium } from 'playwright-core'
import { mkdir } from 'node:fs/promises'

const BASE = process.argv[2] ?? 'http://127.0.0.1'
const OUT = '../docs/uiux/screenshots/round10'

const VIEWPORTS = [
  { w: 1440, h: 900, tag: '1440x900', btnH: 40, fs: 17, mark: 26 },
  { w: 1100, h: 900, tag: '1100x900', btnH: 40, fs: 17, mark: 26 },
  { w: 768, h: 1024, tag: '768x1024', btnH: 40, fs: 17, mark: 26 },
  { w: 480, h: 900, tag: '480x900', btnH: 36, fs: 13, mark: 20 },
  { w: 390, h: 844, tag: '390x844', btnH: 36, fs: 13, mark: 20 },
  { w: 360, h: 800, tag: '360x800', btnH: 36, fs: 13, mark: 20 },
]

const SINGLE = [{ base_url: 'https://single.example.com:8443/', weight: 1, enabled: true }]
const MULTI = [
  { base_url: 'https://a.example.com:8443/', weight: 1, enabled: true },
  { base_url: 'http://b.example.net:8188', weight: 1, enabled: true },
  { base_url: 'https://disabled.example.org/', weight: 1, enabled: false },
]
const NONE = []

const MEASURE = () => {
  const R = (e) => e.getBoundingClientRect()
  /** 文字实际墨迹盒：用 Range 取文本节点盒子，不受容器 padding 影响 */
  const inkBox = (el) => {
    const r = document.createRange()
    r.selectNodeContents(el)
    const b = r.getBoundingClientRect()
    return { w: +b.width.toFixed(1), h: +b.height.toFixed(1) }
  }
  const entry = document.querySelector('.comfy-entry')
  const nav = document.querySelector('.top-nav')
  const pill = document.querySelector('.header-pill')
  const brand = document.querySelector('.brand')
  const icon = document.querySelector('.comfy-icon')
  const label = document.querySelector('.comfy-label')
  const brandName = document.querySelector('.brand-name')
  const brandMark = document.querySelector('.brand-mark')
  const mark = document.querySelector('.comfy-mark')
  const accent = document.querySelector('.comfy-label-accent')
  const navItem = document.querySelector('.nav-item')
  const vw = window.innerWidth
  const box = entry ? R(entry) : null
  const navBox = R(nav)
  const pillBox = R(pill)
  const brandBox = R(brand)
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
  const cs = entry ? getComputedStyle(entry) : null
  return {
    hasEntry: !!entry,
    entryText: entry?.innerText.trim() ?? null,
    entryWidth: box ? Math.round(box.width) : null,
    entryHeight: box ? Math.round(box.height) : null,
    /** 与品牌按钮的垂直中心差（同一行必须同基线） */
    centerDelta: box ? Math.round(box.y + box.height / 2 - (brandBox.y + brandBox.height / 2)) : null,
    entryRight: box ? Math.round(box.right) : null,
    pillRight: Math.round(pillBox.right),
    entryRadius: cs ? cs.borderRadius : null,
    entryBg: cs ? cs.backgroundColor : null,
    entryBorder: cs ? cs.borderColor : null,
    entryFontSize: cs ? parseFloat(cs.fontSize) : null,
    entryFontWeight: cs ? Number(cs.fontWeight) : null,
    /** 图标实际渲染边长（n-icon 会被 size 撑成正方形） */
    iconSize: icon ? Math.round(R(icon).width) : null,
    brandFontSize: brandName ? parseFloat(getComputedStyle(brandName).fontSize) : null,
    navFontSize: navItem ? parseFloat(getComputedStyle(navItem).fontSize) : null,
    /** 文字实际墨迹盒（Range 量取，不受 padding 干扰）—— 判「同一套字」的硬指标 */
    brandInk: brandName ? inkBox(brandName) : null,
    comfyInk: label ? inkBox(label) : null,
    brandColor: brandName ? getComputedStyle(brandName).color : null,
    comfyColor: label ? getComputedStyle(label).color : null,
    brandMarkSize: brandMark ? Math.round(R(brandMark).width) : null,
    iconColor: icon ? getComputedStyle(icon).color : null,
    hasMark: !!document.querySelector('.comfy-mark'),
    markSize: mark ? Math.round(R(mark).width) : null,
    /** SVG 圆角取 rect 的 rx 属性（CSS borderRadius 对 SVG 不生效） */
    markRadius: mark ? (mark.querySelector('rect')?.getAttribute('rx') ?? '0') : null,
    markBg: mark ? (mark.querySelector('rect')?.getAttribute('fill') ?? null) : null,
    accentColor: accent ? getComputedStyle(accent).color : null,
    /** 右列左缘到导航右缘的距离，负数=重叠 */
    navRightGap: (() => {
      const right = document.querySelector('.header-right')
      return right ? Math.round(R(right).x - navBox.right) : null
    })(),
    labelDisplay: label ? getComputedStyle(label).display : null,
    navCenterOffset: Math.round(navBox.x + navBox.width / 2 - (pillBox.x + pillBox.width / 2)),
    activeNavBg: document.querySelector('.nav-item.is-active')
      ? getComputedStyle(document.querySelector('.nav-item.is-active')).backgroundColor
      : null,
    overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    clippedButtons: clipped,
    path: location.pathname,
  }
}

const lum = (s) => {
  const m = s.match(/[\d.]+/g)
  if (!m) return null
  return 0.2126 * +m[0] + 0.7152 * +m[1] + 0.0722 * +m[2]
}

const browser = await chromium.launch({ channel: 'chrome', headless: true })
await mkdir(OUT, { recursive: true })
let fails = 0
const fail = (msg) => {
  fails++
  console.log(`   ✗ ${msg}`)
}

/** 用假数据覆盖实例接口；urls=null 表示走真实后端 */
async function newPage(ctx, urls) {
  // 假域名不可达时浏览器会把 popup 停在 chrome-error://chromewebdata/，
  // 从而拿不到真实 URL。这里把假域名拦下并返回一张空页，popup.url() 才是目标地址。
  await ctx.route(/(single|a|b|disabled)\.example\.(com|net|org)/, (r) =>
    r.fulfill({ status: 200, contentType: 'text/html', body: '<title>stub</title>stub' }),
  )
  const page = await ctx.newPage()
  await page.route('**/api/events*', (r) => r.abort())
  if (urls) {
    await page.route('**/api/settings/comfyui-instances', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(urls) }),
    )
  }
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 45000 })
  await page.waitForSelector('.project-list-page', { timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(700)
  return page
}

/* ─────────── A/B/F：三档视口的结构与视觉规格（真实后端数据） ─────────── */
console.log('== 结构与视觉规格（真实实例配置） ==')
for (const vp of VIEWPORTS) {
  const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h }, deviceScaleFactor: 2 })
  const page = await newPage(ctx, null)
  const m = await page.evaluate(MEASURE)
  await (await page.$('.header-pill')).screenshot({ path: `${OUT}/header__${vp.tag}.png` })
  console.log(
    `${vp.tag.padEnd(10)} entry=${m.entryWidth}x${m.entryHeight} text="${m.entryText}" ` +
      `centerΔ=${m.centerDelta} radius=${m.entryRadius} fs=${m.entryFontSize} ` +
      `label=${m.labelDisplay} centerOffset=${m.navCenterOffset} ` +
      `overflowX=${m.overflowX} clipped=${m.clippedButtons}`,
  )
  if (!m.hasEntry) fail('缺少 .comfy-entry')
  if (m.centerDelta !== null && Math.abs(m.centerDelta) > 1) fail(`与品牌按钮垂直中心差 ${m.centerDelta}px`)
  if (Math.abs(m.navCenterOffset) > 1) fail(`导航中心偏移 ${m.navCenterOffset}px`)
  if (m.overflowX) fail('横向溢出')
  if (m.clippedButtons > 0) fail(`被裁按钮 ${m.clippedButtons}`)
  if (m.entryHeight !== vp.btnH) fail(`高度 ${m.entryHeight} ≠ ${vp.btnH}`)
  if (m.entryFontSize !== vp.fs) fail(`字号 ${m.entryFontSize} ≠ ${vp.fs}`)
  if (m.entryFontWeight < 700) fail(`字重 ${m.entryFontWeight} < 700`)
  if (m.markSize !== vp.mark) fail(`标记 ${m.markSize}px ≠ ${vp.mark}px`)
  // G：wordmark 任何视口都必须可见 —— 这是用户「要有文字 ComfyUI」的硬要求
  if (m.labelDisplay === 'none') fail('窄屏把 ComfyUI 文字藏了（不允许）')
  if (!m.entryText.includes('Comfy')) fail(`入口文案缺 ComfyUI: "${m.entryText}"`)
  if (m.comfyInk && m.comfyInk.w < 40) fail(`文字墨迹仅 ${m.comfyInk.w}px，疑似被裁`)
  // H：标记必须是渐变方块 SVG，不能退回通用 n-icon
  if (!m.hasMark) fail('缺少 .comfy-mark 品牌标记')
  if (!m.markRadius || m.markRadius === '0') fail('标记无圆角，不是渐变方块')
  if (m.markBg === 'none' || !m.markBg) fail(`标记无渐变填充: ${m.markBg}`)
  // I：UI 两字母必须是品牌色
  if (!m.accentColor || !/5,\s*206,\s*254/.test(m.accentColor)) {
    fail(`UI 后缀非品牌色: ${m.accentColor}`)
  }
  // 宽屏字号必须大于品牌名（需求：字体可以大一点）
  if (vp.w > 768 && m.entryFontSize <= m.brandFontSize) {
    fail(`字号 ${m.entryFontSize} 未大于品牌 ${m.brandFontSize}`)
  }
  if (m.entryRadius !== '999px') fail(`圆角 ${m.entryRadius} ≠ 胶囊`)
  // 右列不得与中列导航重叠（窄屏最易发生）
  if (m.navRightGap !== null && m.navRightGap < 0) fail(`右列压住导航 ${m.navRightGap}px`)
  // 视觉权重：入口底不得亮过导航激活态（否则辅助入口盖过当前页标识）
  const le = lum(m.entryBg)
  const la = lum(m.activeNavBg)
  if (le !== null && la !== null && le > la + 1) fail(`入口底(${m.entryBg}) 亮于激活导航(${m.activeNavBg})`)
  await ctx.close()
}

/* ─────────── C：单实例直接打开 ─────────── */
console.log('\n== 单实例：点击直接打开 ==')
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await newPage(ctx, SINGLE)
  const popupPromise = ctx.waitForEvent('page', { timeout: 8000 }).catch(() => null)
  await page.click('.comfy-entry')
  const popup = await popupPromise
  if (!popup) fail('点击后没有打开新标签页')
  else {
    const url = popup.url()
    console.log(`   popup → ${url}`)
    // 目标地址不可达，浏览器会停在错误页，但 URL 必须正确
    if (!url.startsWith(SINGLE[0].base_url)) fail(`popup URL 不符: ${url}`)
    await popup.close()
  }
  const dropdown = await page.$('.n-dropdown-menu')
  if (dropdown) fail('单实例不应出现下拉')
  // 回归防护：window.open(url,'_blank','noopener') 返回 null，会误触发「被拦截」假警告
  const warned = await page.evaluate(() => !!document.querySelector('.n-message'))
  if (warned) fail('成功打开后不应出现「被拦截」提示')
  await ctx.close()
}

/* ─────────── D：多实例出现选择下拉 ─────────── */
console.log('\n== 多实例：下拉选择 ==')
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await newPage(ctx, MULTI)
  const m0 = await page.evaluate(MEASURE)
  if (m0.entryText !== 'ComfyUI') fail(`多实例入口文案异常: ${m0.entryText}`)
  await page.click('.comfy-entry')
  await page.waitForSelector('.n-dropdown-menu', { timeout: 5000 }).catch(() => {})
  const opts = await page.$$eval('.n-dropdown-option', (els) => els.map((e) => e.innerText.trim()))
  console.log(`   选项(${opts.length}): ${JSON.stringify(opts)}`)
  // 3 条配置里有 1 条 disabled，可选项应为 2
  if (opts.length !== 2) fail(`可选项数 ${opts.length} ≠ 2（应排除已停用实例）`)
  if (!opts.some((o) => o.includes(MULTI[0].base_url))) fail('缺少实例 1 地址')
  if (opts.some((o) => o.includes(MULTI[2].base_url))) fail('已停用实例不应出现')
  // 只截下拉浮层本身：固定 clip 会把浮层下方的页面内容一并截进来，
  // 视觉复核时会误读成「下拉被搜索框/新建按钮穿透」。
  // 注意 naive-ui 的 dropdown 根节点类名是 .n-dropdown-menu（不是 .n-popover）。
  const pop = await page.$('.n-dropdown-menu')
  if (pop) await pop.screenshot({ path: `${OUT}/dropdown__1440x900.png` })
  // 浮层必须真的压在页面之上：第二项中心点的 elementFromPoint 必须落在浮层内，
  // 且浮层底色不透明（半透明会被下方的搜索框/按钮透出来，读成层级 bug）。
  const stack = await page.evaluate(() => {
    const menu = document.querySelector('.n-dropdown-menu')
    const r = menu.getBoundingClientRect()
    const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height * 0.75)
    const bg = getComputedStyle(menu).backgroundColor
    const alpha = bg.startsWith('rgba') ? Number(bg.split(',')[3]) : 1
    return { inside: menu.contains(hit), bg, opaque: alpha >= 0.95 }
  })
  console.log(`   浮层命中=${stack.inside} 底色=${stack.bg} 不透明=${stack.opaque}`)
  if (!stack.inside) fail('下拉浮层被页面元素遮挡（层级错误）')
  if (!stack.opaque) fail(`下拉底色半透明: ${stack.bg}`)
  const popupPromise = ctx.waitForEvent('page', { timeout: 8000 }).catch(() => null)
  await page.click(`.n-dropdown-option:has-text("${MULTI[1].base_url}")`)
  const popup = await popupPromise
  if (!popup) fail('选择第 2 项后没有打开新标签页')
  else {
    const url = popup.url()
    console.log(`   选中第 2 项 → ${url}`)
    if (!url.startsWith(MULTI[1].base_url)) fail(`popup URL 不符: ${url}`)
    await popup.close()
  }
  await ctx.close()
}

/* ─────────── E：零实例 → 引导到设置页 ─────────── */
console.log('\n== 零实例：引导配置 ==')
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await newPage(ctx, NONE)
  // 注意：等待窗口要短。naive-ui 的 message 默认 3s 自动消失，
  // 若这里等满 3s 才取文案，取到的必然是空（会被误判成「没有提示」）。
  const popupPromise = ctx.waitForEvent('page', { timeout: 1200 }).catch(() => null)
  await page.click('.comfy-entry')
  const popup = await popupPromise
  if (popup) fail('零实例不应打开新标签页')
  await page.waitForTimeout(400)
  const m = await page.evaluate(() => ({
    path: location.pathname,
    toast:
      document.querySelector('.n-message__content')?.textContent?.trim() ??
      document.querySelector('[class*="n-message"]')?.textContent?.trim() ??
      null,
  }))
  console.log(`   path=${m.path} toast="${m.toast}"`)
  if (!m.path.startsWith('/settings')) fail(`未跳转到设置页: ${m.path}`)
  if (!m.toast) fail('缺少提示文案')
  await ctx.close()
}

await browser.close()
console.log(`\n合计 6 组，FAIL ${fails} 处`)
process.exit(fails ? 1 : 0)
