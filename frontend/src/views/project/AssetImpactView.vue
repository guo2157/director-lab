<script setup lang="ts">
/**
 * 资产变更影响列表页（Phase 3）。
 *
 * 复用项目级依赖影响预演：以所有已过期依赖的上游为变化源，按变化源分组列出
 * 需要重生成/重绑定/引用过期的下游对象。默认只展示，不自动标记失效、不删除产物、
 * 不自动重做；「标记失效」只在用户确认后写入依赖边，重做仍由用户显式触发。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NTag, useMessage } from 'naive-ui'
import {
  getProjectImpactPreview,
  getWorkflowImpact,
  invalidateDependencyRefs,
  IMPACT_NODE_TYPE_LABEL,
  IMPACT_STATUS_LABEL,
  WORKFLOW_IMPACT_LABEL,
  type DependencyAffectedNode,
  type DependencyChangeStatus,
  type DependencyProjectImpactPreview,
  type WorkflowImpactItem,
  type WorkflowImpactPreview,
} from '@/api/dependencies'
import { listAssets } from '@/api/assets'
import type { Asset } from '@/types/asset'
import { ASSET_KIND_LABELS } from '@/components/assets/types'
import StageCard from '@/components/ui/StageCard.vue'
import ErrorRetry from '@/components/ui/ErrorRetry.vue'
import SkeletonList from '@/components/ui/SkeletonList.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const route = useRoute()
const router = useRouter()
const projectId = computed(() => String(route.params.id ?? ''))
const message = useMessage()
const confirmDialog = useConfirmDialog()

const loading = ref(false)
const error = ref<string | null>(null)
const data = ref<DependencyProjectImpactPreview | null>(null)
const workflowData = ref<WorkflowImpactPreview | null>(null)
const assetsById = ref<Map<string, Asset>>(new Map())
const invalidating = ref<string | null>(null)

/** 需要用户处理的状态；未生成/已最新不进入待办清单 */
const PENDING_STATUSES: DependencyChangeStatus[] = [
  'redo_required',
  'rebind_required',
  'stale_reference',
]

interface ImpactGroup {
  key: string
  rootType: string
  rootId: string
  rootVersion: number | null
  markable: number
  nodes: DependencyAffectedNode[]
}

/** 按变化源分组：路径首段即变化源 key（与后端 _path_for 口径一致） */
const groups = computed<ImpactGroup[]>(() => {
  const preview = data.value
  if (!preview) return []
  const byRoot = new Map<string, DependencyAffectedNode[]>()
  for (const node of preview.affected) {
    if (!PENDING_STATUSES.includes(node.change_status)) continue
    const rootKey = node.path[0] ?? node.key
    const list = byRoot.get(rootKey) ?? []
    list.push(node)
    byRoot.set(rootKey, list)
  }
  return preview.changed_roots
    .map((root) => ({
      key: root.key,
      rootType: root.type,
      rootId: root.id,
      rootVersion: root.version ?? null,
      markable: root.markable_edge_count ?? 0,
      nodes: byRoot.get(root.key) ?? [],
    }))
    .filter((group) => group.nodes.length > 0)
})

const pendingCount = computed(() => groups.value.reduce((sum, g) => sum + g.nodes.length, 0))
const workflowItems = computed<WorkflowImpactItem[]>(() => workflowData.value?.items ?? [])
const hasAnyImpact = computed(() => pendingCount.value > 0 || workflowItems.value.length > 0)
const staleCount = computed(
  () => groups.value.reduce(
    (sum, g) => sum + g.nodes.filter((n) => n.change_status === 'stale_reference').length,
    0,
  ),
)
const checkedAt = ref<string>('')

function assetLabel(assetId: string): string {
  const asset = assetsById.value.get(assetId)
  if (!asset) return assetId
  const kind = ASSET_KIND_LABELS[asset.asset_type as keyof typeof ASSET_KIND_LABELS]
  return kind ? `${kind}「${asset.name ?? asset.id}」` : (asset.name ?? asset.id)
}

function rootLabel(group: ImpactGroup): string {
  const version = group.rootVersion == null ? '' : ` v${group.rootVersion}`
  return `${assetLabel(group.rootId)}${version}`
}

function nodeLabel(node: DependencyAffectedNode): string {
  return node.type === 'asset' ? assetLabel(node.id) : node.id
}

