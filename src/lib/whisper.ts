/**
 * lib/whisper.ts
 *
 * Loading Whisper through Transformers.js, shared by both local transcription
 * paths.
 *
 * The model weights come from the Hugging Face CDN at runtime, not from this
 * site, and Transformers.js caches them in the browser after the first run.
 */

import type {
  AutomaticSpeechRecognitionPipeline,
  pipeline as Pipeline,
  env as TransformersEnvValue,
} from '@huggingface/transformers'

export interface Progress {
  // -1 when the work is indeterminate.
  ratio: number
  detail: string
}

export interface WhisperChoice {
  webgpu: string
  threaded: string
  wasm: string
}

const DTYPE = { encoder_model: 'fp32', decoder_model_merged: 'q4' } as const

export interface Transcriber {
  pipeline: AutomaticSpeechRecognitionPipeline
  webgpu: boolean
  threads: number
}

type TransformersEnv = typeof TransformersEnvValue

/**
 * `navigator.gpu` is a browser capability, not an ONNX Runtime one, and the gap
 * between the two is a trap. The WebGPU execution provider is compiled only
 * into the `asyncify` and `jspi` runtime builds, and Transformers.js
 * deliberately points Safari at the plain build to dodge an Asyncify memory
 * leak on Apple devices - so on iPad Safari `navigator.gpu` exists but session
 * creation throws `webgpuInit is not a function`.
 *
 * Asking the runtime which build it loaded, rather than sniffing the user
 * agent, means this re-enables itself if upstream ever lifts the carve-out.
 */
function webgpuUsable (env: TransformersEnv): boolean {
  if (!('gpu' in navigator)) {
    return false
  }
  const paths = env.backends?.onnx?.wasm?.wasmPaths
  if (typeof paths !== 'object' || typeof paths.mjs !== 'string') {
    return true
  }
  return paths.mjs.includes('asyncify') || paths.mjs.includes('jspi')
}

export function wasmThreads (): number {
  if (!globalThis.crossOriginIsolated || typeof SharedArrayBuffer === 'undefined') {
    return 1
  }
  return Math.min(4, Math.max(1, Math.floor((navigator.hardwareConcurrency ?? 2) / 2)))
}

/**
 * Loads the pipeline for whichever device this browser can actually run.
 *
 * The device has to be decided before the first session, not caught around it.
 * Transformers.js chains every session creation onto one module-level promise
 * with no `.catch`, so one rejection poisons that promise for the lifetime of
 * the page: `rejected.then(load)` never runs `load`, and every later attempt
 * re-throws the *first* error. A try/catch that retries on another device would
 * therefore report the original WebGPU failure and look like the fallback
 * silently never ran. Do not replace the up-front check with a retry.
 */
export async function loadTranscriber (
  models: WhisperChoice,
  onProgress: (progress: Progress) => void,
): Promise<Transcriber> {
  const { env, pipeline } = await transformers()
  return build(pipeline, env, models, onProgress)
}

// Dynamic so the ~500 kB library chunk and the 22 MB ONNX Runtime WASM stay out
// of the initial load. Keep it that way.
export async function transformers () {
  return import('@huggingface/transformers')
}

export interface Capability {
  webgpu: boolean
  threads: number
  device: 'webgpu' | 'wasm'
  label: string
}

export function capability (env: TransformersEnv): Capability {
  const webgpu = webgpuUsable(env)
  const threads = webgpu ? 1 : wasmThreads()
  if (!webgpu && env.backends?.onnx?.wasm) {
    env.backends.onnx.wasm.numThreads = threads
    env.backends.onnx.wasm.proxy = true
  }
  return {
    webgpu,
    threads,
    device: webgpu ? 'webgpu' : 'wasm',
    label: webgpu ? 'GPU' : `CPU${threads > 1 ? ` x${threads}` : ''}`,
  }
}

export function downloadProgress (onProgress: (progress: Progress) => void) {
  return (event: { status: string, progress?: number, file?: string }) => {
    if (event.status === 'progress' && event.progress !== undefined) {
      onProgress({
        ratio: event.progress / 100,
        detail: `Downloading model: ${event.file ?? ''}`,
      })
    }
  }
}

async function build (
  pipeline: typeof Pipeline,
  env: TransformersEnv,
  models: WhisperChoice,
  onProgress: (progress: Progress) => void,
): Promise<Transcriber> {
  const { webgpu, threads, device } = capability(env)
  const options = {
    device,
    dtype: DTYPE,
    progress_callback: downloadProgress(onProgress),
  }

  const model = webgpu
    ? models.webgpu
    : (threads > 1 ? models.threaded : models.wasm)
  let loaded
  try {
    loaded = await pipeline('automatic-speech-recognition', model, options)
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new Error(`${model} on ${options.device} failed to load: ${detail}`, { cause: error })
  }
  return { pipeline: loaded as AutomaticSpeechRecognitionPipeline, webgpu, threads }
}

export function transcriptionText (output: unknown): string {
  const result = Array.isArray(output) ? output[0] : output
  return withoutLoops(String((result as { text?: string }).text ?? '').trim())
}

const LOOP = /(?:^| )((?:\S+ ){1,8}?)\1{3,}/

export function withoutLoops (text: string): string {
  const words = text.split(/\s+/).filter(Boolean)
  const flat = words.map(word => word.toLowerCase().replaceAll(/[^\p{L}\p{N}']/gu, '')).join(' ') + ' '
  const match = LOOP.exec(flat)
  if (!match) {
    return text
  }
  const wordsBefore = (index: number) => flat.slice(0, index).split(' ').length - 1
  const runStart = wordsBefore(match.index + match[0].length - match[0].trimStart().length)
  const unit = match[1]!.split(' ').length - 1
  return withoutLoops([
    ...words.slice(0, runStart + unit),
    ...words.slice(wordsBefore(match.index + match[0].length)),
  ].join(' '))
}
