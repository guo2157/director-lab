<script setup lang="ts">
/** 参考图与生成配置面板（多图参考重新生成工作台 V1）。
 *
 * 设计约定：
 * - 默认模式固定为『沿用原配置，只修改提示词』，参考图/参数/工作流均只读
 * - 参考图能力与槽位映射完全来自后端返回的工作流真实声明，不在前端补造
 * - 帧约束语义（上一镜尾帧）与普通参考图语义分开展示，避免混淆
 * - 生成结果先落候选版本，需用户预览比较后采用
 */
import { computed, ref, watch } from 'vue'
import { NImage, useMessage } from 'naive-ui'
import {
  adoptCandidate,
  fetchCandidates,
  fetchRegenerateConfig,
  submitRegenerate,
  type CandidateAsset,
  type RegenerateConfig,
  type RegenerateMode,
  type RegenerateRef,
} from '@/api/regeneration'

const props = defineProps<{
  shotId: string
  target: 'keyframe' | 'clip'
  baseAssetId?: string
}>()

const message = useMessage()

const loading = ref(false)
const submitting = ref(false)
const config = ref<RegenerateConfig | null>(null)
const mode = ref<RegenerateMode>('inherit_prompt_only')
const promptText = ref('')
const candidates = ref<CandidateAsset[]>([])
const localRefs = ref<RegenerateRef[]>([])
/** 本次提交使用的幂等键：提交期间保持不变，成功后才重置，避免重复点击产生重复任务 */
const pendingKey = ref<string | null>(null)

const modeOptions = [
  { label: '沿用原配置，只修改提示词', value: 'inherit_prompt_only' },
  { label: '修改参考图', value: 'modify_refs' },
  { label: '完整重新配置', value: 'full_reconfigure' },
]

const incomplete = computed(() => config.value !== null && !config.value.snapshot_complete)
const promptOnly = computed(() => mode.value === 'inherit_prompt_only')
const refsEditable = computed(() => mode.value !== 'inherit_prompt_only')

const referenceRefs = computed(() => localRefs.value.filter((r) => r.semantic !== 'frame_constraint'))
const constraintRefs = computed(() => localRefs.value.filter((r) => r.semantic === 'frame_constraint'))

const lockedItems = computed(() => {
  if (!config.value) return []
  const items: string[] = []
  if (promptOnly.value) {
    items.push('参考图列表与版本')
    items.push('参考图用途与工作流输入映射')
    items.push('工作流与模型版本')
    items.push('采样器、步数、尺寸与时长等参数')
  } else if (mode.value === 'modify_refs') {
    items.push('工作流与模型版本')
    items.push('采样器、步数、尺寸与时长等参数')
  }
  return items
})

async function load() {
  if (!props.shotId) return
  loading.value = true
  try {
    const data = await fetchRegenerateConfig(props.shotId, props.target, props.baseAssetId)
    config.value = data
    promptText.value = data.prompt?.rendered ?? ''
    localRefs.value = (data.refs ?? []).map((r) => ({ ...r }))
    if (!data.snapshot_complete) {
      mode.value = 'full_reconfigure'
    }
  } catch (e: any) {
    message.error(e?.message ?? '加载生成配置失败')
    config.value = null
  } finally {
    loading.value = false
  }
}

watch(() => [props.shotId, props.target, props.baseAssetId], load, { immediate: true })

function toggleRef(ref: RegenerateRef) {
  if (!refsEditable.value) return
  if (ref.required) {
    message.warning('该槽位为当前工作流必需输入，不可移除')
    return
  }
  ref.enabled = !ref.enabled
}

function slotRoleLabel(role?: string | null): string {
  const map: Record<string, string> = {
    character: '角色参考',
    scene: '场景参考',
    prop: '道具参考',
    keyframe: '本镜分镜图',
    prev_frame: '上一镜尾帧',
    first_frame: '首帧约束',
    end_frame: '尾帧约束',
  }
  return role ? map[role] ?? role : '参考图'
}

