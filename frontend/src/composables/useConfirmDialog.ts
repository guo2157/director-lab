/**
 * 全局确认框 —— ConfirmDialog 的命令式封装
 *
 * 用法（替代 naive-ui 的 useDialog）：
 *   import { useConfirmDialog } from '@/composables/useConfirmDialog'
 *   const confirm = useConfirmDialog()
 *   confirm({
 *     title: '删除作品',
 *     message: '确定要删除作品「X」吗？删除后不可恢复。',
 *     impact: ['场景 12 个', '镜头 48 个'],
 *     confirmText: '删除',
 *     danger: true,
 *     onConfirm: () => doDelete(),
 *   })
 *
 * 要求：App.vue 中已挂载 <GlobalConfirmDialog />（与 NMessageProvider 同级）。
 */
import { reactive } from 'vue'

export interface ConfirmOptions {
  title: string
  message?: string
  /** 影响范围：逐条列出生效对象（危险操作必填） */
  impact?: string[]
  confirmText?: string
  cancelText?: string
  danger?: boolean
  /** 隐藏取消按钮（纯告知场景，如「知道了」） */
  hideCancel?: boolean
  onConfirm?: () => void | Promise<void>
  onCancel?: () => void
}

interface ConfirmState extends ConfirmOptions {
  show: boolean
  loading: boolean
}

const state = reactive<ConfirmState>({
  show: false,
  loading: false,
  title: '',
  message: '',
  impact: [],
  confirmText: '确认',
  cancelText: '取消',
  danger: true,
  hideCancel: false,
  onConfirm: undefined,
  onCancel: undefined
})

export function useConfirmDialog() {
  function open(options: ConfirmOptions) {
    Object.assign(state, {
      show: true,
      loading: false,
      danger: true,
      hideCancel: false,
      impact: [],
      message: '',
      confirmText: '确认',
      cancelText: '取消',
      onConfirm: undefined,
      onCancel: undefined,
      ...options
    })
  }

  async function handleConfirm() {
    if (!state.onConfirm) {
      state.show = false
      return
    }
    try {
      state.loading = true
      await state.onConfirm()
      state.show = false
    } finally {
      state.loading = false
    }
  }

  function handleCancel() {
    state.show = false
    state.onCancel?.()
  }

  return Object.assign(open, { state, handleConfirm, handleCancel })
}

export type ConfirmDialogController = ReturnType<typeof useConfirmDialog>
