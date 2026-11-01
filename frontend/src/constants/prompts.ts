/** 系统级默认提示词常量。
 *
 * 约定：
 * - 负向提示词由本文件统一维护，页面不重复定义，避免多处分叉
 * - 用户未自定义时使用默认模板；用户自定义后优先使用自定义内容
 * - 工作流未声明负向提示词输入能力时不传入、也不展示该字段
 */

/** 通用默认负向提示词模板（图片与视频共用基础项，按模型能力决定是否启用） */
export const DEFAULT_NEGATIVE_PROMPT = [
  'low quality, worst quality, low resolution, blurry, out of focus, pixelated, noisy',
  'compression artifacts, excessive motion blur, overexposed, underexposed, poor lighting',
  'unnatural colors, oversaturated, washed-out colors, excessive sharpening',
  'distorted image, visual artifacts, malformed anatomy, deformed body',
  'unnatural body proportions, deformed face, asymmetrical facial features',
  'malformed hands, malformed feet, extra fingers, missing fingers, extra limbs, missing limbs',
  'duplicated body parts, unnatural pose, unnatural movement, stiff motion, jerky motion',
  'inconsistent motion, temporal inconsistency, flickering, jitter, ghosting, double images',
  'frame instability, warped objects, distorted geometry, inconsistent perspective',
  'inconsistent lighting, inconsistent shadows, floating objects, duplicated objects',
  'disappearing objects, unwanted objects, unexpected background changes',
  'inconsistent character appearance, identity drift, inconsistent facial features',
  'inconsistent clothing, inconsistent hairstyle, inconsistent object shapes',
  'unnatural physical interactions, broken contact between objects, clipping',
  'penetration artifacts, text artifacts, random text, watermark, logo',
  'unwanted borders, unwanted black bars',
].join(', ')

/** 工作流是否真正绑定了负向提示词输入槽位。
 *
 * 与后端判定保持一致：后端以工作流定义中是否声明 negative 参数标记
 * 作为唯一依据，只有真正绑定了该输入的流程传参才会生效；
 * 因此前端也只看参数声明，不看笼统的能力字段，避免显示可填却不生效。
 */
export function workflowSupportsNegative(workflow?: {
  parsed_schema?: { params?: Array<{ key?: string }> } | null
  definition?: Record<string, unknown> | null
  capabilities?: Record<string, unknown> | null
} | null): boolean {
  if (!workflow) return false
  const params = workflow.parsed_schema?.params
  if (Array.isArray(params) && params.some((p) => p?.key === 'negative')) return true
  const def = workflow.definition
  if (def && typeof def === 'object') {
    const text = JSON.stringify(def)
    if (text.includes('$negative.')) return true
  }
  const caps = workflow.capabilities
  if (caps && typeof caps === 'object') {
    const keys = ['negative_slots', 'negative_prompt_slots', 'negative_slot']
    for (const k of keys) {
      const v = (caps as Record<string, unknown>)[k]
      if (Array.isArray(v) && v.length > 0) return true
      if (typeof v === 'string' && v.length > 0) return true
    }
  }
  return false
}
