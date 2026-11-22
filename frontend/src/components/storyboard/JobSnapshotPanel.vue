<script setup lang="ts">
/**
 * 生成快照（只读）：展示某镜头历次生成任务实际冻结的输入。
 *
 * 数据全部来自已落库的快照与 job_input_ref，不做任何推断：
 * - 只在用户点击「加载快照」时请求，不随详情打开自动请求；
 * - 「以此版本为基准重做」只把该任务产出的历史版本作为 base_asset_id 交给既有
 *   重生成工作台；参考图仍按当前镜头定义解析，不复用快照里的参考图版本。
 */
import { computed, ref, watch } from 'vue'
import { NButton, NSelect, NSpin } from 'naive-ui'
import { listJobs } from '@/api/jobs'
import { fetchJobSnapshot } from '@/api/regeneration'
import type { Job } from '@/types/job'

const props = defineProps<{ shotId: string | null }>()
const emit = defineEmits<{
  /** 以该任务产出的历史版本为基准重做（跳转分镜图工作台） */
  rebase: [payload: { shotId: string; assetId: string; version: number | null }]
}>()

interface SnapshotRef {
  slot_param: string
  input_key?: string | null
  asset_id?: string | null
  asset_version?: number | null
  asset_type?: string | null
  role?: string | null
  required?: boolean
  enabled?: boolean
  file_path?: string | null
  /** 资产当前版本；资产已删除时为 null */
  current_version?: number | null
  /** 快照版本与当前版本不一致；资产已删除时为 null（无法判断） */
  version_stale?: boolean | null
}

interface SnapshotView {
  job_id: string
  status: string
  snapshot: {
    mode?: string
    prompt?: { rendered?: string }
    negative_prompt?: { rendered?: string }
    model_params?: Record<string, unknown>
    workflow?: Record<string, unknown>
    refs?: SnapshotRef[]
    snapshot_hash?: string
    created_at?: string
  } | null
  refs: SnapshotRef[]
  frozen?: boolean
  /** 该任务产出的资产版本；早期任务或非出图任务为 null */
  result_asset?: {
    asset_id: string
    asset_type: string
    version: number
    validity: string
  } | null
}

const loading = ref(false)
const loaded = ref(false)
const error = ref('')
const jobs = ref<Job[]>([])
const selectedJobId = ref<string | null>(null)
const detail = ref<SnapshotView | null>(null)
const detailLoading = ref(false)

const jobOptions = computed(() =>
  jobs.value.map((j) => ({
    label: `${j.job_type} · ${j.status} · ${formatTime(j.created_at)}`,
    value: j.id,
  })),
)

const snapshot = computed(() => detail.value?.snapshot ?? null)
const refRows = computed(() => {
  const fromRefs = detail.value?.refs ?? []
  if (fromRefs.length) return fromRefs
  return snapshot.value?.refs ?? []
})
const paramRows = computed(() => Object.entries(snapshot.value?.model_params ?? {}))
const workflowNodes = computed(() => Object.keys(snapshot.value?.workflow ?? {}).length)
const staleCount = computed(
  () => refRows.value.filter((r) => r.version_stale === true).length,
)
const selectedJob = computed(
  () => jobs.value.find((j) => j.id === selectedJobId.value) ?? null,
)
/** 仅分镜图任务支持以历史版本为基准重做，且该任务必须确实产出过资产 */
const rebaseAsset = computed(() => {
  if (selectedJob.value?.job_type !== 'generate_keyframe') return null
  return detail.value?.result_asset ?? null
})

function emitRebase() {
  const asset = rebaseAsset.value
  if (!asset || !props.shotId) return
  emit('rebase', {
    shotId: props.shotId,
    assetId: asset.asset_id,
    version: asset.version ?? null,
  })
}

