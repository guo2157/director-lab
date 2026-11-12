<script setup lang="ts">
/** 分镜视频重新生成工作台。
 *
 * 设计约定（对齐后端 regen_workspace 五条原则）：
 * - 结构化参考图绑定是唯一事实来源，提示词只是绑定的自然语言表达
 * - 槽位 node_id 一律使用后端从工作流 definition 解析出的真实值，前端不按下标推断
 * - 身份（谁是谁）、空间（谁在哪里）、动作（谁做什么）三块独立编辑，互不推导
 * - H3 ref2va 仅支持提示词级空间/时间约束，不提供伪造的坐标或时间轴控件
 * - 历史数据缺失一律显示待确认，不静默补默认值；用户手写提示词不被自动覆盖
 */
import { computed, ref, watch } from 'vue'
import { NButton, NInput, NSelect, NSwitch, NTabPane, NTabs, NTag, useMessage } from 'naive-ui'
import {
  fetchRegenerateWorkspace,
  saveRegenerateWorkspace,
  submitRegenerateWorkspace,
  validateRegenerateWorkspace,
  type ConsistencyIssue,
  type RegenerateWorkspace,
  type WorkspaceReference,
  type WorkspaceSpatialCharacter,
} from '@/api/regeneration'
import { listAssets } from '@/api/assets'
import type { Asset } from '@/types/asset'

const props = defineProps<{
  shotId: string
  projectId: string
  target: 'keyframe' | 'clip'
}>()

const emit = defineEmits<{
  (e: 'submitted', job: Record<string, unknown>): void
  (e: 'close'): void
}>()

const message = useMessage()

const loading = ref(false)
const saving = ref(false)
const validating = ref(false)
const submitting = ref(false)
const workspace = ref<RegenerateWorkspace | null>(null)
const activeTab = ref<'refs' | 'identity' | 'spatial' | 'timeline' | 'prompt' | 'check'>('refs')
const assets = ref<Asset[]>([])
const validationResult = ref<{ valid: boolean; errors: ConsistencyIssue[]; warnings: ConsistencyIssue[] } | null>(null)
const pendingKey = ref<string | null>(null)

/** 可绑定的参考图资产：角色/场景/道具/参考图/分镜图 */
const assetOptions = computed(() =>
  assets.value
    .filter((a) => ['character', 'scene', 'prop', 'reference', 'keyframe'].includes(a.asset_type))
    .map((a) => ({ label: `${a.name ?? a.id}（${a.asset_type} v${a.version}）`, value: a.id })),
)

const slots = computed(() => workspace.value?.available_slots ?? [])
const references = computed(() => workspace.value?.references ?? [])
const bindings = computed(() => workspace.value?.character_bindings ?? [])
const spatialChars = computed<WorkspaceSpatialCharacter[]>(() => workspace.value?.spatial_layout?.characters ?? [])
const actions = computed(() => workspace.value?.action_timeline?.actions ?? [])
const props_ = computed(() => workspace.value?.prop_bindings ?? [])
const continuity = computed(() => workspace.value?.continuity)
const validation = computed(() => workspace.value?.validation)
const historyCompat = computed(() => workspace.value?.history_compat)
const capabilities = computed(() => workspace.value?.capabilities)

/** 已绑定槽位集合，用于新增时排除重复 */
const boundSlots = computed(() => new Set(references.value.map((r) => r.slot_id)))
const unboundSlots = computed(() => slots.value.filter((s) => !boundSlots.value.has(s.param)))

const SECTION_LABELS: Record<string, string> = {
  reference_bindings: '参考图绑定',
  character_identity: '角色身份约束',
  scene: '场景约束',
  spatial: '角色空间位置',
  action_timeline: '动作过程',
  prop: '道具约束',
  camera: '镜头运动',
  continuity: '前后镜衔接',
  style: '风格',
  user_text: '用户正文',
}

async function loadAll() {
  loading.value = true
  try {
    workspace.value = await fetchRegenerateWorkspace(props.shotId, props.target)
    validationResult.value = null
  } catch (e: unknown) {
    message.error(`读取工作台失败：${(e as Error)?.message ?? e}`)
  } finally {
    loading.value = false
  }
}