/** 依赖源 key 转可读文案，如 asset:ast_x → 角色「林小满」 */
function sourceLabel(key: string): string {
  const [type, id] = key.split(':', 2)
  if (!id) return key
  if (type === 'asset') return assetLabel(id)
  return `${IMPACT_NODE_TYPE_LABEL[type] ?? type} ${id}`
}

function staleReason(node: DependencyAffectedNode): string {
  if (!node.stale_inputs.length) return ''
  const sources = Array.from(new Set(node.stale_inputs.map((s) => sourceLabel(s.source))))
  return sources.join('、')
}

function statusTagType(status: DependencyChangeStatus): 'warning' | 'info' | 'error' {
  if (status === 'redo_required') return 'warning'
  if (status === 'stale_reference') return 'error'
  return 'info'
}

/** 显式标记失效：只写依赖边，不重生成、不删除产物；写后刷新影响列表 */
function handleInvalidate(group: ImpactGroup) {
  confirmDialog({
    title: '标记依赖失效',
    message: `将把「${rootLabel(group)}」下游仍引用旧版本的依赖记录标记为失效。`,
    impact: [
      `可标记依赖边 ${group.markable} 条`,
      `影响下游 ${group.nodes.length} 个对象`,
      '只写依赖标记，不重生成、不删除已有产物',
    ],
    confirmText: '标记失效',
    danger: false,
    onConfirm: async () => {
      invalidating.value = group.key
      try {
        const result = await invalidateDependencyRefs(
          projectId.value, group.rootType, group.rootId,
        )
        message.success(`已标记 ${result.summary.marked} 条依赖失效`)
        await load()
      } catch (e: any) {
        message.error(e?.message ?? '标记失效失败')
      } finally {
        invalidating.value = null
      }
    },
  })
}

/** 工作流版本影响的镜头定位文案 */
function workflowShotLabel(item: WorkflowImpactItem): string {
  if (item.episode == null || item.seq == null) return '未绑定镜头'
  return `第${item.episode}集 · 第${item.seq}镜`
}

/** 工作流版本变化：执行时版本 → 当前版本 */
function workflowVersionLabel(item: WorkflowImpactItem): string {
  const current = item.current_workflow_version == null ? '—' : `v${item.current_workflow_version}`
  return `v${item.job_workflow_version} → ${current}`
}

/** 任务产出摘要：资产名 + 版本 */
function workflowOutputLabel(item: WorkflowImpactItem): string {
  const asset = item.output_asset
  if (!asset) return '无产出记录'
  return `${asset.name} v${asset.version}`
}

/** 全量资产索引：仅用于把 ID 显示成名称，失败时退化为 ID，不阻断影响列表 */
async function loadAssetIndex(pid: string): Promise<Map<string, Asset>> {
  const index = new Map<string, Asset>()
  for (let page = 1; ; page += 1) {
    const res = await listAssets({ project_id: pid, page, page_size: 100 })
    const batch = res?.items ?? []
    for (const asset of batch) index.set(asset.id, asset)
    if (!batch.length || index.size >= (res?.total ?? 0)) return index
  }
}

