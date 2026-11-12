<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NCard, NEmpty, NInput, NInputNumber, NSpace, NSpin, NTag, useMessage } from 'naive-ui'
import { continueEpisode, getNextEpisodeNumber, getSeries, previewContext } from '@/api/series'
import { getTask } from '@/api/tasks'
import type { ContextPreview, ContinueResult } from '@/types/series'

/**
 * 续写页：Series -> Episode 的创作入口。
 * 与单集生产链路（project/context_manager）的边界：本页仅负责调用系列级续写接口，
 * 生成产物落到 series_script_versions 的待审核版本，不触碰既有 Project 生产数据结构。
 */
const route = useRoute()
const router = useRouter()
const message = useMessage()

const seriesId = computed(() => String(route.params.id ?? ''))
const routeEpisodeId = computed(() => (route.query.episodeId ? String(route.query.episodeId) : ''))

const seriesName = ref('')
const nextNumber = ref<number | null>(null)
const loadingSeries = ref(true)
const loadError = ref('')

const instruction = ref('')
const direction = ref('')
const constraints = ref('')
const targetEpisodeNumber = ref<number | null>(null)
const targetTitle = ref('')

const previewing = ref(false)
const preview = ref<ContextPreview | null>(null)
const previewError = ref('')

const submitting = ref(false)
const submitError = ref('')
const continueResult = ref<ContinueResult | null>(null)

const taskId = ref('')
const taskStatus = ref('')
const taskProgress = ref(0)
const taskStep = ref('')
const taskMessageText = ref('')
const taskErrorMsg = ref('')
const taskRunning = computed(() => ['queued', 'running', 'pending'].includes(taskStatus.value))
let timer: number | null = null

const nextNumberHint = computed(() => (nextNumber.value ? `默认第 ${nextNumber.value} 集` : '留空自动取下一集'))

async function loadSeries() {
  loadingSeries.value = true
  loadError.value = ''
  try {
    const detail = await getSeries(seriesId.value)
    seriesName.value = detail.name
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '系列信息加载失败'
  } finally {
    loadingSeries.value = false
  }
  try {
    const res = await getNextEpisodeNumber(seriesId.value)
    nextNumber.value = res.next_episode_number
    if (targetEpisodeNumber.value === null) targetEpisodeNumber.value = res.next_episode_number
  } catch {
    nextNumber.value = null
  }
}

function effectiveTargetNumber(): number {
  return targetEpisodeNumber.value ?? nextNumber.value ?? 1
}

async function doPreview() {
  if (!instruction.value.trim()) {
    message.warning('请先填写续写要求')
    return
  }
  previewing.value = true
  previewError.value = ''
  try {
    preview.value = await previewContext(seriesId.value, {
      target_episode_number: effectiveTargetNumber(),
      user_instruction: instruction.value.trim(),
    })
  } catch (e) {
    preview.value = null
    previewError.value = e instanceof Error ? e.message : '上下文预览失败'
  } finally {
    previewing.value = false
  }
}

function stopPolling() {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
}

async function pollTask() {
  if (!taskId.value) return
  try {
    const detail = await getTask(taskId.value)
    taskStatus.value = detail.status ?? ''
    taskProgress.value = detail.progress ?? 0
    taskStep.value = detail.current_step ?? ''
    taskMessageText.value = detail.current_message ?? ''
    taskErrorMsg.value = detail.error_msg ?? ''
    if (['completed', 'failed', 'cancelled'].includes(taskStatus.value)) {
      stopPolling()
      if (taskStatus.value === 'completed') message.success('续写任务已完成，可进入审核页查看剧本')
      if (taskStatus.value === 'failed') message.error(taskErrorMsg.value || '续写任务失败')
    }
  } catch (e) {
    stopPolling()
    taskErrorMsg.value = e instanceof Error ? e.message : '任务状态查询失败'
  }
}

