#!/usr/bin/env node
/**
 * 断点收敛检查器 —— 响应式断点唯一来源验收工具
 *
 * 规则来源：DESIGN.md「断点」章节（与 global.css 配套）
 *
 * 标准断点（三档）：
 *   1100px —— 桌面收窄（侧栏/面板退让、网格降列）
 *    768px —— 平板（单列布局、紧凑内边距、触控放大）
 *    480px —— 手机（最小点击区域、极端窄屏适配）
 *
 * 配套允许：min-width: 769px / 1101px（与 max-width: 768px / 1100px 互补的桌面条件）
 *
 * 用法：
 *   node scripts/check-breakpoints.mjs           # 输出按文件统计的断点分布
 *   node scripts/check-breakpoints.mjs --json    # 机器可读
 *   node scripts/check-breakpoints.mjs --fail    # 存在非标准断点时退出码 1
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const SRC = join(ROOT, 'src')
const EXTENSIONS = new Set(['.vue', '.css', '.scss'])

/** 允许出现的宽度取值（px） */
const ALLOWED = new Set(['1100', '768', '480', '769', '1101'])

const args = process.argv.slice(2)
const asJson = args.includes('--json')
const shouldFail = args.includes('--fail')

const WIDTH_RE = /\((max|min)-width\s*:\s*([0-9]+)px\)/g

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name === 'node_modules' || name.startsWith('.')) continue
    const full = join(dir, name)
    const st = statSync(full)
    if (st.isDirectory()) walk(full, out)
    else if (EXTENSIONS.has(name.slice(name.lastIndexOf('.')))) out.push(full)
  }
  return out
}

const files = walk(SRC)
const violations = []
const dist = new Map()

for (const file of files) {
  const text = readFileSync(file, 'utf8')
  const rel = relative(ROOT, file).split(sep).join('/')
  let m
  while ((m = WIDTH_RE.exec(text))) {
    const value = m[2]
    dist.set(value, (dist.get(value) ?? 0) + 1)
    if (!ALLOWED.has(value)) {
      const line = text.slice(0, m.index).split('\n').length
      violations.push({ file: rel, line, kind: `${m[1]}-width`, value: `${value}px` })
    }
  }
}

violations.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line)

if (asJson) {
  console.log(JSON.stringify({
    generatedAt: new Date().toISOString(),
    allowed: [...ALLOWED].sort(),
    distribution: Object.fromEntries([...dist.entries()].sort((a, b) => b[1] - a[1])),
    violations
  }, null, 2))
} else {
  console.log('断点取值分布（标准档：1100 / 768 / 480；配套 min-width: 769 / 1101）\n')
  const rows = [...dist.entries()].sort((a, b) => b[1] - a[1])
  for (const [value, count] of rows) {
    const mark = ALLOWED.has(value) ? ' ' : '✗'
    console.log(`${mark} ${String(value).padStart(5)}px  × ${count}`)
  }
  console.log('')
  if (violations.length) {
    console.log(`非标准断点 ${violations.length} 处：`)
    for (const v of violations) console.log(`  ${v.file}:${v.line}  ${v.kind}: ${v.value}`)
  }
}

if (violations.length) {
  console.error(`\n✗ 发现 ${violations.length} 处非标准断点，只允许 1100 / 768 / 480（min-width 可用 769 / 1101）。`)
  if (shouldFail) process.exit(1)
} else {
  console.log('✓ 全部断点均为标准档（1100 / 768 / 480 / min-769 / min-1101）')
}
