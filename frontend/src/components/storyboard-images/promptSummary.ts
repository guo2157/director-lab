/**
 * 分镜图提示词解构。
 *
 * 后端 compile_storyboard_image 产出的是「结构化段落」提示词：
 *   【任务】【参考图职责】【美术风格】【目标画面】【必须保持】【允许变化】【禁止】【输出】
 * 这些段落对图像模型有用（任务类型、参考图分工、不变量、负向约束），
 * 但对人来说是噪声——尤其参考图职责在「参考图」标签页已经有了。
 *
 * 卡片上只展示【目标画面】正文；其余段落聚合成一行元信息（任务类型 / 参考图数量 / 字数），
 * 全文仍然完整保留，在详情弹窗里查看与编辑。
 */

export interface PromptDigest {
  /** 【任务】原文，如「多参考图人物一致性生成」 */
  task: string
  /** 【参考图职责】里识别到的参考图数量 */
  refCount: number
  /** 【目标画面】正文：卡片摘要用这一段 */
  body: string
  /** 全文字符数 */
  chars: number
  /** 是否识别到结构化段落（false 说明是自由文本提示词） */
  structured: boolean
}

const SEGMENT_RE = /【([^】]{1,12})】\s*([\s\S]*?)(?=【[^】]{1,12}】|$)/g

/** 把结构化提示词拆成 { 段落名: 内容 } */
export function splitPromptSegments(raw: string): Record<string, string> {
  const text = (raw || '').trim()
  if (!text) return {}
  const out: Record<string, string> = {}
  let matched = false
  for (const m of text.matchAll(SEGMENT_RE)) {
    const key = m[1].trim()
    const value = m[2].trim()
    if (!value) continue
    out[key] = out[key] ? `${out[key]}\n${value}` : value
    matched = true
  }
  if (!matched) return { 正文: text }
  return out
}

/** 卡片摘要：只取目标画面，其余压成元信息 */
export function promptDigest(raw: string): PromptDigest {
  const text = (raw || '').trim()
  const segments = splitPromptSegments(text)
  const structured = !('正文' in segments)

  const body = segments['目标画面'] || segments['正文'] || segments['画面'] || ''
  const task = segments['任务'] || ''
  const refs = segments['参考图职责'] || ''
  const refCount = (refs.match(/图\s*\d+\s*[：:]/g) || []).length

  return {
    task: task.replace(/\s+/g, ' ').slice(0, 24),
    refCount,
    body: body.replace(/\s+/g, ' ').trim(),
    chars: text.length,
    structured,
  }
}
