<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NButton, NSpace, NSpin, NEmpty, NTag, NDrawer, NDrawerContent,
  NInput, NTooltip, NPopconfirm, useMessage,
} from 'naive-ui'
import {
  listAssets, deleteAsset, lockAsset, unlockAsset, rollbackAsset, uploadAsset, listAssetVersions,
} from '@/api/assets'
import type { Asset, AssetVersion } from '@/types/asset'
import { listCharacters, listProps, listScenes } from '@/api/script'
import { generateCharacterPortrait, generatePropAnchor, generateSceneBaseline } from '@/api/storyboard'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const queryClient = useQueryClient()

const projectId = computed(() => route.params.id as string)

// --- Category tree ---
// 分类值必须与后端权威枚举 enums.ASSET_TYPE 完全一致，否则后端过滤条件永远无匹配
const categories = [
  { label: '全部', value: '' },
  { label: '角色', value: 'character' },
  { label: '场景', value: 'scene' },
  { label: '道具', value: 'prop' },
]
const activeCategory = ref('')
const keyword = ref('')
const ALL_PAGE_SIZE = 100

// --- Data fetching ---
function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败，请稍后重试'
}
/** 全量拉取：按 100 条/页循环翻页直到取满 total，资产库一次性展示全部内容 */
async function listAllAssets() {
  const items: Asset[] = []
  let current = 1
  let total = 0
  for (;;) {
    const res = await listAssets({
      project_id: projectId.value,
      asset_type: activeCategory.value || undefined,
      keyword: keyword.value || undefined,
      page: current,
      page_size: ALL_PAGE_SIZE,
    })
    const batch = res?.items ?? []
    total = res?.total ?? items.length + batch.length
    items.push(...batch)
    if (!batch.length || items.length >= total) break
    current += 1
  }
  return { items, total }
}

const assetsQuery = useQuery({
  queryKey: ['assets', projectId, activeCategory, keyword],
  queryFn: () => listAllAssets(),
})

// --- Upload ---
const uploadMutation = useMutation({
  mutationFn: (file: File) => uploadAsset(file, { project_id: projectId.value }),
  onSuccess: () => {
    message.success('上传成功')
    queryClient.invalidateQueries({ queryKey: ['assets', projectId] })
  },
  onError: (e: any) => message.error(`上传失败: ${e?.message ?? e}`),
})

function handleFileDrop(e: DragEvent) {
  const files = e.dataTransfer?.files
  if (!files) return
  for (const file of Array.from(files)) {
    uploadMutation.mutate(file)
  }
}

function handleFileSelect(e: Event) {
  const input = e.target as HTMLInputElement
  if (!input.files) return
  for (const file of Array.from(input.files)) {
    uploadMutation.mutate(file)
  }
  input.value = ''
}

// --- Lock/Unlock ---
const lockMutation = useMutation({
  mutationFn: (params: { id: string; lock: boolean }) =>
    params.lock ? lockAsset(params.id) : unlockAsset(params.id),
  onSuccess: () => {
    message.success('操作成功')
    queryClient.invalidateQueries({ queryKey: ['assets', projectId] })
  },
  onError: (e: any) => message.error(`操作失败: ${e?.message ?? e}`),
})

// --- Delete ---
const deleteMutation = useMutation({
  mutationFn: (id: string) => deleteAsset(id),
  onSuccess: () => {
    message.success('删除成功')
    queryClient.invalidateQueries({ queryKey: ['assets', projectId] })
  },
  onError: (e: any) => message.error(`删除失败: ${e?.message ?? e}`),
})

// --- Drawer ---
const drawerVisible = ref(false)
const activeAsset = ref<Asset | null>(null)
const versions = ref<AssetVersion[]>([])
const loadingVersions = ref(false)

async function openDrawer(asset: Asset) {
  activeAsset.value = asset
  drawerVisible.value = true
  loadingVersions.value = true
  try {
    const res = await listAssetVersions(asset.id)
    versions.value = res
  } catch (e: any) {
    message.error(`加载版本失败: ${e?.message ?? e}`)
  } finally {
    loadingVersions.value = false
  }
}

