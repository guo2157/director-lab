#!/usr/bin/env node
/**
 * 剧本 / 解析拆分验证（2026-11-20）。
 *
 * 判定口径（每视口 × 每页）：
 *   A. 左栏恰好 6 步，序号 01–06，标签依次为
 *      剧本 / 解析 / 资产库 / 分镜图 / 分镜看板 / 合成与导出
 *   B. 当前页对应的步骤高亮（/parse → 02 解析，不再回落到 01 剧本）
 *   C. 页内不再有子流程胶囊（FlowNav 已删）
 *   D. 页头 H1 不可见但仍在 DOM 且文本正确
 *   E. 锁定步骤是真 disabled 且有 aria-disabled + 可访问的原因说明
 *   F. 无横向溢出
 *
 * 用法：node scripts/verify-script-parse-split.mjs [baseUrl] [projectId]
 */
import { chromium } from 'playwright-core'
import { mkdir } from 'node:fs/promises'

const BASE = process.argv[2] ?? 'http://127.0.0.1'
const PROJECT = process.argv[3] ?? 'prj_257406918e63'
const OUT = '../docs/uiux/screenshots/split'
const EXEC = `${process.env.HOME}/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome`

const VIEWPORTS = [
  { w: 1440, h: 900, tag: '1440x900' },
  { w: 1024, h: 900, tag: '1024x900' },
  { w: 768, h: 1024, tag: '768x1024' },
  { w: 390, h: 844, tag: '390x844' },
]

const EXPECTED_STEPS = ['剧本', '解析', '资产库', '分镜图', '分镜看板', '合成与导出']

const PAGES = [
  { path: 'script', title: '剧本', activeNo: '01' },
  { path: 'parse', title: '解析', activeNo: '02' },
  { path: 'assets', title: '资产库', activeNo: '03' },
]

const rows = []
let failures = 0

await mkdir(OUT, { recursive: true })
const browser = await chromium.launch({ executablePath: EXEC, args: ['--no-sandbox'] })

for (const vp of VIEWPORTS) {
  const page = await browser.newPage({ viewport: { width: vp.w, height: vp.h } })
  for (const p of PAGES) {
    await page.goto(`${BASE}/project/${PROJECT}/${p.path}`, {
      waitUntil: 'networkidle',
      timeout: 45000,
    })
    await page.waitForTimeout(700)

    const m = await page.evaluate(() => {
      const rail = document.querySelector('.progress-rail')
      const items = [...(rail?.querySelectorAll('.rail-step') ?? [])]
      const activeItem = rail?.querySelector('.rail-step.is-active')
      const activeBtn = activeItem?.querySelector('.step-hit')
      const h1 = document.querySelector('h1')
      const h1Style = h1 ? getComputedStyle(h1) : null
      const disabled = items
        .map((li) => {
          const btn = li.querySelector('.step-hit')
          return {
            label: li.querySelector('.step-label')?.textContent?.trim() ?? '',
            disabled: btn?.disabled ?? null,
            ariaDisabled: btn?.getAttribute('aria-disabled'),
            ariaLabel: btn?.getAttribute('aria-label'),
            title: btn?.getAttribute('title'),
          }
        })
        .filter((d) => d.disabled)
      return {
        stepCount: items.length,
        stepLabels: items.map((li) => li.querySelector('.step-label')?.textContent?.trim() ?? ''),
        stepNos: items.map((li) => li.querySelector('.step-index')?.textContent?.trim() ?? ''),
        activeLabel: activeItem?.querySelector('.step-label')?.textContent?.trim() ?? null,
        activeNo: activeBtn?.querySelector('.step-index')?.textContent?.trim() ?? null,
        activeAriaCurrent: activeBtn?.getAttribute('aria-current'),
        flowNavExists: !!document.querySelector('.flow-nav'),
        h1Text: h1?.textContent?.trim() ?? null,
        h1Visible: h1Style
          ? !(h1Style.position === 'absolute' && h1Style.width === '1px')
          : false,
        disabled,
        scrollW: document.documentElement.scrollWidth,
        clientW: document.documentElement.clientWidth,
      }
    })

    const reasons = []
    if (m.stepCount !== 6) reasons.push(`步数=${m.stepCount}≠6`)
    if (JSON.stringify(m.stepLabels) !== JSON.stringify(EXPECTED_STEPS)) {
      reasons.push(`标签=${m.stepLabels.join('/')}`)
    }
    const expectedNos = ['01', '02', '03', '04', '05', '06']
    if (JSON.stringify(m.stepNos) !== JSON.stringify(expectedNos)) {
      reasons.push(`序号=${m.stepNos.join('/')}`)
    }
    if (m.activeLabel !== p.title) reasons.push(`高亮=${m.activeLabel}≠${p.title}`)
    if (m.activeNo !== p.activeNo) reasons.push(`高亮序号=${m.activeNo}≠${p.activeNo}`)
    if (m.activeAriaCurrent !== 'page') reasons.push(`aria-current=${m.activeAriaCurrent}`)
    if (m.flowNavExists) reasons.push('FlowNav 仍存在')
    if (m.h1Text !== p.title) reasons.push(`H1=${m.h1Text}≠${p.title}`)
    if (m.h1Visible) reasons.push('H1 可见')
    if (m.scrollW > m.clientW + 1) reasons.push(`横向溢出 ${m.scrollW}>${m.clientW}`)
    for (const d of m.disabled) {
      if (d.ariaDisabled !== 'true') reasons.push(`锁定 ${d.label} 缺 aria-disabled`)
      if (!d.ariaLabel || !d.ariaLabel.includes('锁定')) {
        reasons.push(`锁定 ${d.label} aria-label 未说明原因`)
      }
    }

    const ok = reasons.length === 0
    if (!ok) failures += 1
    rows.push({
      page: p.path,
      viewport: vp.tag,
      ok,
      active: `${m.activeNo} ${m.activeLabel}`,
      locked: m.disabled.map((d) => d.label).join(',') || '—',
      reasons,
    })
    console.log(
      `${ok ? 'PASS' : 'FAIL'}  ${p.path.padEnd(8)} ${vp.tag.padEnd(10)} ` +
        `active=${m.activeNo} ${m.activeLabel} locked=[${m.disabled.map((d) => d.label).join(',') || '—'}]` +
        (ok ? '' : `\n        ✗ ${reasons.join('; ')}`),
    )
    await page.screenshot({ path: `${OUT}/${p.path}__${vp.tag}.png`, fullPage: false })
  }
  await page.close()
}

await browser.close()

console.log('\n汇总：')
console.log(`  用例 ${rows.length} 个，失败 ${failures} 个`)
if (failures) {
  console.log('\n失败明细：')
  for (const r of rows.filter((x) => !x.ok)) {
    console.log(`  ${r.page} @ ${r.viewport}: ${r.reasons.join('; ')}`)
  }
}
process.exit(failures ? 1 : 0)
