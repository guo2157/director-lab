<script setup lang="ts">
/** 全局确认框挂载点：App.vue 中放置一次，配合 useConfirmDialog() 命令式调用 */
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const controller = useConfirmDialog()
const { state, handleConfirm, handleCancel } = controller
</script>
<template>
  <ConfirmDialog
    :show="state.show"
    :title="state.title"
    :message="state.message"
    :impact="state.impact ?? []"
    :confirm-text="state.confirmText"
    :cancel-text="state.cancelText"
    :danger="state.danger"
    :loading="state.loading"
    :hide-cancel="state.hideCancel"
    @update:show="(v: boolean) => { if (!v) handleCancel() }"
    @confirm="handleConfirm"
    @cancel="handleCancel"
  />
</template>
