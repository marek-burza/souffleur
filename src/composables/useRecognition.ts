/**
 * composables/useRecognition.ts
 *
 * Live transcription that runs entirely in the page: the microphone is captured
 * with `getUserMedia`, split into utterances by the energy VAD, and each
 * utterance is transcribed on the device through Transformers.js. Which model
 * that is depends on what the device can run - see `lib/liveModel.ts`.
 *
 * Nothing here leaves the device, so the only thing it needs is a microphone,
 * which is what makes it run in any browser `getUserMedia` does.
 *
 * What it costs: the model is downloaded on first use (cached afterwards), and
 * a line appears when its utterance ends rather than while it is being spoken,
 * so a line lands roughly one pause plus one decode after it is said.
 */

import type { Progress } from '@/lib/whisper'
import { ref, shallowRef } from 'vue'
import { loadRecognizer, type Recognizer } from '@/lib/liveModel'
import { openMicrophone } from '@/lib/micStream'
import { FrameSplitter, VadAccumulator } from '@/lib/vad'

/**
 * Segments waiting to be decoded. Reaching this means transcription is running
 * slower than speech, and the backlog would grow without bound - better to lose
 * the oldest utterance than to fall further behind on every one after it.
 */
const MAX_PENDING = 4

// How often the level readout in the status line is refreshed. Slow enough to
// be readable, fast enough to show that the microphone is live.
const LEVEL_INTERVAL_MS = 500

export function useRecognition (onLine: (text: string) => void) {
  const listening = ref(false)
  const error = ref('')
  const progress = ref<Progress>({ ratio: -1, detail: '' })

  const transcriber = shallowRef<Recognizer>()
  let microphone: Awaited<ReturnType<typeof openMicrophone>> | undefined
  let active = false

  const vad = new VadAccumulator()
  const splitter = new FrameSplitter(vad.frameSamples)
  let levelShownAt = 0
  const pending: Float32Array[] = []
  let draining: Promise<void> | undefined
  let dropped = 0

  function enqueue (segment: Float32Array) {
    pending.push(segment)
    if (pending.length > MAX_PENDING) {
      pending.shift()
      dropped += 1
      error.value = `Transcription is behind; dropped ${dropped} segment(s).`
    }
    draining ??= drain().finally(() => {
      draining = undefined
    })
  }

  /**
   * One decode at a time: the pipeline is not reentrant, and overlapping calls
   * would only make every utterance slower.
   */
  async function drain () {
    while (pending.length > 0) {
      const segment = pending.shift()!
      const instance = transcriber.value
      if (!instance) {
        continue
      }
      try {
        const text = await instance.transcribe(segment)
        if (text) {
          onLine(text)
        }
      } catch (error_) {
        error.value = `Transcription failed: ${message(error_)}`
      }
    }
  }

  async function start () {
    if (active) {
      return
    }
    active = true
    error.value = ''
    dropped = 0

    try {
      progress.value = { ratio: -1, detail: 'Loading speech model...' }
      transcriber.value ??= await loadRecognizer(update => {
        progress.value = update
      })
      if (!active) {
        return
      }

      const device = transcriber.value.label
      progress.value = { ratio: -1, detail: `Listening on ${device}.` }
      microphone = await openMicrophone(block => {
        splitter.split(block, frame => {
          const segment = vad.feed(frame)
          if (segment) {
            enqueue(segment)
          }
        })
        showLevel(device)
      })
      if (!active) {
        // Stopped while the permission prompt was up.
        await microphone.stop()
        microphone = undefined
        return
      }
      listening.value = true
    } catch (error_) {
      active = false
      listening.value = false
      error.value = `Recording unavailable due to ${message(error_)}`
    }
  }

  /**
   * The VAD threshold follows the room, so it cannot be reasoned about from the
   * transcript alone. Showing it alongside the current level is what makes the
   * multiples and the window tunable against a real recording; drop this once
   * they are settled. Held back while an error is showing, since that message
   * matters more than the meter.
   */
  function showLevel (device: string) {
    const now = Date.now()
    if (error.value || now - levelShownAt < LEVEL_INTERVAL_MS) {
      return
    }
    levelShownAt = now
    progress.value = {
      ratio: -1,
      detail: `Listening on ${device}. Level ${vad.energy.toFixed(3)}, `
        + `speech above ${vad.threshold.toFixed(3)}.`,
    }
  }

  async function stop () {
    if (!active) {
      return
    }
    active = false
    listening.value = false

    await microphone?.stop()
    microphone = undefined

    // Whatever was still being spoken when the button was pressed: no boundary
    // will arrive for it, so it has to be forced out.
    const tail = vad.flush()
    splitter.reset()
    if (tail) {
      enqueue(tail)
    }
    await draining
  }

  return { listening, error, progress, start, stop }
}

function message (error_: unknown): string {
  return error_ instanceof Error ? `${error_.name}: ${error_.message}` : String(error_)
}
