/**
 * composables/useTranscript.ts
 *
 * The transcript with one line per utterance, newest last.
 * Kept in LocalStorage, editable in place, and offered as a download.
 */

import { computed, ref, watch } from 'vue'
import { loadTranscript, saveTranscript } from '@/lib/session'

export function useTranscript () {
  const text = ref(loadTranscript())

  watch(text, saveTranscript)

  const isEmpty = computed(() => text.value.trim().length === 0)

  function addLine (line: string) {
    text.value = text.value ? `${text.value}\n${line}` : line
  }

  function setText (replacement: string) {
    text.value = replacement
  }

  function clear () {
    text.value = ''
  }

  function download () {
    const blob = new Blob([`${text.value}\n`], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'transcript.txt'
    anchor.click()
    URL.revokeObjectURL(url)
  }

  return { text, isEmpty, addLine, setText, clear, download }
}
