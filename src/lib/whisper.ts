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

/**
 * One model per device type, because the two devices are not in the same
 * performance class. The WASM path is single-threaded (nothing here sets the
 * cross-origin isolation headers that would unlock threads), so it gets the
 * smaller model of each pair.
 *
 * The encoder is where a Whisper model's accuracy lives, so it stays `fp32`.
 * The decoder is `q4` on both devices - which is what Hugging Face's own WebGPU
 * Whisper demos ship, and, less obviously, the only quantisation the current
 * runtime will load at all. See the note on `q8` in the README.
 */
export interface WhisperChoice {
  webgpu: string
  wasm: string
}

const DTYPE = { encoder_model: 'fp32', decoder_model_merged: 'q4' } as const

export interface Transcriber {
  pipeline: AutomaticSpeechRecognitionPipeline
  webgpu: boolean
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
  // Dynamic so the ~500 kB library chunk and the 22 MB ONNX Runtime WASM stay
  // out of the initial load. Keep it that way.
  const { env, pipeline } = await import('@huggingface/transformers')
  return build(pipeline, env, models, onProgress)
}

async function build (
  pipeline: typeof Pipeline,
  env: TransformersEnv,
  models: WhisperChoice,
  onProgress: (progress: Progress) => void,
): Promise<Transcriber> {
  const webgpu = webgpuUsable(env)
  const options = {
    device: webgpu ? 'webgpu' as const : 'wasm' as const,
    dtype: DTYPE,
    progress_callback: (event: { status: string, progress?: number, file?: string }) => {
      if (event.status === 'progress' && event.progress !== undefined) {
        onProgress({
          ratio: event.progress / 100,
          detail: `Downloading model: ${event.file ?? ''}`,
        })
      }
    },
  }

  const model = webgpu ? models.webgpu : models.wasm
  let loaded
  try {
    loaded = await pipeline('automatic-speech-recognition', model, options)
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new Error(`${model} on ${options.device} failed to load: ${detail}`, { cause: error })
  }
  return { pipeline: loaded as AutomaticSpeechRecognitionPipeline, webgpu }
}

export function transcriptionText (output: unknown): string {
  const result = Array.isArray(output) ? output[0] : output
  return withoutLoops(String((result as { text?: string }).text ?? '').trim())
}

/**
 * A phrase of up to four words, repeated four or more times running. Compared on
 * words with punctuation and case stripped, since the loop rarely repeats its
 * commas exactly.
 */
const LOOP = /(?:^| )((?:\S+ ){1,4}?)\1{3,}/

/**
 * When Whisper cannot make out the audio it does not return nothing, it loops
 * ("sad, sad, sad, ...") until the token budget runs out. Greedy decoding has no
 * way out of that, and Transformers.js implements none of the fallbacks
 * (temperature, compression ratio) the reference implementation uses to catch
 * it. The run is collapsed to a single copy, so a genuine "yeah, yeah, yeah,
 * yeah" costs a few words rather than the rest of the line.
 */
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
