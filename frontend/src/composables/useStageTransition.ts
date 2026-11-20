import { computed, ref } from 'vue'
import { getStageReadiness, type StageBlocker, type StageKey } from '@/api/stageReadiness'

/**
 * 阶段跳转守卫：封装「点击 → 查就绪 → 未就绪提示 / 已就绪弹窗 → 确认时再查一次 → 跳转」。
 *
 * 为什么抽成组合式函数：三个页面（资产库 / 分镜图 / 分镜看板）的交互要求完全一致
 * （任务书 §7），各写一遍必然出现「有的页面确认前查、有的页面不查」「有的页面
 * loading 防重复、有的不防」这类不一致。
 *
 * 关键约束：
 * - 就绪状态只来自后端，前端不自己算 ready（不依赖本地缓存的旧结论）；
 * - 确认时**再查一次**：弹窗打开期间用户可能返回上一步改了图；
 * - 查询与确认期间置 loading，禁止重复提交与重复跳转。
 */
export function useStageTransition(opts: {
  projectId: () => string
  stage: StageKey
  /** 跳转目标路由 */
  to: (projectId: string) => string
  /** 未就绪时的提示前缀，如「分镜图片尚未全部生成完成」 */
  notReadyTitle: string
  /** 已就绪时的弹窗标题，如「确认进入分镜看板？」 */
  confirmTitle: string
  /** 已就绪时的确认描述（多行用 \n） */
  confirmDescription: string
  /** 确认按钮文案 */
  confirmText: string
  /** 未就绪时的 message 提示（由页面注入，避免这里依赖 naive-ui） */
  notify: {
    warning: (msg: string) => void
    error: (msg: string) => void
  }
  /** 路由跳转（由页面注入 useRouter） */
  push: (path: string) => void
}) {
  const visible = ref(false)
  const loading = ref(false)
  const readiness = ref<{
    ready: boolean
    total: number
    done: number
    blockers: StageBlocker[]
  }>({ ready: false, total: 0, done: 0, blockers: [] })

  /** 未就绪时的完整提示文案：进度 + 待完成清单 */
  function notReadyMessage(total: number, done: number, blockers: StageBlocker[]): string {
    const lines = [`${opts.notReadyTitle}`, `当前进度：${done}/${total}`]
    if (blockers.length) {
      lines.push('待完成：')
      for (const b of blockers.slice(0, 8)) lines.push(`- ${b.name}`)
      if (blockers.length > 8) lines.push(`- 等 ${blockers.length - 8} 项`)
    }
    return lines.join('\n')
  }

  /** 点击跳转按钮：先查就绪，未就绪直接提示并中止 */
  async function request() {
    if (loading.value) return
    loading.value = true
    try {
      const data = await getStageReadiness(opts.projectId(), opts.stage)
      if (!data.ready) {
        opts.notify.warning(notReadyMessage(data.total, data.done, data.blockers))
        return
      }
      readiness.value = {
        ready: data.ready,
        total: data.total,
        done: data.done,
        blockers: data.blockers,
      }
      visible.value = true
    } catch (e: any) {
      opts.notify.error(`就绪检查失败：${e?.message ?? e}`)
    } finally {
      loading.value = false
    }
  }

  /** 弹窗确认：再查一次后端，通过后才跳转（防止弹窗期间状态变化） */
  async function confirm() {
    if (loading.value) return
    loading.value = true
    try {
      const data = await getStageReadiness(opts.projectId(), opts.stage)
      if (!data.ready) {
        // 状态已变化：更新弹窗内容为最新阻断原因，不跳转
        readiness.value = {
          ready: data.ready,
          total: data.total,
          done: data.done,
          blockers: data.blockers,
        }
        opts.notify.warning(notReadyMessage(data.total, data.done, data.blockers))
        return
      }
      visible.value = false
      opts.push(opts.to(opts.projectId()))
    } catch (e: any) {
      opts.notify.error(`就绪检查失败：${e?.message ?? e}`)
    } finally {
      loading.value = false
    }
  }

  function cancel() {
    visible.value = false
  }

  /** 弹窗可用的阻断列表：已就绪时为空 */
  const blockers = computed(() => readiness.value.blockers)

  return {
    visible,
    loading,
    readiness,
    blockers,
    request,
    confirm,
    cancel,
    confirmTitle: opts.confirmTitle,
    confirmDescription: opts.confirmDescription,
    confirmText: opts.confirmText,
  }
}
