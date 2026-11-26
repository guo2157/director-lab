<script setup lang="ts">
/**
 * 消息输入区：多行输入 + 内嵌发送按钮 + 多张参考图上传。
 * 参考图用于「多图参考生图 / 多图参考生视频」：支持点击选择、拖拽、粘贴，最多 9 张。
 * 键盘可访问：输入框与按钮均有 aria-label，焦点态沿用 --focus-ring 语义（不写死颜色）。
 */
import { computed, ref } from 'vue'
import { NButton, NIcon, NInput, useMessage } from 'naive-ui'
import { CloseOutline, ImageOutline } from '@vicons/ionicons5'
import {
  agentAttachmentSrc,
  MAX_ATTACHMENT_MB,
  MAX_ATTACHMENTS,
  uploadAttachments,
  type AgentAttachment,
} from '@/api/agent'
import { ApiError } from '@/api/client'

const inputRef = ref<{ focus: () => void } | null>(null)
/** 供父级（快捷提示词）把焦点交还输入框 */
defineExpose({ focus: () => inputRef.value?.focus() })

const props = withDefaults(
  defineProps<{
    modelValue: string
    /** 当前会话 ID：参考图按会话隔离存储，未选择会话时不能上传 */
    sessionId?: string | null
    attachments?: AgentAttachment[]
    disabled?: boolean
    loading?: boolean
    placeholder?: string
  }>(),
  {
    sessionId: null,
    attachments: () => [],
    disabled: false,
    loading: false,
    placeholder: '描述你想推进的创作任务…',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'update:attachments': [value: AgentAttachment[]]
  send: []
}>()

const message = useMessage()
const fileInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
const dragActive = ref(false)
/** 加载失败的缩略图：仅影响展示，不阻塞发送 */
const brokenIds = ref<Set<string>>(new Set())

const canSend = () =>
  !props.disabled &&
  !props.loading &&
  !uploading.value &&
  (props.modelValue.trim().length > 0 || props.attachments.length > 0)

const hint = computed(() => {
  if (!props.sessionId) return '选择会话后可上传参考图'
  if (props.attachments.length === 0) return `可上传多张参考图（最多 ${MAX_ATTACHMENTS} 张）`
  return `${props.attachments.length}/${MAX_ATTACHMENTS} 张参考图`
})

function pickFiles() {
  if (props.disabled || uploading.value || !props.sessionId) return
  fileInput.value?.click()
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  addFiles(Array.from(input.files ?? []))
  input.value = ''
}

function onPaste(event: ClipboardEvent) {
  const files = Array.from(event.clipboardData?.files ?? [])
  if (files.length === 0) return
  event.preventDefault()
  addFiles(files)
}

function onDrop(event: DragEvent) {
  dragActive.value = false
  const files = Array.from(event.dataTransfer?.files ?? [])
  if (files.length === 0) return
  event.preventDefault()
  addFiles(files)
}

async function addFiles(files: File[]) {
  if (!props.sessionId) {
    message.warning('请先选择或创建会话')
    return
  }
  const room = MAX_ATTACHMENTS - props.attachments.length
  if (room <= 0) {
    message.warning(`最多上传 ${MAX_ATTACHMENTS} 张参考图`)
    return
  }
  const images = files.filter((f) => f.type.startsWith('image/'))
  if (images.length === 0) {
    message.warning('仅支持图片文件（PNG / JPG / WEBP / GIF）')
    return
  }
  const oversize = images.find((f) => f.size > MAX_ATTACHMENT_MB * 1024 * 1024)
  if (oversize) {
    message.warning(`单张图片不能超过 ${MAX_ATTACHMENT_MB}MB：${oversize.name}`)
    return
  }
  const batch = images.slice(0, room)
  if (images.length > room) {
    message.warning(`最多 ${MAX_ATTACHMENTS} 张，已忽略多余的 ${images.length - room} 张`)
  }
  uploading.value = true
  try {
    const items = await uploadAttachments(props.sessionId, batch)
    emit('update:attachments', [...props.attachments, ...items])
  } catch (e) {
    const text = e instanceof ApiError ? e.message : String(e)
    message.error(`参考图上传失败：${text}`)
  } finally {
    uploading.value = false
  }
}

function removeAttachment(id: string) {
  emit(
    'update:attachments',
    props.attachments.filter((a) => a.id !== id),
  )
}

function markBroken(id: string) {
  brokenIds.value = new Set(brokenIds.value).add(id)
}

function send() {
  if (!canSend()) return
  emit('send')
}
</script>

<template>
  <div class="chat-input">
    <div
      v-if="attachments.length > 0"
      class="attach-strip"
      aria-label="已上传的参考图"
    >
      <div
        v-for="(item, index) in attachments"
        :key="item.id"
        class="attach-thumb"
        :title="item.name"
      >
        <img
          v-if="!brokenIds.has(item.id)"
          :src="agentAttachmentSrc(item.url)"
          :alt="item.name || `参考图 ${index + 1}`"
          @error="markBroken(item.id)"
        />
        <span v-else class="attach-broken">加载失败</span>
        <span class="attach-index">{{ index + 1 }}</span>
        <button
          type="button"
          class="attach-remove"
          :aria-label="`移除参考图 ${index + 1}`"
          :disabled="disabled"
          @click="removeAttachment(item.id)"
        >
          <NIcon :size="12"><CloseOutline /></NIcon>
        </button>
      </div>
      <div v-if="uploading" class="attach-thumb attach-loading">上传中…</div>
    </div>

    <div
      class="composer"
      :class="{ 'drag-active': dragActive }"
      @paste="onPaste"
      @dragover.prevent="dragActive = true"
      @dragleave.prevent="dragActive = false"
      @drop="onDrop"
    >
      <NInput
        ref="inputRef"
        :value="modelValue"
        type="textarea"
        :rows="2"
        :placeholder="placeholder"
        :disabled="disabled"
        :input-props="{ 'aria-label': '消息输入框' }"
        @update:value="(v: string) => emit('update:modelValue', v)"
        @keydown.enter.exact.prevent="send"
      />
      <NButton
        class="composer-send"
        type="primary"
        aria-label="发送消息"
        :loading="loading"
        :disabled="!canSend()"
        @click="send"
      >
        发送
      </NButton>
    </div>

    <div class="composer-tools">
      <NButton
        quaternary
        circle
        size="small"
        aria-label="上传参考图"
        :disabled="disabled || uploading || !sessionId || attachments.length >= MAX_ATTACHMENTS"
        :loading="uploading"
        @click="pickFiles"
      >
        <template #icon>
          <NIcon><ImageOutline /></NIcon>
        </template>
      </NButton>
      <span class="composer-hint">{{ hint }}</span>
    </div>

    <div class="composer-foot">
      <span v-if="disabled && !loading" class="composer-hint">选择或新建会话后即可输入</span>
      <span v-else class="composer-hint">
        <kbd>Enter</kbd> 发送 · <kbd>Shift</kbd> + <kbd>Enter</kbd> 换行
      </span>
    </div>

    <input
      ref="fileInput"
      class="file-input"
      type="file"
      accept="image/png,image/jpeg,image/webp,image/gif"
      multiple
      tabindex="-1"
      aria-hidden="true"
      @change="onFileChange"
    />
  </div>
</template>

<style scoped>
.chat-input {
  display: block;
  padding: var(--sp-3) var(--sp-4) var(--sp-2);
  border-top: 1px solid var(--border-subtle);
  flex-shrink: 0;
  background: var(--bg-panel);
}

/* 参考图缩略图条：横向滚动，不挤压输入区高度 */
.attach-strip {
  display: flex;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
  overflow-x: auto;
  padding-bottom: 2px;
}

.attach-thumb {
  position: relative;
  width: 64px;
  height: 64px;
  flex: 0 0 auto;
  border-radius: var(--radius);
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  background: var(--bg-elevated);
}

.attach-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.attach-broken,
.attach-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  font-size: 11px;
  color: var(--text-tertiary);
}

