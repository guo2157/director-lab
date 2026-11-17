#!/usr/bin/env node
/**
 * 硬编码色值检查器 —— 设计令牌落地验收工具
 *
 * 规则来源：DESIGN.md / frontend/src/styles/global.css（唯一事实来源）
 *
 * 用法：
 *   node scripts/check-colors.mjs            # 输出按文件统计的色值数量
 *   node scripts/check-colors.mjs --json     # 机器可读，便于 CI 与对比基线
 *   node scripts/check-colors.mjs --fail     # 存在任何硬编码色值时退出码 1（阶段 2 之后的验收门槛）
 *   node scripts/check-colors.mjs --fail-on 0
 *
 * 统计口径：
 *   - 十六进制色值：#rgb / #rgba / #rrggbb / #rrggbbaa
 *   - rgba()/rgb() 字面量（应改用 --brand-soft / --overlay-hover 这类语义令牌）
 *   - 例外：frontend/src/styles/global.css 是令牌定义处，不计入
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const SRC = join(ROOT, 'src')
const TOKEN_FILE = join('src', 'styles', 'global.css')
const EXTENSIONS = new Set(['.vue', '.ts', '.tsx', '.css', '.scss'])

const args = process.argv.slice(2)
const asJson = args.includes('--json')
const shouldFail = args.includes('--fail')
const failOnIdx = args.indexOf('--fail-on')
const failOn = failOnIdx >= 0 ? Number(args[failOnIdx + 1] ?? 0) : shouldFail ? 0 : null

const HEX = /#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b/g
const FUNC = /\b(?:rgba?|hsla?)\([^)]*\)/g

/** 递归收集待检查文件 */
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

const files = walk(SRC).filter((f) => relative(ROOT, f) !== TOKEN_FILE)

const rows = []
for (const file of files) {
  const text = readFileSync(file, 'utf8')
  const hex = text.match(HEX) ?? []
  const func = text.match(FUNC) ?? []
  if (hex.length === 0 && func.length === 0) continue
  rows.push({
    file: relative(ROOT, file).split(sep).join('/'),
    hex: hex.length,
    func: func.length,
    samples: [...new Set([...hex, ...func])].slice(0, 5)
  })
}

rows.sort((a, b) => b.hex + b.func - (a.hex + a.func))
const totals = rows.reduce(
  (acc, r) => ({ hex: acc.hex + r.hex, func: acc.func + r.func }),
  { hex: 0, func: 0 }
)

if (asJson) {
  console.log(
    JSON.stringify(
      {
        generatedAt: new Date().toISOString(),
        tokenFile: TOKEN_FILE.split(sep).join('/'),
        totals,
        files: rows
      },
      null,
      2
    )
  )
} else {
  console.log('硬编码色值统计（不含令牌定义文件 ' + TOKEN_FILE.split(sep).join('/') + '）\n')
  const head = ['hex', 'rgba', 'file']
  const table = rows.map((r) => [String(r.hex), String(r.func), r.file])
  const widths = head.map((h, i) => Math.max(h.length, ...table.map((t) => t[i].length)))
  const fmt = (cells) => cells.map((c, i) => c.padEnd(widths[i])).join('  ')
  console.log(fmt(head))
  console.log(widths.map((w) => '-'.repeat(w)).join('  '))
  for (const row of table) console.log(fmt(row))
  console.log('')
  console.log(
    `合计：hex ${totals.hex} 处，rgba/rgb 字面量 ${totals.func} 处，涉及 ${rows.length} 个文件`
  )
  if (rows.length > 0 && !shouldFail) {
    console.log('提示：阶段 2 目标是把 hex 降为 0，rgba() 改用 --brand-soft / --overlay-hover 等语义令牌。')
  }
}

if (failOn !== null) {
  const over = totals.hex > failOn
  if (over) {
    console.error(
      `\n✗ 硬编码 hex 色值 ${totals.hex} 处，超过允许上限 ${failOn}。请改用 DESIGN.md 中的令牌。`
    )
    process.exit(1)
  }
  if (shouldFail && totals.func > 0) {
    console.warn(`\n⚠ 仍有 ${totals.func} 处 rgba()/rgb() 字面量，建议改用语义 soft/line 令牌。`)
  }
  console.log(`\n✓ hex 色值 ${totals.hex} 处，未超过上限 ${failOn}。`)
}
