<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NButton, NInputNumber, NModal, NRadioButton, NRadioGroup } from 'naive-ui'

const props = defineProps<{
  show: boolean
  totalEpisodes: number
  submitting: boolean
  reparse: boolean
}>()
const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'submit', payload: { episode_start: number; episode_end?: number; episode_count?: number }): void
}>()

/* 解析范围双模式：count=起始+数量，range=起止集数（两种模式等价） */
const parseMode = ref<'count' | 'range'>('count')
const episodeStartInput = ref(1)
const episodeCountInput = ref(1)
const episodeEndInput = ref(1)

watch(
  () => props.totalEpisodes,
  (v) => {
    const total = v || 1
    episodeCountInput.value = total
    episodeEndInput.value = total
  },
  { immediate: true },
)
/* 模式切换时按等价关系同步另一模式的边界值 */
watch(parseMode, (mode) => {
  const start = episodeStartInput.value || 1
  if (mode === 'range') {
    episodeEndInput.value = Math.min(props.totalEpisodes || start, start + episodeCountInput.value - 1)
  } else {
    episodeCountInput.value = Math.max(1, (episodeEndInput.value || start) - start + 1)
  }
})
/* 解析范围归一化：range 模式输出 episode_end，count 模式输出 episode_count（二选一） */
const parseRange = computed(() => {
  const start = Math.min(Math.max(1, episodeStartInput.value || 1), props.totalEpisodes || 1)
  if (parseMode.value === 'range') {
    const end = Math.min(Math.max(start, episodeEndInput.value || start), props.totalEpisodes || start)
    return { episode_start: start, episode_end: end }
  }
  const count = Math.max(1, episodeCountInput.value || 1)
  return { episode_start: start, episode_count: count }
})
/* 快捷范围 chips（全部 / 前 5 集） */
function applyQuickRange(kind: 'all' | 'first5') {
  const total = props.totalEpisodes || 1
  episodeStartInput.value = 1
  const n = kind === 'all' ? total : Math.min(5, total)
  episodeCountInput.value = n
  episodeEndInput.value = n
}
const quickRangeActive = computed(() => {
  const total = props.totalEpisodes || 0
  const start = episodeStartInput.value || 1
  const count =
    parseMode.value === 'count'
      ? episodeCountInput.value || 0
      : (episodeEndInput.value || 0) - start + 1
  if (start === 1 && count === total) return 'all'
  if (start === 1 && count === 5) return 'first5'
  return ''
})
const rangeCount = computed(() =>
  parseMode.value === 'count'
    ? episodeCountInput.value || 0
    : (episodeEndInput.value || 1) - (episodeStartInput.value || 1) + 1,
)

function handleSubmit() {
  emit('submit', parseRange.value)
}
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    :title="reparse ? '重新解析' : '开始解析'"
    class="parse-modal"
    style="width: 520px; max-width: calc(100vw - 32px)"
    :mask-closable="false"
    @update:show="emit('update:show', $event)"
  >
    <p v-if="reparse" class="parse-warn">
      将基于当前剧本版本生成候选解析版本，确认切换前当前分镜与产物不受影响。
    </p>
    <div class="parse-mode-row">
      <NRadioGroup v-model:value="parseMode" size="small">
        <NRadioButton value="count" label="起始 + 数量" />
        <NRadioButton value="range" label="起止集数" />
      </NRadioGroup>
    </div>
    <div class="quick-chips">
      <button
        class="quick-chip"
        :class="{ active: quickRangeActive === 'all' }"
        @click="applyQuickRange('all')"
      >
        全部 {{ totalEpisodes }} 集
      </button>
      <button
        v-if="(totalEpisodes || 0) > 5"
        class="quick-chip"
        :class="{ active: quickRangeActive === 'first5' }"
        @click="applyQuickRange('first5')"
      >
        前 5 集
      </button>
      <span class="quick-chip static">自定义</span>
    </div>
    <div class="parse-range-form">
      <span class="form-label">起始集</span>
      <NInputNumber
        v-model:value="episodeStartInput"
        size="small"
        :min="1"
        :max="totalEpisodes || 1"
        :show-button="false"
        style="width: 96px"
      />
      <template v-if="parseMode === 'count'">
        <span class="form-label">解析数量</span>
        <NInputNumber
          v-model:value="episodeCountInput"
          size="small"
          :min="1"
          :max="Math.max(1, (totalEpisodes || 1) - (episodeStartInput || 1) + 1)"
          :show-button="false"
          style="width: 96px"
        />
      </template>
      <template v-else>
        <span class="form-label">截止集</span>
        <NInputNumber
          v-model:value="episodeEndInput"
          size="small"
          :min="episodeStartInput || 1"
          :max="totalEpisodes || 1"
          :show-button="false"
          style="width: 96px"
        />
      </template>
    </div>
    <div class="parse-range-hint">共 {{ rangeCount }} 集 / 总 {{ totalEpisodes }} 集</div>
    <template #footer>
      <div class="modal-footer">
        <NButton size="small" @click="emit('update:show', false)">取消</NButton>
        <NButton size="small" type="primary" :loading="submitting" @click="handleSubmit()">
          {{ reparse ? '提交重新解析' : '提交解析' }}
        </NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.parse-warn {
  margin: 0 0 12px;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(240, 160, 32, 0.1);
  color: #b8860b;
  font-size: 12px;
  line-height: 1.6;
}
.parse-mode-row {
  margin-bottom: 10px;
}
.quick-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}
.quick-chip {
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid rgba(148, 163, 184, 0.35);
  background: transparent;
  color: #64748b;
  font-size: 12px;
  cursor: pointer;
}
.quick-chip.active {
  border-color: var(--brand-primary, #6366f1);
  color: var(--brand-primary, #6366f1);
  background: rgba(99, 102, 241, 0.08);
}
.quick-chip.static {
  cursor: default;
  opacity: 0.7;
}
.parse-range-form {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.form-label {
  font-size: 12px;
  color: #64748b;
}
.parse-range-hint {
  margin-top: 10px;
  font-size: 12px;
  color: #94a3b8;
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
