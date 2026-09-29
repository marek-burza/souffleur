<template>
  <pre ref="pane" class="pane">{{ body }}</pre>
</template>

<script lang="ts" setup>
  import type { Answer } from '@/lib/solver'
  import { computed, useTemplateRef, watch } from 'vue'

  const { answers, status } = defineProps<{
    answers: Answer[]
    status: string
  }>()

  const pane = useTemplateRef<HTMLElement>('pane')

  const body = computed(() => {
    if (answers.length === 0) {
      return status
    }
    return answers
      .map(answer => (answer.question ? `${answer.question}\n\n${answer.text}` : answer.text))
      .join('\n\n---\n\n')
  })

  watch(() => answers.length, () => {
    if (pane.value) {
      pane.value.scrollTop = 0
    }
  })
</script>

<style scoped>
.pane {
  white-space: pre-wrap;
  font-size: 9pt;
  margin: 0;
  height: 100%;
  overflow-y: auto;
}
</style>
