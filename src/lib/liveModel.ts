/**
 * lib/liveModel.ts
 *
 * Which model the live path runs, and the one contract it exposes: hand it an
 * utterance, get a line back. The choice is per capability tier, and the
 * reasoning behind the table is in the README.
 */

import { loadParakeet, type Recognizer } from '@/lib/parakeet'
import { SAMPLE_RATE } from '@/lib/vad'
import {
  capability,
  loadTranscriber,
  type Progress,
  transcriptionText,
  transformers,
  type WhisperChoice,
} from '@/lib/whisper'

const FALLBACK: WhisperChoice = {
  webgpu: 'onnx-community/whisper-tiny.en',
  threaded: 'onnx-community/whisper-tiny.en',
  wasm: 'onnx-community/whisper-tiny.en',
}

const TOKENS_PER_SECOND = 8

export type { Recognizer } from '@/lib/parakeet'

export async function loadRecognizer (
  onProgress: (progress: Progress) => void,
): Promise<Recognizer> {
  const { env } = await transformers()
  const capable = capability(env)

  if (capable.webgpu || capable.threads > 1) {
    return loadParakeet(capable, onProgress)
  }

  const { pipeline } = await loadTranscriber(FALLBACK, onProgress)
  return {
    label: capable.label,
    transcribe: async segment => transcriptionText(
      await pipeline(segment, {
        return_timestamps: false,
        max_new_tokens: tokenBudget(segment),
      }),
    ),
  }
}

function tokenBudget (segment: Float32Array): number {
  return Math.ceil(segment.length / SAMPLE_RATE * TOKENS_PER_SECOND) + 8
}