async function doContinue() {
  if (!instruction.value.trim()) {
    message.warning('请先填写续写要求')
    return
  }
  submitting.value = true
  submitError.value = ''
  try {
    const res = await continueEpisode(seriesId.value, {
      user_instruction: [
        instruction.value.trim(),
        direction.value.trim() ? `续写方向：${direction.value.trim()}` : '',
        constraints.value.trim() ? `创作约束：${constraints.value.trim()}` : '',
      ]
        .filter(Boolean)
        .join('\n'),
      target_episode_number: targetEpisodeNumber.value,
      title: targetTitle.value.trim() || null,
    })
    continueResult.value = res
    taskId.value = res.task_id
    taskStatus.value = res.status
    taskProgress.value = 0
    stopPolling()
    timer = window.setInterval(pollTask, 2500)
    await pollTask()
  } catch (e) {
    submitError.value = e instanceof Error ? e.message : '续写任务创建失败'
  } finally {
    submitting.value = false
  }
}

function goBack() {
  void router.push({ name: 'series-detail', params: { id: seriesId.value } })
}

function goReview(episodeId: string) {
  void router.push({ name: 'episode-review', params: { id: episodeId } })
}

function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

onMounted(async () => {
  await loadSeries()
})

onBeforeUnmount(() => {
  stopPolling()
})
</script>

<template>
  <div class="series-continue-page">
    <div class="page-head">
      <n-button quaternary size="small" @click="goBack">← 返回系列</n-button>
      <h1 class="page-title">续写剧集<span v-if="seriesName" class="series-name">{{ seriesName }}</span></h1>
      <p class="hint">先构建上下文预览确认引用范围，再创建异步生成任务；生成结果进入待审核，不会自动改写已确认剧情。</p>
    </div>

    <div v-if="loadingSeries" class="state-box"><n-spin size="small" /><span>加载系列信息…</span></div>
    <div v-else-if="loadError" class="state-box error">
      <span>{{ loadError }}</span>
      <n-button size="small" @click="loadSeries">重试</n-button>
    </div>

    <template v-else>
      <n-card size="small" title="续写要求" class="section-card">
        <n-space vertical size="small">
          <span class="field-label">续写要求（必填）</span>
          <n-input v-model:value="instruction" type="textarea" :rows="3" placeholder="例如：本集收束记忆线索，并在结尾留下新的悬念" />
          <span class="field-label">叙事方向（可选）</span>
          <n-input v-model:value="direction" placeholder="例如：悬疑推进，节奏偏快" />
          <span class="field-label">硬性约束（可选）</span>
          <n-input v-model:value="constraints" placeholder="例如：不新增主要角色，单集时长 3 分钟" />
          <span class="field-label">剧集标题（可选）</span>
          <n-input v-model:value="targetTitle" placeholder="留空则由系统按系列设定生成" />
          <span class="field-label">目标集号</span>
          <n-space align="center">
            <n-input-number v-model:value="targetEpisodeNumber" :min="1" clearable :placeholder="nextNumberHint" style="width: 200px" />
            <span class="hint">{{ nextNumberHint }}</span>
          </n-space>
          <n-space>
            <n-button type="primary" :loading="previewing" :disabled="!instruction.trim()" @click="doPreview">构建上下文预览</n-button>
            <n-button type="primary" ghost :loading="submitting" :disabled="!instruction.trim() || taskRunning" @click="doContinue">开始续写</n-button>
            <n-button v-if="routeEpisodeId" quaternary @click="goReview(routeEpisodeId)">查看该集审核</n-button>
          </n-space>
        </n-space>
      </n-card>

      <n-card v-if="previewError" size="small" title="上下文预览" class="section-card">
        <div class="state-box error"><span>{{ previewError }}</span></div>
      </n-card>

      <n-card v-else-if="preview" size="small" title="上下文预览" class="section-card">
        <div class="hash-row">
          <n-tag size="small" type="info">目标第 {{ effectiveTargetNumber() }} 集</n-tag>
          <span class="hint">context_hash: {{ preview.context_hash }}</span>
        </div>
        <div v-if="preview.warnings?.length" class="warn-block">
          <div class="block-title">上下文警告</div>
          <ul class="plain-list"><li v-for="(w, i) in preview.warnings" :key="i">{{ typeof w === 'string' ? w : formatJson(w) }}</li></ul>
        </div>
        <div v-if="preview.missing?.length" class="warn-block">
          <div class="block-title">缺失信息</div>
          <ul class="plain-list"><li v-for="(m, i) in preview.missing" :key="i">{{ typeof m === 'string' ? m : formatJson(m) }}</li></ul>
        </div>
        <n-space vertical size="small">
          <div>
            <div class="block-title">系列设定 Series Bible</div>
            <pre class="json-block">{{ formatJson(preview.series_bible) }}</pre>
          </div>
          <div>
            <div class="block-title">连续性状态</div>
            <pre class="json-block">{{ formatJson(preview.continuity) }}</pre>
          </div>
          <div>
            <div class="block-title">上一集信息</div>
            <pre class="json-block">{{ formatJson(preview.previous_episode) }}</pre>
          </div>
          <div>
            <div class="block-title">未回收剧情线</div>
            <pre class="json-block">{{ formatJson(preview.plot_threads) }}</pre>
          </div>
          <div>
            <div class="block-title">历史剧情事件</div>
            <pre class="json-block">{{ formatJson(preview.history_events) }}</pre>
          </div>
          <div>
            <div class="block-title">共享资产</div>
            <pre class="json-block">{{ formatJson(preview.assets) }}</pre>
          </div>
        </n-space>
      </n-card>

      <n-card v-if="submitError" size="small" title="续写任务" class="section-card">
        <div class="state-box error"><span>{{ submitError }}</span></div>
      </n-card>

      <n-card v-else-if="continueResult" size="small" title="续写任务" class="section-card">
        <n-space vertical size="small">
          <div class="hash-row">
            <n-tag size="small">第 {{ continueResult.episode_number }} 集</n-tag>
            <n-tag size="small" :type="taskStatus === 'completed' ? 'success' : taskStatus === 'failed' ? 'error' : 'info'">{{ taskStatus || continueResult.status }}</n-tag>
            <span class="hint">task_id: {{ continueResult.task_id }}</span>
          </div>
          <div class="progress-row">
            <span class="hint">进度</span>
            <div class="progress-track"><div class="progress-bar" :style="{ width: (taskProgress || 0) + '%' }"></div></div>
            <span class="hint">{{ taskProgress || 0 }}%</span>
          </div>
          <div v-if="taskStep" class="hint">当前步骤：{{ taskStep }}</div>
          <div v-if="taskMessageText" class="hint">{{ taskMessageText }}</div>
          <div v-if="taskErrorMsg" class="state-box error"><span>{{ taskErrorMsg }}</span></div>
          <n-space>
            <n-button size="small" @click="goReview(continueResult.episode_id)">进入剧本审核</n-button>
            <n-button size="small" quaternary @click="pollTask" :disabled="!taskId">刷新进度</n-button>
          </n-space>
        </n-space>
      </n-card>

      <n-card v-else size="small" title="上下文预览" class="section-card">
        <n-empty description="填写续写要求后可先构建上下文预览" />
      </n-card>
    </template>
  </div>
