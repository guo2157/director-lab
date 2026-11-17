<script setup lang="ts">
/**
 * 「参考图」标签页：已绑定槽位用缩略图网格呈现（预览 + 绑定资产 + 启用 + 移除），
 * 未绑定槽位不再平铺成一排标签，统一收进「添加参考图」下拉。
 */
import { computed } from 'vue'
import { NButton, NDropdown, NSelect, NSwitch, NSpin, NTag } from 'naive-ui'
import type { DropdownOption } from 'naive-ui'
import EmptyState from '@/components/ui/EmptyState.vue'
import type { WorkspaceReference, WorkspaceSlot } from '@/api/regeneration'

const props = defineProps<{
  loading: boolean
  saving: boolean
  bindings: WorkspaceReference[]
  unboundSlots: WorkspaceSlot[]
  assetOptions: Array<{ label: string; value: string }>
  chainFrameStatus: 'ready' | 'missing' | 'not_applicable' | null
}>()

const emit = defineEmits<{
  save: []
  add: [slotParam: string]
  remove: [referenceId: string]
  'update-asset': [referenceId: string, assetId: string | null]
  'update-enabled': [referenceId: string, enabled: boolean]
}>()

const addOptions = computed<DropdownOption[]>(() =>
  props.unboundSlots.map((s) => ({
    key: s.param,
    label: `${s.param}（Img ${s.image_no ?? '—'}）`,
  })),
)

function handleAddSelect(key: string | number) {
  emit('add', String(key))
}

function thumbSrc(ref: WorkspaceReference): string | null {
  if (ref.image_url) return ref.image_url
  if (ref.asset_id) return `/api/assets/${ref.asset_id}/file`
  return null
}
</script>

<template>
  <div class="refs-tab">
    <n-spin :show="loading">
      <div v-if="bindings.length" class="ref-grid">
        <article v-for="ref in bindings" :key="ref.reference_id" class="ref-card">
          <div class="ref-thumb">
            <img
              v-if="thumbSrc(ref)"
              :src="thumbSrc(ref)!"
              :alt="ref.asset_name ?? ref.slot_id"
              loading="lazy"
            />
            <span v-else class="ref-thumb-empty">未绑定</span>
          </div>

          <div class="ref-head">
            <span class="ref-slot">{{ ref.slot_id }}</span>
            <n-tag v-if="ref.required" type="warning" size="tiny" :bordered="false">必需</n-tag>
            <span v-if="ref.image_no != null" class="ref-imgno">Img{{ ref.image_no }}</span>
          </div>
          <div class="ref-node">{{ ref.node_id ?? '未映射节点' }}</div>

          <n-select
            :value="ref.asset_id ?? null"
            :options="assetOptions"
            size="tiny"
            filterable
            clearable
            placeholder="选择绑定资产"
            @update:value="(v: string | null) => emit('update-asset', ref.reference_id, v)"
          />

          <div class="ref-actions">
            <label class="ref-toggle">
              <n-switch
                :value="ref.enabled !== false"
                size="small"
                @update:value="(v: boolean) => emit('update-enabled', ref.reference_id, v)"
              />
              <span>{{ ref.enabled !== false ? '已启用' : '已停用' }}</span>
            </label>
            <n-button text type="error" size="tiny" @click="emit('remove', ref.reference_id)">
              移除
            </n-button>
          </div>
        </article>
      </div>

      <EmptyState
        v-else
        compact
        title="暂无参考图绑定"
        description="从下方「添加参考图」选择工作流声明的槽位，绑定后随生成一起提交。"
      >
        <template #icon>▣</template>
      </EmptyState>
    </n-spin>

    <p v-if="chainFrameStatus === 'ready'" class="ref-hint">
      串联生成时本镜首帧（&lt;Picture 1&gt;）取上一镜实际尾帧，见 ref_prev_frame 绑定。
    </p>
    <p v-else-if="chainFrameStatus === 'missing'" class="ref-hint">
      串联生成时本镜首帧取上一镜实际尾帧；上一镜尚未生成视频尾帧，请先生成上一镜视频。
    </p>

    <footer class="tab-foot">
      <n-dropdown
        trigger="click"
        :options="addOptions"
        :disabled="!unboundSlots.length"
        @select="handleAddSelect"
      >
        <n-button size="small" secondary :disabled="!unboundSlots.length">
          添加参考图{{ unboundSlots.length ? `（${unboundSlots.length}）` : '' }}
        </n-button>
      </n-dropdown>
      <span class="foot-hint">槽位来自工作流真实定义，不支持凭空新增未声明的槽位。</span>
      <n-button size="small" :loading="saving" @click="emit('save')">保存绑定</n-button>
    </footer>
  </div>
</template>

<style scoped>
.refs-tab {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.ref-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: var(--sp-3);
}
.ref-card {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}
.ref-thumb {
  width: 100%;
  aspect-ratio: 16 / 9;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
}
.ref-thumb img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: var(--media-backdrop);
  display: block;
}
.ref-thumb-empty {
  font-size: 12px;
  color: var(--text-tertiary);
}
.ref-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.ref-slot {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ref-imgno {
  padding: 0 var(--sp-1);
  font-family: var(--font-mono);
  font-size: 10px;
  line-height: 16px;
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border-radius: var(--radius-sm);
}
.ref-node {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ref-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}
.ref-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: 11px;
  color: var(--text-secondary);
  cursor: pointer;
}
.ref-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-tertiary);
}
.tab-foot {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  padding-top: var(--sp-2);
  border-top: 1px solid var(--border-subtle);
}
.foot-hint {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-tertiary);
}
</style>
