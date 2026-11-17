<script setup lang="ts">
/**
 * 上传替换：选一张图直接替换当前生效版本。
 * 名称与文件位置不变，引用该资产的镜头会被标记待重做——这条影响写在弹窗里，不藏在文档里。
 */
import { ref, watch } from 'vue'
import { NAlert, NButton, NModal } from 'naive-ui'
import type { Asset } from '@/types/asset'

const props = withDefaults(
  defineProps<{
    show: boolean
    target: Asset | null
    busy?: boolean
  }>(),
  { busy: false },
)

const emit = defineEmits<{
  'update:show': [boolean]
  confirm: [File]
}>()

const fileObj = ref<File | null>(null)
const preview = ref<string | null>(null)
const inputRef = ref<HTMLInputElement | null>(null)

function revoke() {
  if (preview.value) {
    URL.revokeObjectURL(preview.value)
    preview.value = null
  }
}

/** 每次打开都是新的一次替换，不能让上一次选择的文件残留 */
watch(
  () => props.show,
  (show) => {
    if (show) {
      fileObj.value = null
      revoke()
    }
  },
)

function pickFile(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  fileObj.value = file
  revoke()
  preview.value = file.type.startsWith('image/') ? URL.createObjectURL(file) : null
  input.value = ''
}

function triggerSelect() {
  inputRef.value?.click()
}

function confirm() {
  if (!fileObj.value) return
  emit('confirm', fileObj.value)
}
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    title="上传替换资产"
    :style="{ width: 'min(640px, 92vw)' }"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <div v-if="target" class="replace-body">
      <div class="replace-label">{{ target.name || target.id }}</div>
      <div class="replace-preview">
        <input ref="inputRef" type="file" accept="image/*,video/*" class="replace-input" @change="pickFile" />
        <template v-if="preview">
          <img :src="preview" alt="" />
          <n-button size="tiny" secondary class="replace-repick" @click="triggerSelect">重新选择</n-button>
        </template>
        <div v-else class="replace-pick">
          <n-button size="small" secondary @click="triggerSelect">选择文件</n-button>
        </div>
      </div>
      <div v-if="fileObj" class="replace-file">已选择：{{ fileObj.name }}</div>
      <n-alert type="info" :bordered="false">
        上传的图片将直接替换当前图片内容，资产名称与文件位置保持不变。引用该资产的镜头会被标记为待重做，但不会自动触发任务。
      </n-alert>
    </div>

    <template #footer>
      <div class="replace-footer">
        <n-button size="small" @click="emit('update:show', false)">取消</n-button>
        <n-button size="small" type="primary" :loading="busy" :disabled="!fileObj" @click="confirm">
          确认替换
        </n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.replace-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.replace-label {
  font-size: 12px;
  color: var(--text-secondary);
}
.replace-preview {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 190px;
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  overflow: hidden;
}
.replace-input {
  display: none;
}
.replace-preview img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}
.replace-pick {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
}
.replace-repick {
  position: absolute;
  bottom: var(--sp-2);
  right: var(--sp-2);
}
.replace-file {
  font-size: 12px;
  color: var(--text-secondary);
}
.replace-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
}
</style>