</template>

<style scoped>
.series-continue-page {
  padding: 24px;
  max-width: 1100px;
  margin: 0 auto;
}
.page-head {
  margin-bottom: 16px;
}
.page-title {
  font-size: 20px;
  margin: 8px 0 4px;
}
.series-name {
  font-size: 13px;
  font-weight: 400;
  color: #888;
  margin-left: 8px;
}
.hint {
  color: #888;
  font-size: 12px;
  margin: 0;
}
.section-card {
  margin-bottom: 16px;
}
.state-box {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px;
  color: #666;
  font-size: 13px;
}
.state-box.error {
  color: #d03050;
}
.field-label {
  font-size: 12px;
  color: #666;
}
.hash-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.block-title {
  font-size: 13px;
  font-weight: 600;
  margin: 8px 0 4px;
}
.json-block {
  background: #fafafa;
  border: 1px solid #eee;
  border-radius: 6px;
  padding: 8px;
  font-size: 12px;
  max-height: 220px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
.warn-block {
  border: 1px solid #eee;
  border-radius: 6px;
  padding: 8px;
  margin-bottom: 8px;
  background: #fffdf5;
}
.plain-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  color: #666;
}
.progress-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.progress-track {
  flex: 1;
  height: 6px;
  background: #eee;
  border-radius: 3px;
  overflow: hidden;
}
.progress-bar {
  height: 100%;
  background: #2080f0;
  transition: width 0.3s ease;
}
</style>