async function loadAssets() {
  try {
    const page = await listAssets({ project_id: props.projectId, page_size: 200 })
    assets.value = page.items ?? []
  } catch {
    assets.value = []
  }
}

watch(() => props.shotId, () => { void loadAll() }, { immediate: true })
watch(() => props.projectId, () => { void loadAssets() }, { immediate: true })

function addReference(slotParam: string) {
  if (!workspace.value) return
  const slot = slots.value.find((s) => s.param === slotParam)
  if (!slot) return
  workspace.value.references = [
    ...workspace.value.references,
    {
      reference_id: `ref_${slotParam}_${Date.now()}`,
      slot_id: slotParam,
      node_id: slot.node_id,
      input_key: slot.input_key,
      image_no: slot.image_no,
      asset_id: null,
      enabled: true,
      required: Boolean(slot.required),
      status: 'pending_confirmation',
    } as WorkspaceReference,
  ]
}

function removeReference(referenceId: string) {
  if (!workspace.value) return
  const ref = workspace.value.references.find((r) => r.reference_id === referenceId)
  if (ref?.required) {
    message.warning('该槽位为工作流必需输入，不可移除，只能替换资产或禁用可选槽位')
    return
  }
  workspace.value.references = workspace.value.references.filter((r) => r.reference_id !== referenceId)
}

function buildPayload() {
  const w = workspace.value!
  return {
    target: props.target,
    references: w.references,
    character_bindings: w.character_bindings,
    spatial_layout: w.spatial_layout,
    action_timeline: w.action_timeline,
    prop_bindings: w.prop_bindings,
    prompt_sections: w.prompt_sections,
    final_prompt: w.final_prompt,
    parameters: w.parameters,
  }
}

async function handleSave() {
  if (!workspace.value) return
  saving.value = true
  try {
    workspace.value = await saveRegenerateWorkspace(props.shotId, buildPayload())
    message.success(`草稿已保存（版本 ${workspace.value.history_compat.saved_version}）`)
  } catch (e: unknown) {
    message.error(`保存失败：${(e as Error)?.message ?? e}`)
  } finally {
    saving.value = false
  }
}

async function handleValidate() {
  if (!workspace.value) return
  validating.value = true
  try {
    const res = await validateRegenerateWorkspace(props.shotId, buildPayload())
    validationResult.value = { valid: res.valid, errors: res.errors, warnings: res.warnings }
    if (res.valid) {
      message.success(res.warnings.length ? `校验通过，有 ${res.warnings.length} 条提示需确认` : '校验通过')
    } else {
      message.error(`校验未通过：${res.errors.length} 个错误`)
    }
  } catch (e: unknown) {
    message.error(`校验失败：${(e as Error)?.message ?? e}`)
  } finally {
    validating.value = false
  }
}