async function submit() {
  if (!config.value) return
  if (incomplete.value && mode.value === 'inherit_prompt_only') {
    message.warning('历史配置不完整，请改用完整重新配置')
    return
  }
  submitting.value = true
  // 幂等键在本次提交内保持不变，成功后才重置：
  // 网络慢时用户重复点击不会产生多个重复任务，失败后仍可用同一键安全重试
  if (!pendingKey.value) {
    pendingKey.value = `${props.shotId}-${props.target}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  }
  try {
    const payload: Record<string, unknown> = {
      target: props.target,
      mode: mode.value,
      prompt: promptText.value,
      idempotency_key: pendingKey.value,
    }
    if (props.baseAssetId) payload.base_asset_id = props.baseAssetId
    if (refsEditable.value) {
      payload.refs = localRefs.value.map((r) => ({
        slot_param: r.slot_param,
        asset_id: r.asset_id,
        enabled: r.enabled !== false,
      }))
    }
    const job = await submitRegenerate(props.shotId, payload)
    pendingKey.value = null
    message.success(`已提交重新生成任务，结果将作为候选版本保存（任务 ${job.id}）`)
    if (config.value.base?.asset_id) {
      const res = await fetchCandidates(config.value.base.asset_id)
      candidates.value = res.candidates ?? []
    }
  } catch (e: any) {
    message.error(e?.message ?? '提交重新生成任务失败')
  } finally {
    submitting.value = false
  }
}

async function adopt(candidate: CandidateAsset) {
  try {
    await adoptCandidate(candidate.asset_id)
    message.success(`已采用候选版本 v${candidate.version}`)
    await load()
    candidates.value = candidates.value.filter((c) => c.asset_id !== candidate.asset_id)
  } catch (e: any) {
    message.error(e?.message ?? '采用候选版本失败')
  }
}
</script>

<template>
  <n-spin :show="loading">
    <div class="regen-panel">
      <n-alert v-if="incomplete" type="warning" :bordered="false" style="margin-bottom: 12px">
        历史配置不完整（{{ config?.incomplete_reasons?.join('、') }}），无法沿用原配置，请改用完整重新配置。
      </n-alert>

      <div class="mode-banner">
        <span class="mode-label">当前模式</span>
        <n-radio-group v-model:value="mode" size="small">
          <n-radio-button v-for="opt in modeOptions" :key="opt.value" :value="opt.value">
            {{ opt.label }}
          </n-radio-button>
        </n-radio-group>
      </div>

      <n-descriptions v-if="config" size="small" :column="1" bordered style="margin-top: 12px">
        <n-descriptions-item label="当前资产 / 基础版本">
          {{ config.base?.name ?? '—' }} · v{{ config.base?.version ?? '—' }}
        </n-descriptions-item>
        <n-descriptions-item label="当前工作流">
          {{ config.workflow.name ?? '—' }} · 版本 {{ config.workflow.version ?? '—' }}
        </n-descriptions-item>
        <n-descriptions-item label="模型参数（本次不修改）">
          <code>{{ JSON.stringify(config.model_params) }}</code>
        </n-descriptions-item>
      </n-descriptions>

      <div class="section-title">参考图与帧约束</div>
      <n-empty v-if="!localRefs.length" description="本次生成不携带参考图" size="small" />
      <div v-for="ref in referenceRefs" :key="ref.slot_param" class="ref-card">
        <n-image
          v-if="ref.file_path"
          :src="`/api/assets/${ref.asset_id}/file`"
          width="48"
          height="48"
          object-fit="cover"
          class="ref-thumb"
          :img-props="{ style: 'width:100%;height:100%;object-fit:cover;border-radius:4px' }"
        />
        <div v-else class="ref-thumb placeholder">无预览</div>
        <div class="ref-meta">
          <div class="ref-line">
            <span class="ref-slot">{{ ref.slot_param }}</span>
            <n-tag size="tiny" :bordered="false">{{ slotRoleLabel(ref.role) }}</n-tag>
            <n-tag v-if="ref.required" size="tiny" type="warning" :bordered="false">必需</n-tag>
          </div>
          <div class="ref-sub">资产 v{{ ref.asset_version ?? '—' }} · 输入位置 {{ ref.input_key ?? '—' }}</div>
        </div>
        <n-switch
          :value="ref.enabled !== false"
          size="small"
          :disabled="!refsEditable || ref.required"
          @update:value="toggleRef(ref)"
        />
      </div>

      <template v-if="constraintRefs.length">
        <div class="section-title">帧约束（区别于参考图）</div>
        <div v-for="ref in constraintRefs" :key="ref.slot_param" class="ref-card constraint">
          <n-image
            v-if="ref.file_path"
            :src="`/api/assets/${ref.asset_id}/file`"
            width="48"
            height="48"
            object-fit="cover"
            class="ref-thumb"
            :img-props="{ style: 'width:100%;height:100%;object-fit:cover;border-radius:4px' }"
          />
          <div v-else class="ref-thumb placeholder">无预览</div>
          <div class="ref-meta">
            <div class="ref-line">
              <span class="ref-slot">{{ ref.slot_param }}</span>
              <n-tag size="tiny" type="info" :bordered="false">{{ slotRoleLabel(ref.role) }}</n-tag>
              <n-tag v-if="ref.required" size="tiny" type="warning" :bordered="false">必需</n-tag>
            </div>
            <div class="ref-sub">资产 v{{ ref.asset_version ?? '—' }} · 输入位置 {{ ref.input_key ?? '—' }}</div>
          </div>
          <n-switch
            :value="ref.enabled !== false"
            size="small"
            :disabled="!refsEditable || ref.required"
            @update:value="toggleRef(ref)"
          />
        </div>
      </template>

      <div class="section-title">正向提示词</div>
      <n-input v-model:value="promptText" type="textarea" :rows="4" />

      <div v-if="lockedItems.length" class="locked-box">
        <div class="locked-title">本次不修改</div>
        <ul>
          <li v-for="item in lockedItems" :key="item">{{ item }}</li>
        </ul>
      </div>

      <n-space justify="end" style="margin-top: 12px">
        <n-button type="primary" :loading="submitting" @click="submit">生成候选版本</n-button>
      </n-space>

      <template v-if="candidates.length">
        <div class="section-title">候选版本</div>
        <div v-for="c in candidates" :key="c.asset_id" class="ref-card">
          <n-image
            v-if="c.thumb_path || c.file_path"
            :src="`/api/assets/${c.asset_id}/file?thumb=true`"
            width="48"
            height="48"
            object-fit="cover"
            class="ref-thumb"
            :img-props="{ style: 'width:100%;height:100%;object-fit:cover;border-radius:4px' }"
          />
          <div v-else class="ref-thumb placeholder">无预览</div>
          <div class="ref-meta">
            <div class="ref-line"><span class="ref-slot">候选 v{{ c.version }}</span></div>
            <div class="ref-sub">来源任务 {{ c.source_job_id ?? '—' }}</div>
          </div>
          <n-button size="small" type="primary" secondary @click="adopt(c)">采用</n-button>
        </div>
      </template>
    </div>
  </n-spin>
</template>

<style scoped>
.regen-panel {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--divider-color, #efeff5);
}
.mode-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.mode-label {
  font-size: 12px;
  color: var(--text-tertiary, #909399);
}
.section-title {
  margin: 14px 0 6px;
  font-size: 13px;
  font-weight: 600;
}
.ref-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px;
  border: 1px solid var(--divider-color, #efeff5);
  border-radius: 6px;
  margin-bottom: 8px;
}
.ref-card.constraint {
  border-style: dashed;
}
.ref-thumb {
  width: 48px;
  height: 48px;
  object-fit: cover;
  border-radius: 4px;
  background: #f5f7fa;
}
.ref-thumb.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: #c0c4cc;
}
.ref-meta {
  flex: 1;
  min-width: 0;
}
.ref-line {
  display: flex;
  align-items: center;
  gap: 6px;
}
.ref-slot {
  font-size: 12px;
  font-family: monospace;
}
.ref-sub {
  font-size: 11px;
  color: var(--text-tertiary, #909399);
  margin-top: 2px;
}
.locked-box {
  margin-top: 14px;
  padding: 8px 10px;
  background: #fafafa;
  border-radius: 6px;
  font-size: 12px;
}
.locked-title {
  font-weight: 600;
  margin-bottom: 4px;
}
.locked-box ul {
  margin: 0;
  padding-left: 18px;
  color: var(--text-tertiary, #909399);
}
</style>
