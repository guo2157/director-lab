<script setup lang="ts">
/**
 * 重新生成抽屉：改提示词 → 提交任务 → 新结果直接覆盖旧版，只保留一份。
 * 生成期间原图仍可见；成功后页面展示的即为重新生成后的资产图。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NAlert, NButton, NDrawer, NDrawerContent, NInput, NSpin, NTag, useMessage } from 'naive-ui'
import { getAssetRegenerateConfig, regenerateAsset } from '@/api/assets'
import { getJob } from '@/api/jobs'
import type { Asset } from '@/types/asset'

const props = defineProps<{
  show: boolean
  asset: Asset | null
  /** 当前选中的资产出图工作流（asset_image 槽位）；未传时由后端按默认解析 */
  workflowId?: string
  /** 详情页带入的提示词（含未保存编辑），优先于模板默认值 */
  presetPrompt?: string
}>()

const emit = defineEmits<{
  'update:show': [boolean]
  /** 重新生成完成、新结果覆盖旧版后：父组件刷新资产与实体数据 */
  refresh: []
  /** 任务已提交：父组件接管顶部进度面板的轮询 */
  submitted: []
}>()

const message = useMessage()

const loading = ref(false)
const prompt = ref('')
const negative = ref('')
const promptVersion = ref('')
const submitting = ref(false)
const jobStatus = ref('')
const errorMsg = ref('')
let timer: number | null = null

const typeLabel = computed(() => {
  const map: Record<string, string> = { character: '角色', scene: '场景', prop: '道具' }
  return map[props.asset?.asset_type ?? ''] ?? (props.asset?.asset_type ?? '')
})

function stopPoll() {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
}

function startPoll(jobId: string) {
  stopPoll()
  timer = window.setInterval(async () => {
    try {
      const job = await getJob(jobId)
      jobStatus.value = job.status
      if (['succeeded', 'failed', 'lost', 'cancelled'].includes(job.status)) {
        stopPoll()
        if (job.status === 'succeeded') {
          message.success('重新生成完成，新结果已覆盖旧版')
          emit('refresh')
        } else {
          errorMsg.value = (job as { error_msg?: string }).error_msg || `任务${job.status}`
        }
      }
    } catch {
      stopPoll()
    }
  }, 3000)
}

async function load() {
  const asset = props.asset
  if (!asset) return
  stopPoll()
  loading.value = true
  errorMsg.value = ''
  jobStatus.value = ''
  try {
    const cfg = await getAssetRegenerateConfig(asset.id)
    // 详情页带入的提示词优先：用户改过内容时不能被模板默认值覆盖
    const preset = (props.presetPrompt ?? '').trim()
    prompt.value = preset || cfg.prompt
    negative.value = cfg.negative_prompt
    promptVersion.value = cfg.asset_prompt_version
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : '读取重新生成配置失败'
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.show, props.asset?.id] as const,
  ([show]) => {
    if (show) void load()
    else stopPoll()
  },
)

async function submit() {
  const asset = props.asset
  if (!asset) return
  submitting.value = true
  errorMsg.value = ''
  try {
    // 重新生成必须与批量生成使用同一份工作流选择，不能在这里回落成别的默认
    const res = await regenerateAsset(asset.id, {
      prompt: prompt.value,
      negative_prompt: negative.value,
      workflow_id: props.workflowId,
      idempotency_key: `asset_regen_${asset.id}_${Date.now()}`,
    })
    jobStatus.value = res.status
    message.success('重新生成任务已提交，正在生成（进度见页面顶部状态条）')
    startPoll(res.job_id)
    emit('submitted')
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : '提交失败'
  } finally {
    submitting.value = false
  }
}

onBeforeUnmount(stopPoll)
</script>

<template>
  <n-drawer
    :show="show"
    :width="'min(560px, 100vw)'"
    @update:show="(v: boolean) => emit('update:show', v)"
    @after-leave="stopPoll"
  >
    <n-drawer-content title="重新生成资产图" closable>
      <div v-if="asset" class="regen">
        <div class="regen-base">
          <img v-if="asset.thumb_url || asset.url" :src="asset.thumb_url || asset.url" :alt="`${asset.name || asset.id} 当前图`" class="regen-base-img" />
          <div class="regen-base-info">
            <div class="regen-base-name">{{ asset.name || asset.id }}</div>
            <div class="regen-base-meta">{{ typeLabel }} · 当前 v{{ asset.version }}</div>
          </div>
        </div>

        <n-spin v-if="loading" class="regen-loading" />

        <template v-else>
          <n-alert v-if="errorMsg" type="error" :bordered="false">{{ errorMsg }}</n-alert>

          <div class="regen-field">
            <div class="regen-label">
              提示词
              <span v-if="promptVersion" class="regen-tag">模板 v{{ promptVersion }}</span>
            </div>
            <n-input
              v-model:value="prompt"
              type="textarea"
              :autosize="{ minRows: 6, maxRows: 14 }"
              placeholder="描述该资产的外观与画面要求"
            />
          </div>

          <div class="regen-field">
            <div class="regen-label">负面提示词</div>
            <n-input
              v-model:value="negative"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 6 }"
              placeholder="不希望出现的元素"
            />
          </div>

          <p class="regen-locked">
            参考图与工作流沿用原生成配置，不可在此修改，以保证资产风格与分镜一致性。
          </p>

          <div class="regen-submit">
            <n-button size="small" type="primary" :loading="submitting" @click="submit">提交重新生成</n-button>
            <n-tag
              v-if="jobStatus"
              size="small"
              :type="jobStatus === 'succeeded' ? 'success' : jobStatus === 'failed' ? 'error' : 'info'"
            >
              任务状态：{{ jobStatus }}
            </n-tag>
          </div>
        </template>
      </div>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.regen {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.regen-base {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding-bottom: var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
}
.regen-base-img {
  width: 88px;
  height: 88px;
  object-fit: cover;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}
.regen-base-info {
  min-width: 0;
}
.regen-base-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.regen-base-meta {
  font-size: 12px;
  color: var(--text-secondary);
}
.regen-loading {
  display: flex;
  justify-content: center;
  padding: var(--sp-10) 0;
}
.regen-field {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.regen-label {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
.regen-tag {
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 400;
  color: var(--text-tertiary);
}
.regen-locked {
  margin: 0;
  padding: var(--sp-2) var(--sp-3);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
}
.regen-submit {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
</style>