.attach-index {
  position: absolute;
  left: 2px;
  bottom: 2px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.6);
  color: #fff;
  font-size: 10px;
  line-height: 16px;
  text-align: center;
}

.attach-remove {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 18px;
  height: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.6);
  color: #fff;
  cursor: pointer;
}

.attach-remove:hover {
  background: rgba(0, 0, 0, 0.8);
}

.attach-remove:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 1px;
}

/* 输入框与发送按钮合为一体：按钮内嵌在输入框右下角，不再单独占一行 */
.composer {
  position: relative;
  display: block;
  border-radius: var(--radius-lg);
  transition: box-shadow var(--transition-fast);
}

.composer.drag-active {
  box-shadow: 0 0 0 2px var(--brand-hover);
}

.composer :deep(.n-input) {
  border-radius: var(--radius-lg);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

/* 聚焦：组件自身边框提亮 + 一圈极淡品牌光晕，替代被移除的内嵌 outline */
.composer :deep(.n-input.n-input--focus) {
  border-color: var(--brand-primary);
  box-shadow: 0 0 0 3px var(--brand-glow);
}

.composer :deep(.n-input .n-input__textarea) {
  font-size: 14px;
  line-height: 1.7;
}

/* 让输入文字避开右下角的发送按钮，避免被遮挡 */
.composer :deep(.n-input .n-input__textarea-el) {
  padding-right: 92px;
}

.composer-send {
  position: absolute;
  right: var(--sp-2);
  bottom: var(--sp-2);
  z-index: 2;
  border-radius: var(--radius) !important;
  height: 32px !important;
  min-width: 58px !important;
  font-weight: 500;
  transition: background var(--transition-fast);
}

.composer-send:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}

.composer-tools {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  margin-top: var(--sp-1);
}

/* 键位提示：说明「Enter 发送」，不占用输入区视觉重心 */
.composer-foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--sp-2);
  min-height: 18px;
  padding: var(--sp-1) var(--sp-1) 0;
}

.composer-hint {
  font-size: 11px;
  line-height: 16px;
  color: var(--text-tertiary);
}

.composer-hint kbd {
  padding: 0 4px;
  font-family: var(--font-ui);
  font-size: 10px;
  color: var(--text-secondary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.file-input {
  display: none;
}

@media (max-width: 768px) {
  .chat-input {
    padding: var(--sp-2) var(--sp-3) var(--sp-2);
  }
  .composer :deep(.n-input .n-input__textarea-el) {
    padding-right: 82px;
  }
  .composer-foot {
    display: none;
  }
}
</style>
