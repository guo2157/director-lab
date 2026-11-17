/**
 * 剧本 Markdown 解析：把标准剧本格式拆成可排版的块。
 *
 * 后端 normalize/parse 产出的是约定格式，不是通用 Markdown：
 *   第1集
 *   场次1 周六上午·游乐场正门入口
 *   场景：彩色摩天轮在背景缓缓转动…
 *   人物：陆屿、苏晚
 *   △ 陆屿穿浅色休闲衬衫，站在检票口旁等苏晚。
 *   苏晚：不好意思，排队买饮料耽误了！
 *   （第1集完）
 * 另外兼容标题 / 列表 / 引用 / 分隔线 / 加粗 / 行内代码 / @引用。
 */

export type BlockKind = 'action' | 'dialogue' | 'heading' | 'list' | 'quote' | 'divider' | 'para'

export interface Block {
  kind: BlockKind
  text: string
  /** 台词角色 */
  who?: string
  /** Markdown 标题层级 */
  level?: number
  /** 列表项 */
  items?: string[]
}

export interface Meta {
  label: string
  value: string
}

export type ScriptNode =
  | { kind: 'episode'; text: string }
  | { kind: 'scene'; no: string; title: string; meta: Meta[]; body: Block[] }
  | { kind: 'loose'; body: Block[] }
  | { kind: 'end'; text: string }

const EPISODE_RE = /^第\s*[0-9一二三四五六七八九十百千]+\s*集\s*(.*)$/
const SCENE_RE = /^场[次景]\s*([0-9]{1,4})\s*[、.．,，:：]?\s*(.*)$/
const META_RE = /^(场景|时间|地点|人物|道具|画面|音效|备注|字幕|镜头)[：:]\s*(.*)$/
const ACTION_RE = /^[△▲▽▼]\s*(.*)$/
const DIALOGUE_RE = /^(?:\*\*)?([^\s：:*]{1,12})(?:\*\*)?\s*[：:]\s*(.+)$/
const HEADING_RE = /^(#{1,6})\s+(.*)$/
const LIST_RE = /^[-*+]\s+(.*)$/
const QUOTE_RE = /^>\s?(.*)$/
const DIVIDER_RE = /^([-*_]\s*){3,}$/
const END_RE = /^[（(][^）)]{0,12}完[^）)]{0,12}[）)]$/
/** 这些前缀是段落标签，不是角色名 */
const NON_SPEAKER = new Set(['场景', '时间', '地点', '人物', '道具', '画面', '音效', '备注', '字幕', '镜头', '提示', '说明', '概要', '注意'])

type OpenNode =
  | { kind: 'scene'; no: string; title: string; meta: Meta[]; body: Block[] }
  | { kind: 'loose'; body: Block[] }

/** 逐行解析成节点序列：集标题 / 场次（含场景与人物元信息 + 正文块）/ 结尾标记 */
export function parseScript(raw: string): ScriptNode[] {
  const nodes: ScriptNode[] = []
  let open: OpenNode | null = null

  const ensure = (): OpenNode => {
    if (!open) {
      open = { kind: 'loose', body: [] }
      nodes.push(open)
    }
    return open
  }
  const push = (b: Block) => ensure().body.push(b)

  for (const rawLine of (raw || '').replace(/\r\n?/g, '\n').split('\n')) {
    const line = rawLine.trim()
    if (!line) continue

    if (END_RE.test(line)) {
      nodes.push({ kind: 'end', text: line })
      open = null
      continue
    }
    if (DIVIDER_RE.test(line)) {
      push({ kind: 'divider', text: '' })
      continue
    }
    const h = line.match(HEADING_RE)
    if (h) {
      push({ kind: 'heading', text: h[2], level: h[1].length })
      continue
    }
    if (EPISODE_RE.test(line)) {
      nodes.push({ kind: 'episode', text: line })
      open = null
      continue
    }
    const sc = line.match(SCENE_RE)
    if (sc) {
      const node = { kind: 'scene' as const, no: sc[1], title: sc[2].trim(), meta: [], body: [] }
      nodes.push(node)
      open = node
      continue
    }
    const meta = line.match(META_RE)
    if (meta) {
      const body = ensure()
      if (body.kind === 'scene') body.meta.push({ label: meta[1], value: meta[2].trim() })
      else body.body.push({ kind: 'para', text: line })
      continue
    }
    const act = line.match(ACTION_RE)
    if (act) {
      push({ kind: 'action', text: act[1] })
      continue
    }
    const li = line.match(LIST_RE)
    if (li) {
      const body = ensure().body
      const last = body[body.length - 1]
      if (last && last.kind === 'list') last.items!.push(li[1])
      else body.push({ kind: 'list', text: '', items: [li[1]] })
      continue
    }
    const q = line.match(QUOTE_RE)
    if (q) {
      push({ kind: 'quote', text: q[1] })
      continue
    }
    const d = line.match(DIALOGUE_RE)
    if (d && !NON_SPEAKER.has(d[1]) && d[1].length <= 8) {
      push({ kind: 'dialogue', text: d[2], who: d[1] })
      continue
    }
    push({ kind: 'para', text: line })
  }
  return nodes
}

export interface Part {
  text: string
  cls?: 'b' | 'i' | 'code' | 'at'
}

const INLINE_RE = /(\*\*[^*]+\*\*|\*[^*\n]+\*|`[^`]+`|@[\w\u4e00-\u9fa5]+)/g

/** 行内格式：**加粗** / *斜体* / `代码` / @引用 —— 不引入 v-html，逐段渲染 */
export function inlineParts(text: string): Part[] {
  const out: Part[] = []
  let last = 0
  for (const m of (text || '').matchAll(INLINE_RE)) {
    const at = m.index ?? 0
    if (at > last) out.push({ text: text.slice(last, at) })
    const token = m[0]
    if (token.startsWith('**')) out.push({ text: token.slice(2, -2), cls: 'b' })
    else if (token.startsWith('`')) out.push({ text: token.slice(1, -1), cls: 'code' })
    else if (token.startsWith('@')) out.push({ text: token, cls: 'at' })
    else out.push({ text: token.slice(1, -1), cls: 'i' })
    last = at + token.length
  }
  if (last < text.length) out.push({ text: text.slice(last) })
  return out
}
