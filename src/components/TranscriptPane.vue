<template>
  <div class="flex flex-col h-full">
    <textarea
      ref="area"
      v-model="text"
      class="pane"
      :placeholder="error || 'No speech yet.'"
      spellcheck="false"
    />
  </div>
</template>

<script lang="ts" setup>
  import { nextTick, useTemplateRef, watch } from 'vue'

  const { error } = defineProps<{
    error: string
  }>()

  const text = defineModel<string>({ required: true })

  const area = useTemplateRef<HTMLTextAreaElement>('area')

  // Recognition appends while the user may be typing; rewriting the textarea
  // value drops the caret to the end, so put it back where it was.
  watch(text, async () => {
    const element = area.value
    if (!element || document.activeElement !== element) {
      return
    }
    const { selectionStart, selectionEnd } = element
    await nextTick()
    element.setSelectionRange(selectionStart, selectionEnd)
  })
</script>

<style scoped>
.pane {
  font-family: inherit;
  font-size: 9pt;
  flex: 1 1 0;
  min-height: 0;
  overflow-y: auto;
  resize: none;
  border: none;
  outline: none;
  background: transparent;
  color: inherit;
  white-space: pre-wrap;
}
</style>