const rollbackMutation = useMutation({
  mutationFn: (params: { id: string; version: number }) =>
    rollbackAsset(params.id, params.version),
  onSuccess: () => {
    message.success('回滚成功')
    queryClient.invalidateQueries({ queryKey: ['assets', projectId] })
  },
  onError: (e: any) => message.error(`回滚失败: ${e?.message ?? e}`),
})

const assetTypeLabels: Record<string, string> = {
  character: '角色', scene: '场景', prop: '道具',
  keyframe: '分镜图', clip: '视频', audio: '音频', last_frame: '末帧',
  reference: '参考图', first_frame: '首帧', end_frame: '末帧',
}

// --- 卡片状态点（生成状态角标） ---
const statusDotClass = (status?: string | null): string => {
  const s = (status ?? '').toLowerCase()
  if (['done', 'ready', 'success', 'completed', 'active'].includes(s)) return 'ok'
  if (['fail', 'failed', 'error'].includes(s)) return 'err'
  if (['pending', 'processing', 'generating', 'running', 'queued'].includes(s)) return 'busy'
  return 'idle'
}
const statusLabel = (status?: string | null): string => status || '未知状态'

// --- 拖拽资产（供分镜槽位绑定） ---
function handleAssetDragStart(e: DragEvent, asset: Asset) {
  if (!e.dataTransfer) return
  e.dataTransfer.setData('application/x-asset-id', asset.id)
  e.dataTransfer.setData('text/plain', asset.id)
  e.dataTransfer.effectAllowed = 'copy'
}