async function load() {
  if (!projectId.value) return
  loading.value = true
  error.value = null
  try {
    const [preview, index, workflow] = await Promise.all([
      getProjectImpactPreview(projectId.value),
      loadAssetIndex(projectId.value).catch(() => new Map<string, Asset>()),
      getWorkflowImpact(projectId.value).catch(() => null),
    ])
    data.value = preview
    assetsById.value = index
    workflowData.value = workflow
    checkedAt.value = new Date().toLocaleTimeString()
  } catch (e: any) {
    data.value = null
    workflowData.value = null
    error.value = e?.message ?? '加载资产变更影响失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page asset-impact-page">
    <h1 class="visually-hidden">资产变更影响</h1>

    <div class="impact-head">
      <n-button quaternary size="small" @click="router.push(`/project/${projectId}/assets`)">
        ← 返回资产库
      </n-button>
      <span class="impact-title">资产变更影响</span>
      <n-button size="small" secondary :loading="loading" @click="load">重新检查</n-button>
    </div>
    <p class="impact-sub">
      上游资产版本变化后，下游镜头与生成产物中需要处理的对象。只读检查，不会自动重做、标记失效或删除已有结果。
    </p>

    <ErrorRetry v-if="error" title="检查失败" :message="error" @retry="load" />

    <SkeletonList v-else-if="loading && !data" variant="card" :rows="3" :columns="1" height="72px" />

    <EmptyState
      v-else-if="data && !hasAnyImpact"
      title="没有待处理的资产变更影响"
      description="当前没有因上游资产版本变化或工作流版本变化而需要处理的对象。"
    />

    <template v-else-if="data">
      <StageCard label="影响概览">
        <div class="impact-summary">
          <div class="summary-item">
            <span class="summary-value">{{ data.changed_roots.length }}</span>
            <span class="summary-label">变化源</span>
          </div>
          <div class="summary-item">
            <span class="summary-value is-warning">{{ data.summary.redo_required }}</span>
            <span class="summary-label">需重生成</span>
          </div>
          <div class="summary-item">
            <span class="summary-value">{{ data.summary.rebind_required }}</span>
            <span class="summary-label">需重绑定</span>
          </div>
          <div class="summary-item">
            <span class="summary-value">{{ staleCount }}</span>
            <span class="summary-label">引用过期</span>
          </div>
          <span v-if="checkedAt" class="summary-checked">检查于 {{ checkedAt }}</span>
        </div>
        <p class="impact-note">{{ data.policy.note }}</p>
      </StageCard>

      <StageCard v-for="group in groups" :key="group.key" :label="rootLabel(group)">
        <template v-if="group.markable > 0" #actions>
          <n-button
            size="tiny"
            secondary
            :loading="invalidating === group.key"
            @click="handleInvalidate(group)"
          >
            标记失效
          </n-button>
        </template>
        <ul class="impact-list">
          <li v-for="node in group.nodes" :key="node.key" class="impact-row">
            <span class="row-depth">L{{ node.depth }}</span>
            <span class="row-type">{{ IMPACT_NODE_TYPE_LABEL[node.type] ?? node.type }}</span>
            <span class="row-name" :title="node.id">{{ nodeLabel(node) }}</span>
            <n-tag size="tiny" :bordered="false" :type="statusTagType(node.change_status)">
              {{ IMPACT_STATUS_LABEL[node.change_status] }}
            </n-tag>
            <span v-if="staleReason(node)" class="row-reason" :title="staleReason(node)">
              {{ staleReason(node) }}
            </span>
          </li>
        </ul>
      </StageCard>

      <StageCard v-if="workflowItems.length" label="工作流版本影响">
        <ul class="impact-list">
          <li v-for="item in workflowItems" :key="item.job_id" class="impact-row">
            <span class="row-depth">{{ workflowShotLabel(item) }}</span>
            <span class="row-type">{{ item.workflow_name ?? item.workflow_id }}</span>
            <span class="row-name">{{ workflowVersionLabel(item) }}</span>
            <n-tag
              size="tiny"
              :bordered="false"
              :type="item.change_status === 'workflow_changed' ? 'warning' : 'error'"
            >
              {{ WORKFLOW_IMPACT_LABEL[item.change_status] }}
            </n-tag>
            <span class="row-reason" :title="workflowOutputLabel(item)">
              {{ workflowOutputLabel(item) }}
            </span>
          </li>
        </ul>
        <p class="impact-note">
          以下已生成任务使用的不是当前工作流版本。只读比对，不会自动重生成或删除已有结果。
        </p>
      </StageCard>
    </template>
  </div>
</template>

<style scoped>
.asset-impact-page {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.impact-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.impact-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}
.impact-sub {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-tertiary);
}
.impact-summary {
  display: flex;
  align-items: baseline;
  gap: var(--sp-6);
  flex-wrap: wrap;
}
.summary-item {
  display: flex;
  align-items: baseline;
  gap: var(--sp-1);
}
.summary-value {
  font-family: var(--font-mono);
  font-size: 18px;
  color: var(--text-primary);
}
.summary-value.is-warning {
  color: var(--state-warning);
}
.summary-label {
  font-size: 11px;
  color: var(--text-tertiary);
}
.summary-checked {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-tertiary);
}
.impact-note {
  margin: var(--sp-2) 0 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-tertiary);
}
.impact-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  margin: 0;
  padding: 0;
  list-style: none;
}
.impact-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-2);
  font-size: 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
}
.row-depth {
  flex: none;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-tertiary);
}
.row-type {
  flex: none;
  font-size: 11px;
  color: var(--text-tertiary);
}
.row-name {
  flex: none;
  max-width: 40%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-primary);
}
.row-reason {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