function formatTime(value?: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return `${d.getMonth() + 1}-${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function loadSnapshot(jobId: string) {
  detailLoading.value = true
  error.value = ''
  try {
    detail.value = (await fetchJobSnapshot(jobId)) as unknown as SnapshotView
  } catch (e: any) {
    detail.value = null
    error.value = e?.message ?? '快照加载失败'
  } finally {
    detailLoading.value = false
  }
}

async function loadJobs() {
  if (!props.shotId) return
  loading.value = true
  error.value = ''
  try {
    const res = await listJobs({ shot_id: props.shotId, page_size: 20 })
    jobs.value = res.items ?? []
    loaded.value = true
    const first = jobs.value[0]?.id ?? null
    selectedJobId.value = first
    if (first) await loadSnapshot(first)
    else detail.value = null
  } catch (e: any) {
    jobs.value = []
    detail.value = null
    error.value = e?.message ?? '任务列表加载失败'
  } finally {
    loading.value = false
  }
}

function handleSelect(jobId: string) {
  selectedJobId.value = jobId
  void loadSnapshot(jobId)
}

// 切换镜头时清空上一个镜头的结果，避免把 A 镜头的快照显示在 B 镜头下
watch(() => props.shotId, () => {
  jobs.value = []
  detail.value = null
  selectedJobId.value = null
  loaded.value = false
  error.value = ''
})
</script>

<template>
  <div class="js-sec">
    <div class="js-head">
      <span class="js-label">生成快照</span>
      <span class="js-hint">（只读，记录当时实际使用的输入）</span>
      <n-button size="tiny" quaternary :loading="loading" @click="loadJobs">
        {{ loaded ? '刷新' : '加载快照' }}
      </n-button>
    </div>

    <div v-if="error" class="js-error">{{ error }}</div>

    <template v-if="loaded && !error">
      <div v-if="!jobs.length" class="js-empty">该镜头暂无生成任务，无快照可查看</div>

      <template v-else>
        <n-select
          size="small"
          :value="selectedJobId"
          :options="jobOptions"
          @update:value="handleSelect"
        />

        <n-spin :show="detailLoading">
          <div v-if="detail && snapshot" class="js-body">
            <div class="js-row">
              <span class="js-key">模式</span>
              <span class="js-val">{{ snapshot.mode || '—' }}</span>
            </div>
            <div class="js-row">
              <span class="js-key">快照哈希</span>
              <span class="js-val js-mono">{{ snapshot.snapshot_hash || '—' }}</span>
            </div>
            <div class="js-row">
              <span class="js-key">冻结于</span>
              <span class="js-val">{{ formatTime(snapshot.created_at) }}</span>
            </div>

            <div class="js-block">
              <div class="js-block-title">提示词</div>
              <div class="js-prompt">{{ snapshot.prompt?.rendered || '—' }}</div>
            </div>

            <div v-if="paramRows.length" class="js-block">
              <div class="js-block-title">模型参数</div>
              <div class="js-params">
                <span v-for="[k, v] in paramRows" :key="k" class="js-param">
                  {{ k }}: <span class="js-mono">{{ String(v) }}</span>
                </span>
              </div>
            </div>

            <div class="js-block">
              <div class="js-block-title">
                参考图
                <span class="js-hint">（{{ refRows.length }} 张，工作流节点 {{ workflowNodes }} 个）</span>
                <span v-if="staleCount" class="js-stale-summary">
                  {{ staleCount }} 张已非当前版本
                </span>
              </div>
              <div v-if="!refRows.length" class="js-empty">快照未记录参考图</div>
              <div v-else class="js-refs">
                <div v-for="(r, i) in refRows" :key="`${r.slot_param}-${i}`" class="js-ref">
                  <span class="js-ref-slot">{{ r.slot_param }}</span>
                  <span class="js-ref-meta">
                    {{ r.asset_type || '未知类型' }}
                    <template v-if="r.asset_version != null"> v{{ r.asset_version }}</template>
                    <template v-if="r.asset_id"> · {{ r.asset_id }}</template>
                  </span>
                  <span v-if="r.required" class="js-tag is-req">必需</span>
                  <span v-if="r.enabled === false" class="js-tag is-off">未注入</span>
                  <span v-if="r.version_stale === true" class="js-tag is-stale">
                    快照 v{{ r.asset_version }} → 当前 v{{ r.current_version }}
                  </span>
                  <span v-else-if="r.version_stale === null && r.asset_id" class="js-tag is-off">
                    资产已删除
                  </span>
                </div>
              </div>
            </div>

            <div v-if="rebaseAsset" class="js-rebase">
              <n-button size="tiny" secondary @click="emitRebase">
                以 v{{ rebaseAsset.version }} 为基准重做
              </n-button>
              <span class="js-hint">进入分镜图工作台；参考图按当前镜头定义重新解析</span>
            </div>

            <div class="js-foot">
              快照为生成时的历史记录，不随当前资产变化；标记「已非当前版本」只提示差异，不会自动重做。
              「以此版本为基准重做」只复用该版本的提示词与参数，不复用快照里的参考图版本。
            </div>
          </div>

          <div v-else-if="!detailLoading" class="js-empty">
            {{ error || '该任务没有冻结的输入快照（早期任务可能未记录）' }}
          </div>
        </n-spin>
      </template>
    </template>
  </div>
</template>

<style scoped>
.js-sec {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.js-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.js-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
}
.js-hint {
  font-size: 12px;
  color: var(--text-tertiary);
}
.js-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-2) 0;
}
.js-row {
  display: flex;
  gap: var(--sp-2);
  font-size: 12px;
}
.js-key {
  min-width: 64px;
  color: var(--text-tertiary);
}
.js-val {
  color: var(--text-secondary);
}
.js-mono {
  font-family: var(--font-mono, ui-monospace, monospace);
}
.js-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.js-block-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}
.js-prompt {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border-radius: var(--radius-sm, 6px);
  padding: var(--sp-2);
  max-height: 140px;
  overflow: auto;
  white-space: pre-wrap;
}
.js-params {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  font-size: 12px;
  color: var(--text-secondary);
}
.js-refs {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.js-ref {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: 12px;
  color: var(--text-secondary);
}
.js-ref-slot {
  font-weight: 600;
}
.js-ref-meta {
  color: var(--text-tertiary);
}
.js-tag {
  font-size: 11px;
  padding: 0 4px;
  border-radius: var(--radius-sm, 4px);
}
.js-tag.is-req {
  color: var(--state-warning);
}
.js-tag.is-off {
  color: var(--text-tertiary);
}
.js-tag.is-stale {
  color: var(--state-warning);
}
.js-stale-summary {
  margin-left: var(--sp-2);
  font-size: 11px;
  color: var(--state-warning);
}
.js-empty {
  font-size: 12px;
  color: var(--text-tertiary);
}
.js-error {
  font-size: 12px;
  color: var(--danger-text);
}
.js-foot {
  font-size: 11px;
  color: var(--text-tertiary);
}
.js-rebase {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
</style>
