<script setup lang="ts">
/**
 * 阶段跳转确认弹窗（资产库 → 分镜图 → 分镜看板 → 合成与导出 三处共用）。
 *
 * 职责边界（重要）：
 * - 本组件只负责「展示」，不做任何业务就绪判断。
 *   就绪状态由调用方查后端 stage-readiness 接口得到，再通过 props 传进来。
 * - 未就绪时展示阻断明细（缺哪些图/哪些镜头），确认按钮不可用；
 *   已就绪时展示进度统计与阶段说明，等待用户确认。
 *
 * 交互要求（任务书 §7）：
 * 1. 点击跳转按钮 → 重新获取最新就绪数据
 * 2. 未就绪 → 展示阻断原因，不弹确认
 * 3. 已就绪 → 弹窗
 * 4. 取消 → 停留当前页
 * 5. 确认 → 再次检查就绪（防弹窗期间状态变化），通过后才跳转
 * 6. 确认期间按钮 loading，禁止重复提交
 */
import { computed } from 'vue'
import { NButton, NIcon, NModal, NProgress } from 'naive-ui'
import { ArrowForwardOutline, WarningOutline } from '@vicons/ionicons5'

export interface StageBlockerItem {
  name: string
  reason: string
  /** 资产类阻断会带 asset_type（character/scene/prop），镜头类阻断带 shot_id */
  asset_type?: string
  shot_id?: string
  detail?: string
}

const props = withDefaults(
  defineProps<{
    visible: boolean
    /** 弹窗标题，如「确认进入分镜看板？」 */
    title: string
    /** 已就绪时的确认描述（多段用 \n 分隔） */
    description: string
    /** 确认按钮文案，如「确认，进入分镜看板」 */
    confirmText: string
    total: number
    done: number
    blockers?: StageBlockerItem[]
    /** 确认中：按钮 loading + 禁止重复点击 */
    loading?: boolean
    /** 后端权威就绪结论；未传时按 blockers 与 total 兜底推导 */
    ready?: boolean
    /** 阻断项最多展示几条（其余折叠成「等 N 项」） */
    maxBlockers?: number
  }>(),
  {
    blockers: () => [],
    loading: false,
    maxBlockers: 12,
  },
)

const emit = defineEmits<{
  'update:visible': [boolean]
  confirm: []
  cancel: []
}>()

// 就绪结论以后端为准：后端可能因「有在途任务但旧版本仍有效」等规则放行，
// 前端不能仅凭 blockers 是否为空自行推导，否则会与后端判定不一致。
const ready = computed(
  () => props.ready ?? (props.blockers.length === 0 && props.total > 0),
)
const percent = computed(() => (props.total ? Math.round((props.done / props.total) * 100) : 0))
const shownBlockers = computed(() => props.blockers.slice(0, props.maxBlockers))
const restCount = computed(() => Math.max(0, props.blockers.length - props.maxBlockers))
const descriptionLines = computed(() =>
  props.description.split('\n').map((l) => l.trim()).filter(Boolean),
)

/** 阻断原因 → 可读文案（不把后端枚举直接抛给用户） */
const REASON_LABELS: Record<string, string> = {
  asset_missing: '尚未生成',
  asset_generating: '正在生成',
  asset_invalid: '版本已失效',
  asset_file_missing: '文件缺失',
  shot_missing_asset: '尚未生成',
  shot_generating: '正在生成',
  shot_failed: '生成失败',
  shot_file_missing: '文件缺失',
  shot_asset_invalid: '版本已失效',
}
function reasonLabel(reason: string): string {
  return REASON_LABELS[reason] ?? '未就绪'
}

function onCancel() {
  if (props.loading) return
  emit('update:visible', false)
  emit('cancel')
}
</script>

<template>
  <n-modal
    :show="visible"
    preset="card"
    :title="title"
    :bordered="false"
    :closable="!loading"
    :mask-closable="!loading"
    :style="{ width: 'min(560px, 94vw)' }"
    @update:show="(v: boolean) => !loading && emit('update:visible', v)"
  >
    <!-- 进度统计：始终展示「做到哪了」，未就绪时它是缺失项的分母 -->
    <div class="st-progress">
      <span class="st-nums">{{ done }} / {{ total }}</span>
      <n-progress
        type="line"
        :percentage="percent"
        :height="6"
        :border-radius="3"
        :show-indicator="false"
        :color="ready ? 'var(--state-success)' : 'var(--brand-primary)'"
        rail-color="var(--border-subtle)"
      />
    </div>

    <!-- 未就绪：展示阻断明细，不提供确认入口 -->
    <template v-if="!ready">
      <p class="st-lead is-blocked">
        <n-icon :component="WarningOutline" :size="15" />
        还有 {{ blockers.length }} 项未完成，完成全部生成后才能进入下一阶段
      </p>
      <ul class="st-blockers">
        <li v-for="(b, i) in shownBlockers" :key="`${b.name}-${i}`" class="st-blocker">
          <span class="st-blocker-name">{{ b.name }}</span>
          <span class="st-blocker-reason">{{ reasonLabel(b.reason) }}</span>
        </li>
        <li v-if="restCount" class="st-blocker is-more">等 {{ restCount }} 项</li>
      </ul>
    </template>

    <!-- 已就绪：展示阶段说明，等待用户确认 -->
    <template v-else>
      <p v-for="(line, i) in descriptionLines" :key="i" class="st-lead">{{ line }}</p>
    </template>

    <template #footer>
      <div class="st-actions">
        <n-button size="small" :disabled="loading" @click="onCancel">取消</n-button>
        <n-button
          size="small"
          type="primary"
          :disabled="!ready"
          :loading="loading"
          @click="emit('confirm')"
        >
          <template v-if="ready">{{ confirmText }} <n-icon :component="ArrowForwardOutline" :size="14" /></template>
          <template v-else>完成生成后可进入</template>
        </n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.st-progress {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
}
.st-nums {
  flex: none;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-secondary);
}
.st-progress :deep(.n-progress) {
  flex: 1;
  min-width: 0;
}
.st-lead {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  margin: 0 0 var(--sp-2);
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-secondary);
}
.st-lead.is-blocked {
  color: var(--danger-text, var(--state-danger));
}
.st-blockers {
  margin: 0;
  padding: var(--sp-2) 0 0;
  list-style: none;
  max-height: 260px;
  overflow-y: auto;
  border-top: 1px solid var(--border-subtle);
}
.st-blocker {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: 3px 0;
  font-size: 12.5px;
}
.st-blocker-name {
  min-width: 0;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.st-blocker-reason {
  flex: none;
  color: var(--text-tertiary);
}
.st-blocker.is-more {
  justify-content: flex-start;
  color: var(--text-tertiary);
}
.st-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
}
</style>
