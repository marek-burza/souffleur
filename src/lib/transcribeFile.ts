/**
 * lib/transcribeFile.ts
 *
 * Transcription of an uploaded recording, using Whisper through
 * Transformers.js. Offline and one-shot: the whole file is decoded, then handed
 * to the pipeline in one call, which is what lets this path afford the larger
 * of the two models.
 */

import { decodeToMono16k } from '@/lib/audio'
import { loadTranscriber, type Progress, type WhisperChoice } from '@/lib/whisper'

const MODELS: WhisperChoice = {
  webgpu: 'onnx-community/whisper-small.en',
  threaded: 'onnx-community/whisper-small.en',
  wasm: 'onnx-community/whisper-base.en',
}

// Plain Whisper is trained on a 30 s window, and the
// stride is the usual chunk/6 of overlap on each side.
const CHUNK_LENGTH_S = 30
const STRIDE_LENGTH_S = 5

export type { Progress } from '@/lib/whisper'

interface Chunk { text?: string }

export function isFileTranscriptionSupported (): boolean {
  return typeof AudioContext !== 'undefined'
}

export async function transcribeFile (
  file: File,
  onProgress: (progress: Progress) => void,
): Promise<string[]> {
  onProgress({ ratio: -1, detail: 'Decoding audio...' })
  const audio = await decodeToMono16k(file)

  const { pipeline, webgpu } = await loadTranscriber(MODELS, onProgress)

  const minutes = Math.max(1, Math.round(audio.length / 16_000 / 60))
  onProgress({
    ratio: -1,
    detail: `Transcribing ${minutes} min on ${webgpu ? 'GPU' : 'CPU'}...`,
  })

  const output = await pipeline(audio, {
    chunk_length_s: CHUNK_LENGTH_S,
    stride_length_s: STRIDE_LENGTH_S,
    return_timestamps: true,
  })

  const result = Array.isArray(output) ? output[0] : output
  // Timestamped chunks give one line per utterance, matching the granularity
  // the live path produces; the flat text is the fallback if they are absent.
  const chunks = (result as { chunks?: Chunk[] }).chunks
  const lines = chunks?.length
    ? chunks.map(chunk => (chunk.text ?? '').trim())
    : String((result as { text?: string }).text ?? '').split('\n')

  return lines.filter(line => line.length > 0)
}