async function handleSubmit() {
  if (!workspace.value) return
  if (!pendingKey.value) pendingKey.value = `ws-${props.shotId}-${Date.now()}`
  submitting.value = true
  try {
    const job = await submitRegenerateWorkspace(props.shotId, {
      ...buildPayload(),
      idempotency_key: pendingKey.value,
    })
    message.success('已提交生成，结果先落候选版本，需确认后才会替换当前视频')
    emit('submitted', job as unknown as Record<string, unknown>)
    pendingKey.value = null
    await loadAll()
    return job
  } catch (e: unknown) {
    message.error(`提交失败：${(e as Error)?.message ?? e}`)
  } finally {
    submitting.value = false
  }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function posLabel(p: unknown) {
  const r = p as Record<string, any> | null | undefined;
  return _posLabel(r);
}
function _posLabel(p: Record<string, any> | null | undefined) {
  if (!p) return '未设置';
  if (p.status !== 'confirmed') return '待确认';
  const parts = [p.horizontal, p.vertical, p.depth].filter(Boolean);
  return parts.length ? parts.join('/') : '已确认';
}
</script>

<template>
  <div class="regen-workbench">
    <div v-if="loading" class="wb-loading">正在读取工作台…</div>

    <template v-else-if="workspace">
      <!-- 状态条 -->
      <div class="wb-status">
        <n-tag :type="workspace.source === 'saved' ? 'success' : 'default'" size="small">
          {{ workspace.source === 'saved' ? `已保存草稿 v${historyCompat?.saved_version}` : '由历史配置推导' }}
        </n-tag>
        <n-tag v-if="historyCompat && !historyCompat.base_snapshot_complete" type="warning" size="small">
          历史配置不完整（{{ (historyCompat.incomplete_reasons || []).join('、') }}）
        </n-tag>
        <n-tag size="small">槽位 {{ slots.length }} 个</n-tag>
        <n-tag size="small">已绑定 {{ references.length }} 个</n-tag>
        <n-tag type="info" size="small">
          空间/时间控制：提示词级（工作流不支持坐标或时间节点输入）
        </n-tag>
      </div>

      <!-- 连续性提示 -->
      <div v-if="continuity && continuity.issues.length" class="wb-continuity">
        <div class="wb-continuity-title">
          前后镜连续性：{{ continuity.status === 'first_shot' ? '首镜无需比对' : (continuity.status === 'conflict' ? '存在冲突' : '已比对') }}
          <span class="wb-hint">（仅提示，不会自动改写配置）</span>
        </div>
        <div v-for="(issue, i) in continuity.issues" :key="i" class="wb-continuity-item">
          <n-tag :type="issue.level === 'warning' ? 'warning' : 'default'" size="tiny">{{ issue.code }}</n-tag>
          <span>{{ issue.message }}</span>
        </div>
      </div>

      <!-- 校验结果区 -->
      <div v-if="validationResult" class="wb-validation">
        <div v-for="(e, i) in validationResult.errors" :key="`e${i}`" class="wb-v-item wb-v-error">
          <n-tag type="error" size="tiny">{{ e.code }}</n-tag><span>{{ e.message }}</span>
        </div>
        <div v-for="(w, i) in validationResult.warnings" :key="`w${i}`" class="wb-v-item wb-v-warn">
          <n-tag type="warning" size="tiny">{{ w.code }}</n-tag><span>{{ w.message }}</span>
        </div>
      </div>

      <n-tabs v-model:value="activeTab" type="line" size="small">
        <!-- 参考图与槽位 -->
        <n-tab-pane name="refs" tab="参考图与槽位">
          <div class="wb-hint-line">槽位与节点来自工作流真实定义，不支持凭空新增未声明的槽位。</div>
          <table class="wb-table">
            <thead>
              <tr><th>槽位</th><th>Image</th><th>节点</th><th>资产</th><th>启用</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-for="ref in references" :key="ref.reference_id">
                <td>
                  <div class="wb-slot">{{ ref.slot_id }}</div>
                  <n-tag v-if="ref.required" type="warning" size="tiny">必需</n-tag>
                </td>
                <td>{{ ref.image_no ?? '—' }}</td>
                <td class="wb-mono">{{ ref.node_id ?? '未映射' }}</td>
                <td>
                  <n-select
                    v-model:value="ref.asset_id"
                    :options="assetOptions"
                    size="small"
                    filterable
                    clearable
                    placeholder="选择参考资产"
                    style="min-width: 200px"
                  />
                </td>
                <td><n-switch v-model:value="ref.enabled" size="small" /></td>
                <td>
                  <n-button text type="error" size="tiny" @click="removeReference(ref.reference_id)">移除</n-button>
                </td>
              </tr>
              <tr v-if="!references.length">
                <td colspan="6" class="wb-empty">当前没有绑定任何参考图</td>
              </tr>
            </tbody>
          </table>
          <div v-if="unboundSlots.length" class="wb-add-row">
            <span class="wb-hint-line">未绑定槽位：</span>
            <n-button
              v-for="s in unboundSlots"
              :key="s.param"
              size="tiny"
              secondary
              @click="addReference(s.param)"
            >
              + {{ s.param }}（Image {{ s.image_no ?? '—' }}）
            </n-button>
          </div>
        </n-tab-pane>

        <!-- 角色身份 -->
        <n-tab-pane name="identity" tab="角色身份">
          <div class="wb-hint-line">身份绑定解决『谁是谁』：角色必须绑定一个身份参考槽位，避免多角色外观串味。</div>
          <table class="wb-table">
            <thead><tr><th>角色</th><th>身份参考槽位</th><th>Image</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="(b, i) in bindings" :key="i">
                <td>{{ b.character_name ?? '未命名' }}</td>
                <td class="wb-mono">{{ b.identity_slot_id ?? '—' }}</td>
                <td>{{ b.identity_image_no ?? '—' }}</td>
                <td>
                  <n-tag :type="b.status === 'bound' ? 'success' : 'warning'" size="tiny">
                    {{ b.status === 'bound' ? '已绑定' : '缺少身份参考图' }}
                  </n-tag>
                </td>
              </tr>
              <tr v-if="!bindings.length"><td colspan="4" class="wb-empty">本镜未识别到出场角色</td></tr>
            </tbody>
          </table>
        </n-tab-pane>

        <!-- 空间布局 -->
        <n-tab-pane name="spatial" tab="空间布局">
          <div class="wb-hint-line">
            空间布局解决『谁在哪里』。工作流不支持坐标输入，此处只转为自然语言描述；待确认项需人工补齐。
          </div>
          <table class="wb-table">
            <thead><tr><th>角色</th><th>初始位置</th><th>最终位置</th><th>朝向</th><th>移动</th></tr></thead>
            <tbody>
              <tr v-for="(c, i) in spatialChars" :key="i">
                <td>{{ c.character_name }}</td>
                <td>{{ posLabel(c.initial_position) }}</td>
                <td>{{ posLabel(c.final_position) }}</td>
                <td>{{ c.initial_position?.facing_direction ?? '—' }}</td>
                <td>{{ c.movement_enabled ? '有位移' : '静止' }}</td>
              </tr>
              <tr v-if="!spatialChars.length"><td colspan="5" class="wb-empty">本镜未识别到角色空间信息</td></tr>
            </tbody>
          </table>
        </n-tab-pane>

        <!-- 动作时间轴 -->
        <n-tab-pane name="timeline" tab="动作时间轴">
          <div class="wb-hint-line">
            动作时间轴解决『谁做什么』。工作流不支持时间节点输入，进度区间仅用于生成自然语言描述。
          </div>
          <table class="wb-table">
            <thead><tr><th>进度</th><th>主体</th><th>动作</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="a in actions" :key="a.action_id">
                <td>{{ a.start_progress }} ~ {{ a.end_progress }}</td>
                <td>{{ a.character_name ?? '未指定' }}</td>
                <td>{{ a.action_description }}</td>
                <td>
                  <n-tag :type="a.status === 'confirmed' ? 'success' : 'warning'" size="tiny">
                    {{ a.status === 'confirmed' ? '已确认' : '动作主体待确认' }}
                  </n-tag>
                </td>
              </tr>
              <tr v-if="!actions.length"><td colspan="4" class="wb-empty">本镜未解析到动作节拍</td></tr>
            </tbody>
          </table>
          <div v-if="props_.length" class="wb-props">
            <div class="wb-section-title">道具绑定</div>
            <div v-for="(p, i) in props_" :key="i" class="wb-prop-item">
              {{ p.prop_name }}：持有者 {{ p.holder_character_name ?? '待确认' }}
            </div>
          </div>
        </n-tab-pane>

        <!-- 分层提示词 -->
        <n-tab-pane name="prompt" tab="分层提示词">
          <div class="wb-hint-line">
            各分层由结构化数据生成，最终提示词可自由编辑；系统不会自动覆盖你手写的内容。
          </div>
          <div v-for="(_val, key, i) in workspace.prompt_sections" :key="i" class="wb-prompt-section">
            <div class="wb-section-title">{{ SECTION_LABELS[key] ?? key }}</div>
            <n-input
              v-model:value="workspace.prompt_sections[key]"
              type="textarea"
              size="small"
              :autosize="{ minRows: 1, maxRows: 6 }"
              :placeholder="`（${SECTION_LABELS[key] ?? key}为空）`"
            />
          </div>
          <div class="wb-prompt-section">
            <div class="wb-section-title">最终提示词（提交时使用）</div>
            <n-input
              v-model:value="workspace.final_prompt"
              type="textarea"
              size="small"
              :autosize="{ minRows: 4, maxRows: 14 }"
              placeholder="此处为实际提交给工作流的完整提示词"
            />
          </div>
        </n-tab-pane>

        <!-- 校验与参数 -->
        <n-tab-pane name="check" tab="校验与参数">
          <div class="wb-section-title">生成参数（来自基础版本快照）</div>
          <pre class="wb-params">{{ JSON.stringify(workspace.parameters, null, 2) }}</pre>
          <div class="wb-section-title">能力声明</div>
          <div class="wb-hint-line">{{ capabilities?.reason }}</div>
          <div class="wb-section-title">当前派生校验</div>
          <div v-for="(e, i) in (validation?.errors || [])" :key="`e${i}`" class="wb-v-item wb-v-error">
            <n-tag type="error" size="tiny">{{ e.code }}</n-tag><span>{{ e.message }}</span>
          </div>
          <div v-for="(w, i) in (validation?.warnings || [])" :key="`w${i}`" class="wb-v-item wb-v-warn">
            <n-tag type="warning" size="tiny">{{ w.code }}</n-tag><span>{{ w.message }}</span>
          </div>
        </n-tab-pane>
      </n-tabs>

      <!-- 操作栏 -->
      <div class="wb-actions">
        <n-button size="small" :loading="saving" @click="handleSave">保存草稿</n-button>
        <n-button size="small" :loading="validating" @click="handleValidate">校验</n-button>
        <n-button
          size="small"
          type="primary"
          :loading="submitting"
          :disabled="validationResult ? !validationResult.valid : false"
          @click="handleSubmit"
        >
          提交生成（先落候选）
        </n-button>
      </div>
    </template>

    <div v-else class="wb-loading">暂无工作台数据</div>
  </div>
</template>

<style scoped>
.regen-workbench { display: flex; flex-direction: column; gap: 8px; }
.wb-loading { padding: 24px; text-align: center; color: #999; font-size: 13px; }
.wb-status { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.wb-continuity { border: 1px solid #ffd591; background: #fffbe6; border-radius: 4px; padding: 8px 10px; }
.wb-continuity-title { font-size: 12px; font-weight: 600; margin-bottom: 4px; }
.wb-continuity-item { display: flex; gap: 6px; font-size: 12px; line-height: 1.7; align-items: flex-start; }
.wb-validation { border: 1px solid #ffccc7; background: #fff2f0; border-radius: 4px; padding: 8px 10px; }
.wb-v-item { display: flex; gap: 6px; font-size: 12px; line-height: 1.8; align-items: flex-start; }
.wb-v-error span { color: #cf1322; }
.wb-v-warn span { color: #d46b08; }
.wb-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.wb-table th, .wb-table td { border-bottom: 1px solid #f0f0f0; padding: 6px 8px; text-align: left; vertical-align: middle; }
.wb-table th { background: #fafafa; font-weight: 600; color: #666; }
.wb-slot { font-family: monospace; font-size: 12px; }
.wb-mono { font-family: monospace; color: #888; }
.wb-empty { text-align: center; color: #bbb; padding: 12px; }
.wb-hint-line { font-size: 12px; color: #999; margin: 4px 0 8px; }
.wb-add-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 8px; }
.wb-section-title { font-size: 12px; font-weight: 600; color: #333; margin: 10px 0 4px; }
.wb-prompt-section { margin-bottom: 8px; }
.wb-params { background: #fafafa; border: 1px solid #f0f0f0; border-radius: 4px; padding: 8px; font-size: 11px; max-height: 200px; overflow: auto; }
.wb-actions { display: flex; gap: 8px; justify-content: flex-end; padding-top: 8px; border-top: 1px solid #f0f0f0; }
.wb-props { margin-top: 10px; }
.wb-prop-item { font-size: 12px; color: #666; line-height: 1.8; }
.wb-hint { font-size: 11px; color: #999; font-weight: normal; }
</style>
