/**
 * lib/parakeet.ts
 *
 * Parakeet through Transformers.js: a FastConformer encoder with a CTC head,
 * which is what the live path uses wherever the device can afford it.
 *
 * Two things here work around the library and the export rather than the model.
 * Both are described in the README; neither is optional.
 */

import {
  type Capability,
  downloadProgress,
  type Progress,
  transformers,
} from '@/lib/whisper'

export const PARAKEET = 'onnx-community/parakeet-ctc-0.6b-ONNX'

const DTYPE = 'q4' as const
const TAIL_SAMPLES = 8000
const RETRY_SAMPLES = 1600

export interface Recognizer {
  transcribe: (segment: Float32Array) => Promise<string>
  label: string
}

interface Decoded {
  text: string
  unknown: number
  frames: number
}

export async function loadParakeet (
  capable: Capability,
  onProgress: (progress: Progress) => void,
): Promise<Recognizer> {
  const { AutoModel, AutoProcessor, AutoTokenizer } = await transformers()
  const options = {
    dtype: DTYPE,
    device: capable.device,
    progress_callback: downloadProgress(onProgress),
  }

  let model, processor, tokenizer
  try {
    ;[model, processor, tokenizer] = await Promise.all([
      AutoModel.from_pretrained(PARAKEET, options),
      AutoProcessor.from_pretrained(PARAKEET),
      AutoTokenizer.from_pretrained(PARAKEET),
    ])
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new Error(`${PARAKEET} on ${capable.device} failed to load: ${detail}`, { cause: error })
  }

  const config = model.config as { pad_token_id?: number, vocab_size?: number }
  const blank = config.pad_token_id ?? ((config.vocab_size ?? 1025) - 1)

  const decode = async (audio: Float32Array): Promise<Decoded> => {
    const inputs = await processor(audio)
    const { logits } = await model(inputs) as {
      logits: { dims: number[], data: Float32Array }
    }
    const frames = logits.dims[1]!
    const vocab = logits.dims[2]!
    const ids: number[] = []
    let previous = -1
    let unknown = 0
    for (let frame = 0; frame < frames; frame++) {
      const offset = frame * vocab
      let best = 0
      let bestValue = -Infinity
      for (let index = 0; index < vocab; index++) {
        const value = logits.data[offset + index]!
        if (value > bestValue) {
          bestValue = value
          best = index
        }
      }
      if (best === 0) {
        unknown += 1
      }
      if (best !== previous && best !== blank) {
        ids.push(best)
      }
      previous = best
    }
    const text = tokenizer.decode(ids, { skip_special_tokens: true }).trim()
    return { text, unknown, frames }
  }

  return {
    label: capable.label,
    transcribe: async segment => {
      let decoded = await decode(padded(segment, TAIL_SAMPLES))
      if (decoded.unknown > decoded.frames / 2) {
        decoded = await decode(padded(segment, TAIL_SAMPLES + RETRY_SAMPLES))
      }
      return decoded.text
    },
  }
}

function padded (segment: Float32Array, tail: number): Float32Array {
  const result = new Float32Array(segment.length + tail)
  result.set(segment)
  return result
}