// --- 自适应卡片网格（ResizeObserver 动态列数） ---
const gridRef = ref<HTMLElement | null>(null)
const gridWidth = ref(0)
const MIN_CARD_WIDTH = 200
const MAX_CARD_WIDTH = 320
const GRID_GAP = 12
let gridObserver: ResizeObserver | null = null
onMounted(() => {
  if (!gridRef.value) return
  gridObserver = new ResizeObserver((entries) => {
    for (const entry of entries) gridWidth.value = entry.contentRect.width
  })
  gridObserver.observe(gridRef.value)
})
onBeforeUnmount(() => {
  gridObserver?.disconnect()
  gridObserver = null
})
const assetGridStyle = computed(() => {
  if (!gridWidth.value) return undefined
  const gap = GRID_GAP
  const maxColsByMin = Math.max(1, Math.floor((gridWidth.value + gap) / (MIN_CARD_WIDTH + gap)))
  const idealCols = Math.max(1, Math.round((gridWidth.value + gap) / (MAX_CARD_WIDTH + gap)))
  const cols = Math.min(Math.max(idealCols, 1), maxColsByMin)
  return { gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`, gap: `${gap}px` }
})

// --- 全部分组展示 ---
const groupedAssets = computed(() => {
  const items = assetsQuery.data.value?.items ?? []
  const groups = [
    { type: 'character', label: '角色', items: items.filter(a => a.asset_type === 'character') },
    { type: 'scene', label: '场景', items: items.filter(a => a.asset_type === 'scene') },
    { type: 'prop', label: '道具', items: items.filter(a => a.asset_type === 'prop') },
  ]
  const others = items.filter(a => !['character', 'scene', 'prop'].includes(a.asset_type))
  if (others.length) groups.push({ type: 'other', label: '其他', items: others })
  return groups.filter(g => g.items.length > 0)
})

// --- 资产齐全度检查（角色/场景/道具参考图生成进度） ---
/** 通用全量拉取：循环翻页直到取满 total（角色/场景/道具均展示全部条目） */
async function fetchAllPages<T>(
  fetcher: (params: { page: number; page_size: number }) => Promise<{ items?: T[]; total?: number }>,
): Promise<{ items: T[]; total: number }> {
  const items: T[] = []
  let current = 1
  for (;;) {
    const res = await fetcher({ page: current, page_size: ALL_PAGE_SIZE })
    const batch = res?.items ?? []
    const total = res?.total ?? items.length + batch.length
    items.push(...batch)
    if (!batch.length || items.length >= total) return { items, total }
    current += 1
  }
}

const { data: charsData } = useQuery({
  queryKey: ['characters', projectId],
  queryFn: () => fetchAllPages((p) => listCharacters(projectId.value, p)),
  enabled: computed(() => !!projectId.value),
})
const { data: scenesData } = useQuery({
  queryKey: ['scenes', projectId],
  queryFn: () => fetchAllPages((p) => listScenes(projectId.value, { ...p, include_shots: false })),
  enabled: computed(() => !!projectId.value),
})
const { data: propsData } = useQuery({
  queryKey: ['props', projectId],
  queryFn: () => fetchAllPages((p) => listProps(projectId.value, p)),
  enabled: computed(() => !!projectId.value),
})
const readiness = computed(() => {
  const cs = charsData.value?.items ?? []
  const ss = (scenesData.value?.items ?? []) as Array<{ baseline_asset_id?: string | null }>
  const ps = propsData.value?.items ?? []
  const total = cs.length + ss.length + ps.length
  const done =
    cs.filter((c) => !!c.portrait_asset_id).length +
    ss.filter((s) => !!s.baseline_asset_id).length +
    ps.filter((p) => !!p.anchor_asset_id).length
  return { total, done, all: total > 0 && done === total }
})
// --- 制作资产区（公共资产生成：角色定妆图/场景基准图/道具锚定图） ---
const craftingId = ref<string | null>(null)
const batchCrafting = ref(false)
const genPortrait = useMutation({
  mutationFn: (id: string) => generateCharacterPortrait(id),
  onSuccess: () => {
    message.success('角色定妆图任务已提交')
    queryClient.invalidateQueries({ queryKey: ['characters', projectId] })
  },
  onError: (e: any) => message.error(`生成失败: ${e?.message ?? e}`),
})
const genBaseline = useMutation({
  mutationFn: (id: string) => generateSceneBaseline(id),
  onSuccess: () => {
    message.success('场景基准图任务已提交')
    queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
  },
  onError: (e: any) => message.error(`生成失败: ${e?.message ?? e}`),
})
const genAnchor = useMutation({
  mutationFn: (id: string) => generatePropAnchor(id),
  onSuccess: () => {
    message.success('道具锚定图任务已提交')
    queryClient.invalidateQueries({ queryKey: ['props', projectId] })
  },
  onError: (e: any) => message.error(`生成失败: ${e?.message ?? e}`),
})
interface CraftItem {
  id: string
  name: string
  prompt: string
  done: boolean
  kind: 'character' | 'scene' | 'prop'
}
const craftGroups = computed(() => {
  const cs = (charsData.value?.items ?? []) as Array<{ id: string; name: string; description?: string; portrait_asset_id?: string | null }>
  const ss = (scenesData.value?.items ?? []) as Array<{ id: string; title?: string; summary?: string; baseline_asset_id?: string | null }>
  const ps = (propsData.value?.items ?? []) as Array<{ id: string; name: string; description?: string; anchor_asset_id?: string | null }>
  return [
    {
      type: 'character', label: '角色', unit: '定妆图',
      items: cs.map((c) => ({ id: c.id, name: c.name, prompt: c.description ?? '', done: !!c.portrait_asset_id, kind: 'character' as const })),
    },
    {
      type: 'scene', label: '场景', unit: '基准图',
      items: ss.map((s) => ({ id: s.id, name: s.title ?? s.id, prompt: s.summary ?? '', done: !!s.baseline_asset_id, kind: 'scene' as const })),
    },
    {
      type: 'prop', label: '道具', unit: '锚定图',
      items: ps.map((p) => ({ id: p.id, name: p.name, prompt: p.description ?? '', done: !!p.anchor_asset_id, kind: 'prop' as const })),
    },
  ]
})
/** 当前分类下可见的制作资产分组：全部=三组（仅展示分类卡片），单分类=仅该分类条目 */
const visibleCraftGroups = computed(() => {
  if (!activeCategory.value) return craftGroups.value
  return craftGroups.value.filter((g) => g.type === activeCategory.value)
})
function handleCraft(it: CraftItem) {
  craftingId.value = it.id
  const onSettled = () => { craftingId.value = null }
  if (it.kind === 'character') genPortrait.mutate(it.id, { onSettled })
  else if (it.kind === 'scene') genBaseline.mutate(it.id, { onSettled })
  else genAnchor.mutate(it.id, { onSettled })
}
function handleBatchCraft() {
  let count = 0
  for (const grp of visibleCraftGroups.value) {
    for (const it of grp.items) {
      if (it.done) continue
      count++
      if (it.kind === 'character') genPortrait.mutate(it.id)
      else if (it.kind === 'scene') genBaseline.mutate(it.id)
      else genAnchor.mutate(it.id)
    }
  }
  if (!count) { message.info('资产图已全部生成，无需重复提交'); return }
  batchCrafting.value = true
  message.success(`已提交 ${count} 个资产生成任务，生成进度见齐全度横幅`)
  batchCrafting.value = false
}
</script>

<template>
  <div class="assets-page">
    <div class="assets-layout">
      <!-- Category tree -->
      <div class="category-tree">
        <div
          v-for="cat in categories"
          :key="cat.value"
          class="category-item"
          :class="{ active: activeCategory === cat.value }"
          @click="activeCategory = cat.value"
        >
          {{ cat.label }}
        </div>
      </div>

      <!-- Main content -->
      <div class="assets-main">
        <!-- 资产齐全度横幅 -->
        <div class="readiness-banner" :class="{ done: readiness.all }">
          <span class="readiness-badge">{{ readiness.all ? '✓' : '⏳' }}</span>
          <span class="readiness-text">
            {{ readiness.all ? '资产图已齐全，可进入分镜看板生成视频' : `资产图生成中：${readiness.done}/${readiness.total} 已完成` }}
          </span>
          <n-button size="tiny" quaternary @click="router.push(`/project/${projectId}/parse`)">返回解析页</n-button>
                    <n-button size="tiny" type="primary" :disabled="!readiness.all" @click="router.push(`/project/${projectId}/storyboard`)">去分镜看板 →</n-button>
        </div>
        <!-- 制作资产区（公共资产生成） -->
        <div class="craft-section">
          <div class="craft-head">
            <span class="craft-title">制作资产区</span>
            <span class="craft-sub">角色定妆图 / 场景基准图 / 道具锚定图（项目级公共资产，按名称绑定供分镜引用）</span>
            <n-button size="tiny" :loading="batchCrafting" @click="handleBatchCraft">批量生成缺失资产图</n-button>
          </div>
          <div class="craft-body">
          <div v-for="grp in visibleCraftGroups" :key="grp.type" class="craft-group">
            <div class="craft-group-head">
              <span class="craft-group-label">{{ grp.label }}（{{ grp.items.length }}）</span>
              <span class="craft-group-done">{{ grp.items.filter((i) => i.done).length }}/{{ grp.items.length }} 已生成</span>
            </div>
            <!-- 全部：仅展示分类汇总卡片 -->
            <div v-if="activeCategory === ''" class="craft-grid">
              <div class="craft-card category-card" @click="activeCategory = grp.type">
                <div class="craft-card-head">
                  <span class="craft-card-name">{{ grp.label }}</span>
                  <n-tag size="tiny" :type="grp.items.length && grp.items.every((i) => i.done) ? 'success' : 'warning'" :bordered="false">
                    {{ grp.items.filter((i) => i.done).length }}/{{ grp.items.length }} 已生成
                  </n-tag>
                </div>
                <p class="craft-card-prompt">点击进入{{ grp.label }}分类，查看并生成{{ grp.unit }}</p>
              </div>
            </div>
            <!-- 单分类：展示该分类需要生成的图片卡片 -->
            <div v-else-if="grp.items.length" class="craft-grid">
              <div v-for="it in grp.items" :key="it.id" class="craft-card" :class="{ done: it.done }">
                <div class="craft-card-head">
                  <span class="craft-card-name">{{ it.name }}</span>
                  <n-tag v-if="it.done" size="tiny" type="success" :bordered="false">已生成</n-tag>
                  <n-button v-else size="tiny" quaternary type="primary" :loading="craftingId === it.id" @click="handleCraft(it)">生成{{ grp.unit }}</n-button>
                </div>
                <p class="craft-card-prompt" :title="it.prompt">{{ it.prompt || '暂无提示词描述' }}</p>
              </div>
            </div>
            <n-empty v-else size="small" :description="`暂无${grp.label}`" />
          </div>
          </div>
          <n-empty v-if="!readiness.total" size="small" description="解析完成后此处展示角色/场景/道具实体" />
        </div>
        <!-- Toolbar -->
        <div class="toolbar">
          <n-input v-model:value="keyword" placeholder="搜索资产..." size="small" style="width: 200px" clearable />
          <n-space>
            <label class="upload-btn">
              <input type="file" multiple @change="handleFileSelect" style="display:none" />
              <n-button size="small" type="primary" :loading="uploadMutation.isPending.value">上传资产</n-button>
            </label>
          </n-space>
        </div>

        <!-- Upload drop zone -->
        <div class="drop-zone" @dragover.prevent @drop.prevent="handleFileDrop">
          <n-spin v-if="assetsQuery.isLoading.value" class="page-loading" />
          <div v-else-if="assetsQuery.isError.value" class="state-block">
            <p class="state-text">资产加载失败：{{ errMsg(assetsQuery.error.value) }}</p>
            <n-button secondary @click="() => assetsQuery.refetch()">重试</n-button>
          </div>
          <n-empty v-else-if="assetsQuery.data.value?.items?.length === 0" description="暂无资产" />
          <!-- 全部：按类型分组展示 -->
          <div v-else-if="activeCategory === ''" class="asset-groups">
            <div v-for="grp in groupedAssets" :key="grp.type" class="asset-group">
              <div class="asset-group-head">
                <span class="asset-group-label">{{ grp.label }}</span>
                <span class="asset-group-count">{{ grp.items.length }} 项</span>
              </div>
              <div class="asset-group-grid">
                <div
                  v-for="(asset, idx) in grp.items"
                  :key="asset.id"
                  class="asset-card"
                  :class="{ 'is-portrait': asset.asset_type === 'character' }"
                  :style="{ animationDelay: `${idx * 0.05}s` }"
                  draggable="true"
                  @dragstart="handleAssetDragStart($event, asset)"
                  @click="openDrawer(asset)"
                >
                  <div class="asset-thumb">
                    <img v-if="asset.thumb_url || asset.url" :src="asset.thumb_url || asset.url" alt="" />
                    <div v-else class="thumb-placeholder">
                      <span>{{ assetTypeLabels[asset.asset_type] || asset.asset_type }}</span>
                    </div>
                    <div v-if="asset.version > 1" class="version-badge">v{{ asset.version }}</div>
                    <div v-if="asset.locked" class="lock-icon">🔒</div>
                    <div class="status-dot" :class="statusDotClass(asset.status)" :title="statusLabel(asset.status)"></div>
                  </div>
                  <div class="asset-info">
                    <div class="asset-name">{{ asset.id.split('_').slice(1).join('_') || asset.id }}</div>
                    <div class="asset-meta">
                      <n-tag size="tiny" round>{{ assetTypeLabels[asset.asset_type] || asset.asset_type }}</n-tag>
                      <span class="ref-count">引用 {{ asset.reference_count ?? 0 }}</span>
                    </div>
                  </div>
                  <div class="asset-actions" @click.stop>
                    <n-tooltip>
                      <template #trigger>
                        <n-button
                          size="tiny"
                          quaternary
                          @click="lockMutation.mutate({ id: asset.id, lock: !asset.locked })"
                        >{{ asset.locked ? '🔓' : '🔒' }}</n-button>
                      </template>
                      {{ asset.locked ? '解锁' : '锁定' }}
                    </n-tooltip>
                    <n-popconfirm v-if="!asset.locked" @positive-click="deleteMutation.mutate(asset.id)">
                      <template #trigger>
                        <n-button size="tiny" quaternary type="error">🗑</n-button>
                      </template>
                      确认删除此资产？
                    </n-popconfirm>
                    <n-tooltip v-else>
                      <template #trigger>
                        <n-button size="tiny" quaternary disabled>🗑</n-button>
                      </template>
                      锁定状态不可删除
                    </n-tooltip>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <!-- 单分类：扁平网格 -->
          <div v-else ref="gridRef" class="asset-grid" :style="assetGridStyle">
            <div
              v-for="(asset, idx) in assetsQuery.data.value?.items || []"
              :key="asset.id"
              class="asset-card"
              :class="{ 'is-portrait': asset.asset_type === 'character' }"
              :style="{ animationDelay: `${idx * 0.05}s` }"
              draggable="true"
              @dragstart="handleAssetDragStart($event, asset)"
              @click="openDrawer(asset)"
            >
              <div class="asset-thumb">
                <img v-if="asset.thumb_url || asset.url" :src="asset.thumb_url || asset.url" alt="" />
                <div v-else class="thumb-placeholder">
                  <span>{{ assetTypeLabels[asset.asset_type] || asset.asset_type }}</span>
                </div>
                <div v-if="asset.version > 1" class="version-badge">v{{ asset.version }}</div>
                <div v-if="asset.locked" class="lock-icon">🔒</div>
                <div class="status-dot" :class="statusDotClass(asset.status)" :title="statusLabel(asset.status)"></div>
              </div>
              <div class="asset-info">
                <div class="asset-name">{{ asset.id.split('_').slice(1).join('_') || asset.id }}</div>
                <div class="asset-meta">
                  <n-tag size="tiny" round>{{ assetTypeLabels[asset.asset_type] || asset.asset_type }}</n-tag>
                  <span class="ref-count">引用 {{ asset.reference_count ?? 0 }}</span>
                </div>
              </div>
              <div class="asset-actions" @click.stop>
                <n-tooltip>
                  <template #trigger>
                    <n-button
                      size="tiny"
                      quaternary
                      @click="lockMutation.mutate({ id: asset.id, lock: !asset.locked })"
                    >{{ asset.locked ? '🔓' : '🔒' }}</n-button>
                  </template>
                  {{ asset.locked ? '解锁' : '锁定' }}
                </n-tooltip>
                <n-popconfirm v-if="!asset.locked" @positive-click="deleteMutation.mutate(asset.id)">
                  <template #trigger>
                    <n-button size="tiny" quaternary type="error">🗑</n-button>
                  </template>
                  确认删除此资产？
                </n-popconfirm>
                <n-tooltip v-else>
                  <template #trigger>
                    <n-button size="tiny" quaternary disabled>🗑</n-button>
                  </template>
                  锁定状态不可删除
                </n-tooltip>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>

    <!-- Drawer -->
    <n-drawer v-model:show="drawerVisible" :width="480">
      <n-drawer-content title="资产详情" closable>
        <template v-if="activeAsset">
          <div class="drawer-preview">
            <img v-if="activeAsset.url" :src="activeAsset.url" alt="" class="preview-img" />
            <div v-else class="preview-placeholder">暂无预览</div>
          </div>
          <n-space vertical :size="12">
            <div><span class="field-label">ID：</span>{{ activeAsset.id }}</div>
            <div><span class="field-label">类型：</span>{{ assetTypeLabels[activeAsset.asset_type] || activeAsset.asset_type }}</div>
            <div><span class="field-label">状态：</span>{{ activeAsset.status }}</div>
            <div><span class="field-label">引用次数：</span>{{ activeAsset.reference_count ?? 0 }}</div>

            <div class="version-section">
              <div class="section-title">版本时间线</div>
              <n-spin v-if="loadingVersions" size="small" />
              <div v-else class="version-list">
                <div v-for="v in versions" :key="v.version" class="version-item">
                  <span class="version-num">v{{ v.version }}</span>
                  <span class="version-date">{{ new Date(v.created_at).toLocaleString() }}</span>
                  <n-button
                    v-if="v.version !== activeAsset.version"
                    size="tiny"
                    quaternary
                    @click="rollbackMutation.mutate({ id: activeAsset.id, version: v.version })"
                  >回滚</n-button>
                </div>
              </div>
            </div>
          </n-space>
        </template>
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<style scoped>
.readiness-banner { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 10px; padding: 10px 14px; margin-bottom: 12px; background: var(--bg-panel, #fff); }
.readiness-banner.done { border-color: rgba(34, 197, 94, 0.35); background: rgba(34, 197, 94, 0.06); }
.readiness-badge { font-size: 14px; }
.readiness-text { font-size: 13px; color: var(--text-primary, #333); margin-right: auto; }
.assets-page {
  padding: 16px; min-height: calc(100vh - var(--app-header-height, 56px));
  /* 设计令牌（局部，暗色主题变量化预留） */
  --asset-card-radius: 12px;
  --asset-thumb-height: 160px;
  --asset-portrait-thumb-width: 120px;
  --asset-gap: 12px;
  --asset-enter-duration: 0.3s;
}
.state-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 48px 24px;
}
.state-text {
  margin: 0;
  color: var(--text-secondary);
  font-size: 14px;
}
.assets-layout { display: flex; gap: 16px; }
.category-tree { width: 200px; flex-shrink: 0; }
.category-item {
  padding: 8px 12px; cursor: pointer; border-radius: 8px; font-size: 14px; color: var(--text-secondary);
  transition: all 0.2s; margin-bottom: 4px;
}
.category-item:hover { background: var(--bg-elevated); color: var(--text-primary); }
.category-item.active { background: var(--brand-primary); color: var(--text-on-brand, #fff); }
.assets-main { flex: 1; min-width: 0; }
.craft-section { border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 10px; padding: 12px 14px; margin-bottom: 12px; background: var(--bg-panel, #fff); display: flex; flex-direction: column; gap: 10px; }
.craft-card.category-card { cursor: pointer; transition: border-color 0.2s; }
.craft-card.category-card:hover { border-color: var(--brand-primary, #6366f1); }
.craft-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.craft-title { font-size: 14px; font-weight: 600; color: var(--text-primary, #333); }
.craft-sub { font-size: 12px; color: var(--text-secondary, #64748b); margin-right: auto; }
.craft-body { display: flex; gap: 12px; align-items: stretch; flex-wrap: wrap; }
.craft-group { flex: 1 1 260px; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.craft-group-head { display: flex; align-items: center; justify-content: space-between; }
.craft-group-label { font-size: 13px; font-weight: 600; color: var(--text-primary, #333); }
.craft-group-done { font-size: 12px; color: var(--text-secondary, #64748b); }
.craft-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px; }
.craft-card { border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 8px; padding: 8px 10px; display: flex; flex-direction: column; gap: 6px; }
.craft-card.done { border-color: rgba(34, 197, 94, 0.35); background: rgba(34, 197, 94, 0.04); }
.craft-card-head { display: flex; align-items: center; gap: 8px; }
.craft-card-name { font-size: 13px; font-weight: 600; color: var(--text-primary, #333); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.craft-card-prompt { font-size: 12px; color: var(--text-secondary, #64748b); line-height: 1.5; margin: 0; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.upload-btn { cursor: pointer; }
.drop-zone { min-height: 400px; border: 2px dashed transparent; border-radius: 12px; }
.page-loading { display: flex; justify-content: center; padding: 80px 0; }
.asset-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: var(--asset-gap); }
.asset-groups { display: flex; flex-direction: column; gap: 20px; }
.asset-group { display: flex; flex-direction: column; gap: 8px; }
.asset-group-head { display: flex; align-items: center; gap: 8px; padding-bottom: 4px; border-bottom: 1px solid var(--border-subtle, #e5e7eb); }
.asset-group-label { font-size: 14px; font-weight: 600; color: var(--text-primary, #333); }
.asset-group-count { font-size: 12px; color: var(--text-secondary, #64748b); }
.asset-group-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: var(--asset-gap); }
.asset-card {
  background: var(--bg-panel); border-radius: var(--asset-card-radius); overflow: hidden; cursor: pointer;
  border: 1px solid var(--border-subtle); transition: box-shadow 0.2s; position: relative;
  animation: asset-card-in var(--asset-enter-duration) ease both;
}
.asset-card:hover { box-shadow: 0 4px 16px rgba(0,0,0,0.4); }
@keyframes asset-card-in {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
/* 角色卡片横向布局：缩略图左 + 信息右 */
.asset-card.is-portrait { display: flex; align-items: stretch; }
.asset-card.is-portrait .asset-thumb { width: var(--asset-portrait-thumb-width); height: auto; flex-shrink: 0; }
.asset-card.is-portrait .asset-thumb img { object-fit: cover; }
.asset-card.is-portrait .asset-info { flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: center; }
.asset-card.is-portrait .asset-actions { position: static; align-self: center; padding: 0 8px 0 0; }
.asset-thumb { width: 100%; height: var(--asset-thumb-height); position: relative; background: var(--bg-elevated); }
.asset-thumb img { width: 100%; height: 100%; object-fit: cover; }
.thumb-placeholder { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: var(--text-tertiary); }
.version-badge { position: absolute; top: 6px; right: 6px; background: var(--brand-primary); color: var(--text-on-brand, #fff); font-size: 11px; padding: 1px 6px; border-radius: 999px; }
.lock-icon { position: absolute; top: 6px; left: 6px; font-size: 14px; }
.status-dot { position: absolute; bottom: 6px; left: 6px; width: 8px; height: 8px; border-radius: 50%; border: 2px solid var(--bg-panel, #fff); }
.status-dot.ok { background: var(--state-success, #22c55e); }
.status-dot.busy { background: #f0a020; animation: status-pulse 1.2s ease-in-out infinite; }
.status-dot.err { background: #d03050; }
.status-dot.idle { background: #c2c8d0; }
@keyframes status-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.asset-info { padding: 8px 12px; }
.asset-name { font-size: 13px; color: var(--text-primary); margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.asset-meta { display: flex; align-items: center; gap: 8px; }
.ref-count { font-size: 12px; color: var(--text-secondary); }
.asset-actions { position: absolute; bottom: 6px; right: 6px; display: flex; gap: 4px; }
.drawer-preview { margin-bottom: 16px; }
.preview-img { width: 100%; border-radius: 8px; }
.preview-placeholder { width: 100%; height: 200px; background: var(--bg-elevated); border-radius: 8px; display: flex; align-items: center; justify-content: center; color: var(--text-tertiary); }
.field-label { color: var(--text-secondary); font-size: 13px; }
.version-section { margin-top: 8px; }
.section-title { font-size: 14px; color: var(--text-primary); margin-bottom: 8px; font-weight: 500; }
.version-list { display: flex; flex-direction: column; gap: 8px; }
.version-item { display: flex; align-items: center; gap: 8px; padding: 6px 8px; background: var(--bg-elevated); border-radius: 8px; }
.version-num { font-size: 13px; color: var(--brand-primary); font-weight: 600; min-width: 32px; }
.version-date { font-size: 12px; color: var(--text-secondary); flex: 1; }
@media (max-width: 768px) {
  .assets-layout { flex-direction: column; }
  .category-tree { width: 100%; display: flex; gap: 8px; overflow-x: auto; }
  .category-item { margin-bottom: 0; white-space: nowrap; }
}
</style>
