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
  const dialog = await page.$('.comfy-dialog')
  if (dialog) fail('单实例不应弹出选择弹窗')
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
  // 多实例：点击入口不得立刻开新标签页，必须先弹窗让用户选
  const early = ctx.waitForEvent('page', { timeout: 800 }).catch(() => null)
  await page.click('.comfy-entry')
  if (await early) fail('多实例不应在点击瞬间打开新标签页')
  await page.waitForSelector('.comfy-dialog', { timeout: 5000 }).catch(() => {})
  const dlg = await page.evaluate(() => {
    const el = document.querySelector('.comfy-dialog')
    if (!el) return null
    const r = el.getBoundingClientRect()
    const cs = getComputedStyle(el)
    const bg = cs.backgroundColor
    return {
      w: Math.round(r.width),
      h: Math.round(r.height),
      radius: cs.borderRadius,
      bg,
      alpha: bg.startsWith('rgba') ? Number(bg.split(',')[3]) : 1,
      role: el.getAttribute('role'),
      ariaModal: el.getAttribute('aria-modal'),
      labelledby: el.getAttribute('aria-labelledby'),
      titleId: el.querySelector('.comfy-dialog-title')?.id ?? null,
      title: el.querySelector('.comfy-dialog-title')?.textContent?.trim() ?? null,
      opts: [...el.querySelectorAll('.comfy-option')].map((e) => e.innerText.trim()),
      inViewport:
        r.left >= -1 &&
        r.right <= window.innerWidth + 1 &&
        r.top >= -1 &&
        r.bottom <= window.innerHeight + 1,
    }
  })
  if (!dlg) fail('多实例点击后未出现选择弹窗')
  else {
    console.log(
      `   弹窗 ${dlg.w}x${dlg.h} radius=${dlg.radius} 标题=${dlg.title} 选项(${dlg.opts.length}): ${JSON.stringify(dlg.opts)}`,
    )
    // 3 条配置里有 1 条 disabled，可选项应为 2
    if (dlg.opts.length !== 2) fail(`可选项数 ${dlg.opts.length} ≠ 2（应排除已停用实例）`)
    if (!dlg.opts.some((o) => o.includes(MULTI[0].base_url))) fail('缺少实例 1 地址')
    if (dlg.opts.some((o) => o.includes(MULTI[2].base_url))) fail('已停用实例不应出现')
    // 设计规范：容器圆角走 --radius-xl(20px)；底色必须不透明（半透明会把下方内容透出来读成层级 bug）
    if (dlg.radius !== '20px') fail(`弹窗圆角 ${dlg.radius} ≠ 20px`)
    if (dlg.alpha < 0.95) fail(`弹窗底色半透明: ${dlg.bg}`)
    if (dlg.role !== 'dialog' || dlg.ariaModal !== 'true') fail('弹窗缺少 role=dialog / aria-modal=true')
    if (!dlg.labelledby || dlg.labelledby !== dlg.titleId) fail('弹窗 aria-labelledby 未指向标题')
    if (!dlg.title) fail('弹窗缺少标题')
    if (!dlg.inViewport) fail('弹窗超出视口')
    const shot = await page.$('.comfy-dialog')
    if (shot) await shot.screenshot({ path: `${OUT}/dialog-choose__1440x900.png` })
  }
  // 弹窗必须真的压在页面之上：中心点的 elementFromPoint 必须落在弹窗内
  const stack = await page.evaluate(() => {
    const el = document.querySelector('.comfy-dialog')
    if (!el) return { inside: false }
    const r = el.getBoundingClientRect()
    const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)
    return { inside: el.contains(hit) }
  })
  console.log(`   弹窗命中=${stack.inside}`)
  if (!stack.inside) fail('弹窗被页面元素遮挡（层级错误）')
  const popupPromise = ctx.waitForEvent('page', { timeout: 8000 }).catch(() => null)
  await page.click(`.comfy-option:has-text("${MULTI[1].base_url}")`)
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

/* ─────────── E：零实例 → 先弹窗提示，用户确认后再跳到设置页 ─────────── */
console.log('\n== 零实例：弹窗提示并引导配置 ==')
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await newPage(ctx, NONE)
  const popupPromise = ctx.waitForEvent('page', { timeout: 1200 }).catch(() => null)
  await page.click('.comfy-entry')
  const popup = await popupPromise
  if (popup) fail('零实例不应打开新标签页')
  await page.waitForSelector('.comfy-dialog', { timeout: 3000 }).catch(() => {})
  const tip = await page.evaluate(() => {
    const el = document.querySelector('.comfy-dialog')
    if (!el) return null
    return {
      path: location.pathname + location.search,
      title: el.querySelector('.comfy-dialog-title')?.textContent?.trim() ?? '',
      desc: el.querySelector('.comfy-dialog-desc')?.textContent?.trim() ?? '',
      buttons: [...el.querySelectorAll('.comfy-btn')].map((b) => (b.textContent ?? '').trim()),
      hasList: !!el.querySelector('.comfy-dialog-list'),
    }
  })
  if (!tip) fail('零实例点击后未出现提示弹窗')
  else {
    console.log(`   弹窗标题=${tip.title} 按钮=${JSON.stringify(tip.buttons)} path=${tip.path}`)
    // 关键：弹窗出现时还不能跳走，必须等用户点确认 —— 否则点击像误触
    if (tip.path.startsWith('/settings')) fail('弹窗出现时不应已跳转（需用户确认）')
    if (tip.hasList) fail('零实例不应渲染实例列表')
    if (!tip.desc) fail('提示弹窗缺少说明文案')
    if (!tip.buttons.some((b) => b.includes('前往设置'))) fail('缺少「前往设置」按钮')
    if (!tip.buttons.some((b) => b.includes('稍后'))) fail('缺少「稍后再说」按钮')
    const shot = await page.$('.comfy-dialog')
    if (shot) await shot.screenshot({ path: `${OUT}/dialog-none__1440x900.png` })
  }
  await page.click('.comfy-btn.is-primary')
  await page.waitForTimeout(900)
  const after = await page.evaluate(() => ({
    path: location.pathname,
    search: location.search,
    title: document.querySelector('.page-title')?.textContent?.trim() ?? null,
    dialogGone: !document.querySelector('.comfy-dialog'),
  }))
  console.log(`   确认后 path=${after.path}${after.search} 面板标题=${after.title}`)
  if (!after.path.startsWith('/settings')) fail(`未跳转到设置页: ${after.path}`)
  if (!after.search.includes('tab=comfyui')) fail(`未带 tab=comfyui 参数: ${after.search}`)
  if (after.title !== 'ComfyUI 配置') fail(`未落在 ComfyUI 面板: ${after.title}`)
  if (!after.dialogGone) fail('跳转后弹窗未关闭')
  await ctx.close()
}

await browser.close()
console.log(`\n合计 6 组，FAIL ${fails} 处`)
process.exit(fails ? 1 : 0)
