/**
 * composables/useUniversalRecognition.ts
 *
 * Live transcription that runs entirely in the page: the microphone is captured
 * with `getUserMedia`, split into utterances by the energy VAD, and each
 * utterance is transcribed by Whisper through Transformers.js.
 *
 * This exists because the Web Speech path in `useRecognition.ts` needs a speech
 * service the browser may not be able to reach - plain Chromium ships without
 * the keys for it, and Firefox has no Web Speech API at all. Nothing here
 * leaves the device, so the only thing it needs is a microphone.
 *
 * What it costs: the model is downloaded on first use (cached afterwards), and
 * a line appears when its utterance ends rather than while it is being spoken,
 * so this path is later than Web Speech by roughly one pause plus one decode.
 */

import { ref, shallowRef } from 'vue'
import { openMicrophone } from '@/lib/micStream'
import { FrameSplitter, SAMPLE_RATE, VadAccumulator } from '@/lib/vad'
import {
  loadTranscriber,
  type Progress,
  type Transcriber,
  transcriptionText,
  type WhisperChoice,
} from '@/lib/whisper'

/**
 * Smaller than the file path's pair, and for a different reason on each device:
 * segments arrive continuously here, so decoding has to keep ahead of speech in
 * real time rather than merely finish.
 *
 * `base.en` on the single-threaded WASM fallback was tried on an iPad and did
 * not work, so that device keeps `tiny.en`. The accuracy is worse; the
 * alternative on that hardware is no live transcript.
 */
const MODELS: WhisperChoice = {
  webgpu: 'onnx-community/whisper-base.en',
  wasm: 'onnx-community/whisper-tiny.en',
}

/**
 * Segments waiting to be decoded. Reaching this means transcription is running
 * slower than speech, and the backlog would grow without bound - better to lose
 * the oldest utterance than to fall further behind on every one after it.
 */
const MAX_PENDING = 4

// How often the level readout in the status line is refreshed. Slow enough to
// be readable, fast enough to show that the microphone is live.
const LEVEL_INTERVAL_MS = 500

export function useUniversalRecognition (onLine: (text: string) => void) {
  const listening = ref(false)
  const error = ref('')
  const progress = ref<Progress>({ ratio: -1, detail: '' })

  const transcriber = shallowRef<Transcriber>()
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
        // No `chunk_length_s`: the VAD already capped the segment below the
        // 30 s window, so the chunking machinery would add cost and nothing else.
        const text = transcriptionText(
          await instance.pipeline(segment, {
            return_timestamps: false,
            max_new_tokens: tokenBudget(segment),
          }),
        )
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
      transcriber.value ??= await loadTranscriber(MODELS, update => {
        progress.value = update
      })
      if (!active) {
        return
      }

      progress.value = {
        ratio: -1,
        detail: `Listening on ${transcriber.value.webgpu ? 'GPU' : 'CPU'}.`,
      }
      const device = transcriber.value.webgpu ? 'GPU' : 'CPU'
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

/**
 * Fast speech is about four words, or six tokens, a second; this leaves room
 * above that. Left to the model's `max_length` of 448, a decoding loop on a
 * short segment runs as long as a full 30 s window would - on the CPU path that
 * is seconds of decode, during which the queue backs up past `MAX_PENDING` and
 * real utterances are dropped.
 */
const TOKENS_PER_SECOND = 8

function tokenBudget (segment: Float32Array): number {
  return Math.ceil(segment.length / SAMPLE_RATE * TOKENS_PER_SECOND) + 8
}

function message (error_: unknown): string {
  return error_ instanceof Error ? error_.message : String(error_)
}
