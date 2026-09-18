<template>
  <v-app>
    <v-main class="shell">
      <div class="flex flex-col gap-3 p-4 h-full">
        <div class="flex flex-wrap items-center gap-3 controls">
          <CameraPreview v-model:video="video" />

          <v-select
            v-model="selected"
            class="w-60 min-w-0"
            density="compact"
            hide-details
            :items="cameras"
            label="Camera"
            @update:menu="onCameraMenu"
            @update:model-value="onCameraChange"
          />

          <v-btn
            :disabled="capturing"
            :loading="capturing"
            text="Capture"
            variant="tonal"
            @click="onCapture"
          />

          <v-select
            v-model="prompt"
            class="w-48 min-w-0"
            density="compact"
            hide-details
            :items="PROMPTS"
            label="Prompt"
            @update:model-value="onPromptChange"
          />

          <v-btn
            :disabled="solving"
            :loading="solving"
            text="Solve"
            variant="tonal"
            @click="onSolve"
          />

          <v-btn class="ml-auto" icon="mdi-cog" variant="text" @click="dialog = true" />
        </div>

        <div class="text-xs status controls" :title="status">{{ status }}</div>

        <v-tabs v-model="tab" class="controls">
          <v-tab text="Answer" value="answer" />
          <v-tab text="Transcript" value="transcript" />
        </v-tabs>

        <v-tabs-window v-model="tab" class="content">
          <v-tabs-window-item
            class="h-full"
            :reverse-transition="false"
            :transition="false"
            value="answer"
          >
            <AnswerPane :answer="answer" :status="answerStatus" />
          </v-tabs-window-item>

          <v-tabs-window-item
            class="h-full"
            :reverse-transition="false"
            :transition="false"
            value="transcript"
          >
            <TranscriptPane
              v-model="text"
              :error="error"
              @download="download"
            />
          </v-tabs-window-item>
        </v-tabs-window>
      </div>
    </v-main>

    <SettingsDialog v-model="dialog" @record="onRecord" @transcribed="onTranscribed" />
  </v-app>
</template>

<script lang="ts" setup>
  import { ref, watch } from 'vue'
  import AnswerPane from '@/components/AnswerPane.vue'
  import CameraPreview from '@/components/CameraPreview.vue'
  import SettingsDialog from '@/components/SettingsDialog.vue'
  import TranscriptPane from '@/components/TranscriptPane.vue'
  import { useCamera } from '@/composables/useCamera'
  import { useRecognition } from '@/composables/useRecognition'
  import { useTranscript } from '@/composables/useTranscript'
  import { PROMPTS, resolvePrompt } from '@/lib/prompt'
  import { loadSettings, saveSettings } from '@/lib/settings'
  import { type Answer, createModel, solve } from '@/lib/solver'

  const dialog = ref(true)
  const tab = ref('answer')
  const status = ref('Idle')
  const answerStatus = ref('No answer yet.')
  const answer = ref<Answer>()
  const solving = ref(false)
  const capturing = ref(false)
  const screenshot = ref('')
  const prompt = ref(loadSettings().prompt)

  const { cameras, selected, video, listCameras, startCamera, capture } = useCamera()
  const { text, addLine, setText, download } = useTranscript()
  const recognition = useRecognition(addLine)

  const error = recognition.error

  watch(error, message_ => {
    if (message_) {
      status.value = message_
    }
  })

  // Loading and download progress from the local model, which the status line
  // is the only place to show once the dialog has closed.
  watch(() => recognition.progress.value.detail, detail => {
    if (detail) {
      status.value = detail
    }
  })

  async function onRecord () {
    // Camera first: iOS Safari ties getUserMedia to the user gesture, and the
    // microphone prompt can consume it.
    let cameraError = ''
    try {
      await startCamera()
    } catch (error_) {
      cameraError = `Camera unavailable due to ${message(error_)}`
    }
    // Always enumerate, even when the camera failed - labels and deviceIds only
    // appear once permission is granted, and skipping this left the picker
    // permanently empty with no way to refresh it.
    try {
      await listCameras()
    } catch { /* enumeration is best-effort */ }

    // A session already running holds the microphone, so it goes first.
    await recognition.stop()
    await recognition.start()
    status.value = cameraError
      || recognition.error.value
      || 'Listening; transcribing on this device.'
  }

  async function onCameraMenu (open: boolean) {
    if (open) {
      await listCameras().catch(() => {})
    }
  }

  async function onTranscribed (transcribed: string[], name: string) {
    await recognition.stop()
    setText(transcribed.join('\n'))
    tab.value = 'transcript'
    dialog.value = false
    status.value = `Loaded ${transcribed.length} lines from ${name}.`
  }

  async function onCameraChange () {
    try {
      await startCamera()
    } catch (error_) {
      status.value = `Camera unavailable due to ${message(error_)}`
    }
  }

  function onPromptChange () {
    saveSettings({ prompt: prompt.value })
  }

  async function onCapture () {
    capturing.value = true
    try {
      const shot = await capture()
      if (shot) {
        screenshot.value = shot.base64
        status.value = `Captured ${shot.width}x${shot.height}, ${shot.bytes} bytes.`
      } else {
        status.value = 'Capture failed: no video frame available.'
      }
    } catch (error_) {
      status.value = `Capture failed due to ${message(error_)}`
    } finally {
      capturing.value = false
    }
  }

  async function onSolve () {
    const { apiKey, model } = loadSettings()
    if (!apiKey) {
      dialog.value = true
      return
    }

    solving.value = true
    tab.value = 'answer'
    answer.value = undefined
    answerStatus.value = 'Solving...'
    try {
      answer.value = await solve(
        createModel(apiKey, model),
        resolvePrompt(prompt.value).text,
        text.value,
        screenshot.value,
      )
    } catch (error_) {
      answerStatus.value = `Failed due to ${message(error_)}`
    } finally {
      solving.value = false
    }
  }

  function message (error_: unknown): string {
    return error_ instanceof Error ? error_.message : String(error_)
  }
</script>

<style scoped>
/* The shell owns the viewport; only the pane inside the tab scrolls, so an
   arriving answer never moves the controls above it. */
.shell {
  height: 100dvh;
}

.controls {
  flex: 0 0 auto;
}

.content {
  flex: 1 1 0;
  min-height: 0;
}

.content :deep(.v-window__container),
.content :deep(.v-window-item) {
  height: 100%;
}

/* Two lines, then an ellipsis. */
.status {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  overflow: hidden;
  height: 2.5rem;
}

:deep(.v-tabs-slider),
:deep(.v-tab__slider) {
  transition: none !important;
}
</style>
