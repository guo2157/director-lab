<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NButton, NSpace, NSpin, NEmpty, NTag, NDrawer, NDrawerContent,
  NInput, NTooltip, NPopconfirm, NPagination, useMessage,
} from 'naive-ui'
import {
  listAssets, deleteAsset, lockAsset, unlockAsset, rollbackAsset, uploadAsset, listAssetVersions,
} from '@/api/assets'
import type { Asset, AssetVersion } from '@/types/asset'

const route = useRoute()
const message = useMessage()
const queryClient = useQueryClient()

const projectId = computed(() => route.params.id as string)

// --- Category tree ---
const categories = [
  { label: '全部', value: '' },
  { label: '角色', value: 'portrait' },
  { label: '场景', value: 'baseline' },
  { label: '道具', value: 'anchor' },
  { label: '分镜图', value: 'keyframe' },
  { label: '视频', value: 'clip' },
  { label: '音频', value: 'audio' },
]
const activeCategory = ref('')
const keyword = ref('')
const page = ref(1)
const pageSize = 20

// --- Data fetching ---
const assetsQuery = useQuery({
  queryKey: ['assets', projectId, activeCategory, keyword, page],
  queryFn: () => listAssets({
    project_id: projectId.value,
    asset_type: activeCategory.value || undefined,
    keyword: keyword.value || undefined,
    page: page.value,
    page_size: pageSize,
  }),
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
  keyframe: '分镜图', clip: '视频', audio: '音频', portrait: '角色',
  anchor: '道具', baseline: '场景', reference: '参考图',
  first_frame: '首帧', end_frame: '末帧',
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
          @click="activeCategory = cat.value; page = 1"
        >
          {{ cat.label }}
        </div>
      </div>

      <!-- Main content -->
      <div class="assets-main">
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
          <n-empty v-else-if="assetsQuery.data.value?.items?.length === 0" description="暂无资产" />
          <div v-else class="asset-grid">
            <div
              v-for="asset in assetsQuery.data.value?.items || []"
              :key="asset.id"
              class="asset-card"
              @click="openDrawer(asset)"
            >
              <div class="asset-thumb">
                <img v-if="asset.thumb_url || asset.url" :src="asset.thumb_url || asset.url" alt="" />
                <div v-else class="thumb-placeholder">
                  <span>{{ assetTypeLabels[asset.asset_type] || asset.asset_type }}</span>
                </div>
                <div v-if="asset.version > 1" class="version-badge">v{{ asset.version }}</div>
                <div v-if="asset.locked" class="lock-icon">🔒</div>
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

        <!-- Pagination -->
        <n-pagination
          v-if="(assetsQuery.data.value?.total ?? 0) > pageSize"
          v-model:page="page"
          :page-count="Math.ceil((assetsQuery.data.value?.total ?? 0) / pageSize)"
          size="small"
          style="margin-top: 16px; justify-content: center"
        />
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
.assets-page { padding: 16px; min-height: calc(100vh - var(--app-header-height, 56px)); }
.assets-layout { display: flex; gap: 16px; }
.category-tree { width: 200px; flex-shrink: 0; }
.category-item {
  padding: 8px 12px; cursor: pointer; border-radius: 8px; font-size: 14px; color: var(--text-secondary);
  transition: all 0.2s; margin-bottom: 4px;
}
.category-item:hover { background: var(--bg-elevated); color: var(--text-primary); }
.category-item.active { background: var(--brand-primary); color: var(--text-on-brand, #fff); }
.assets-main { flex: 1; min-width: 0; }
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.upload-btn { cursor: pointer; }
.drop-zone { min-height: 400px; border: 2px dashed transparent; border-radius: 12px; }
.page-loading { display: flex; justify-content: center; padding: 80px 0; }
.asset-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
.asset-card {
  background: var(--bg-panel); border-radius: 12px; overflow: hidden; cursor: pointer;
  border: 1px solid var(--border-subtle); transition: box-shadow 0.2s; position: relative;
}
.asset-card:hover { box-shadow: 0 4px 16px rgba(0,0,0,0.4); }
.asset-thumb { width: 100%; height: 160px; position: relative; background: var(--bg-elevated); }
.asset-thumb img { width: 100%; height: 100%; object-fit: cover; }
.thumb-placeholder { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: var(--text-tertiary); }
.version-badge { position: absolute; top: 6px; right: 6px; background: var(--brand-primary); color: var(--text-on-brand, #fff); font-size: 11px; padding: 1px 6px; border-radius: 999px; }
.lock-icon { position: absolute; top: 6px; left: 6px; font-size: 14px; }
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
