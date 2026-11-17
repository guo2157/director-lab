<script setup lang="ts">
/**
 * 「提示词」标签页：视频提示词默认折叠为摘要，展开才编辑；
 * 负向提示词仅在当前工作流声明支持时出现（随本次生成提交，不单独保存）。
 */
import { NButton, useMessage } from 'naive-ui'
import PromptEditor from '@/components/ui/PromptEditor.vue'

defineProps<{
  negativeSupported: boolean
  defaultNegativePrompt: string
  saving: boolean
}>()

const emit = defineEmits<{ save: [] }>()

const videoPrompt = defineModel<string>('videoPrompt', { required: true })
const negativePrompt = defineModel<string>('negativePrompt', { required: true })

const message = useMessage()

function handleCopy() {
  message.success('提示词已复制')
}
</script>

<template>
  <div class="prompt-tab">
    <PromptEditor
      v-model="videoPrompt"
      label="视频提示词（MiniMax H3）"
      placeholder="尚未生成视频提示词"
      :rows="10"
      @copy="handleCopy"
    />
    <p class="field-hint">
      首帧指令 + integrated_multimodal_description / overall_soundscape / non_diegetic_music；
      串联生成时 &lt;Picture 1&gt; 为上一镜尾帧（本镜首帧），&lt;Picture 2&gt; 为本镜分镜图；
      @角色名/@场景名 自动携带定妆图与基准图。
    </p>

    <PromptEditor
      v-if="negativeSupported"
      v-model="negativePrompt"
      label="负向提示词"
      :default-text="defaultNegativePrompt"
      placeholder="未填写负向提示词"
      :rows="5"
      @copy="handleCopy"
    />
    <p v-if="negativeSupported" class="field-hint">
      负向提示词不单独保存，随本次生成提交；仅在当前工作流支持时传入。
    </p>

    <footer class="tab-foot">
      <span class="foot-hint">
        修改提示词会让该镜头已有视频标记为过期，需要重新生成才会替换。
      </span>
      <n-button size="small" :loading="saving" @click="emit('save')">保存提示词</n-button>
    </footer>
  </div>
</template>

<style scoped>
.prompt-tab {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.field-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-tertiary);
}
.tab-foot {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
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
@media (max-width: 768px) {
  .tab-foot {
    flex-wrap: wrap;
  }
  .foot-hint {
    flex: 1 1 100%;
  }
}
</style>
