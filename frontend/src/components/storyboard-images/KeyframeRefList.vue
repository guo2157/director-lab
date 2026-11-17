<script setup lang="ts">
/**
 * 详情左栏「生成参考图」：真实入参排前，推导出的按来源打标。
 * 每种来源都有文字标注，不靠颜色单独表意。
 */
import { NImage, NSpin } from 'naive-ui'
import type { KeyframeRefItem } from '@/composables/useKeyframeRefList'

defineProps<{
  items: KeyframeRefItem[]
  loading: boolean
  maxRefs: number
}>()
</script>

<template>
  <n-spin :show="loading">
    <div v-if="items.length" class="kf-ref-list">
      <div v-for="r in items" :key="r.key" class="kf-ref-item">
        <n-image
          v-if="r.url"
          :src="r.url"
          :width="52"
          :height="52"
          object-fit="cover"
          class="kf-ref-thumb"
          :img-props="{ style: 'width:52px;height:52px;object-fit:cover;border-radius:var(--radius)' }"
        />
        <div v-else class="kf-ref-thumb kf-ref-empty">未生成</div>
        <div class="kf-ref-meta">
          <div class="kf-ref-name">
            {{ r.name }}
            <span v-if="r.injected" class="kf-tag ok">真实入参</span>
            <span v-else-if="r.assetMissing" class="kf-tag warn">图未生成</span>
            <span v-else-if="r.noSlot" class="kf-tag warn">无可用槽位</span>
            <span v-else class="kf-tag plan">按槽位推导</span>
          </div>
          <div class="kf-ref-role">
            {{ r.roleLabel }}
            <span v-if="r.slot" class="kf-ref-slot">{{ r.slot }}</span>
            <span v-if="r.required" class="kf-ref-req">必需</span>
          </div>
          <div class="kf-ref-input">输入位置 {{ r.inputKey }}</div>
        </div>
      </div>
    </div>
    <div v-else class="detail-hint-block">
      该镜头暂无可展示的参考图：{{
        maxRefs
          ? '提示词中没有可注入的 @角色 / @场景 / @道具引用，请先补齐对应资产'
          : '当前生图工作流为纯文生图，未声明参考图槽位'
      }}。
    </div>
  </n-spin>
</template>

<style scoped>
.kf-ref-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.kf-ref-item {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-panel);
}
.kf-ref-thumb {
  flex: none;
}
.kf-ref-empty {
  width: 52px;
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--border-subtle);
  border-radius: var(--radius);
  font-size: 11px;
  color: var(--text-tertiary);
}
.kf-ref-meta {
  flex: 1;
  min-width: 0;
}
.kf-ref-name {
  font-size: 13px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kf-ref-role {
  margin-top: 3px;
  font-size: 11px;
  color: var(--text-tertiary);
}
.kf-ref-slot {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  background: var(--bg-sunken);
  font-family: var(--font-mono);
  font-size: 10px;
}
.kf-ref-req {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  background: var(--warning-soft);
  color: var(--accent-amber);
}
.kf-ref-input {
  margin-top: 2px;
  font-size: 10px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}
.kf-tag {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-size: 10px;
  font-weight: 400;
}
.kf-tag.ok {
  background: var(--success-soft);
  color: var(--state-success);
}
.kf-tag.plan {
  background: var(--brand-soft);
  color: var(--brand-hover);
}
.kf-tag.warn {
  background: var(--warning-soft);
  color: var(--accent-amber);
}
.detail-hint-block {
  padding: var(--sp-3);
  border: 1px dashed var(--border-subtle);
  border-radius: var(--radius-lg);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-tertiary);
}
@media (max-width: 768px) {
  .kf-ref-item {
    gap: var(--sp-2);
    padding: var(--sp-2) var(--sp-1);
  }
  .kf-ref-name {
    white-space: normal;
  }
}
</style>
