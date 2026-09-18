/**
 * lib/micStream.ts
 *
 * Microphone capture through an AudioWorklet.
 *
 * Everything that wants samples - to run a VAD over, or to feed Whisper - opens
 * the microphone here and gets mono float blocks at the rate Whisper wants.
 */

import { SAMPLE_RATE } from '@/lib/vad'

/**
 * The worklet ships as a string rather than a module file on purpose: an
 * AudioWorklet module is fetched by URL, and a blob URL sidesteps the
 * base path that a bundled asset would have to resolve correctly on Pages.
 */
const WORKLET_CODE = `
class AudioProcessor extends AudioWorkletProcessor {
  process (inputs) {
    const samples = inputs[0]?.[0]
    if (samples) {
      const block = new Float32Array(samples)
      this.port.postMessage(block, [block.buffer])
    }
    return true
  }
}
registerProcessor('audio-processor', AudioProcessor)
`

export interface MicStream {
  // What the hardware actually gave us, which need not be the rate asked for.
  sampleRate: number
  stop: () => Promise<void>
}

export function microphoneUnavailable (): string {
  if (!navigator.mediaDevices?.getUserMedia) {
    return globalThis.isSecureContext
      ? 'This browser exposes no microphone, so recording will not run.'
      : 'Microphone capture requires HTTPS.'
  }
  if (typeof AudioContext === 'undefined') {
    return 'This browser has no Web Audio API, so recording will not run.'
  }
  return ''
}

/**
 * Opens the microphone and calls `onBlock` with mono float samples.
 *
 * Resampling happens in the AudioContext rather than after it - the same reason
 * `lib/audio.ts` decodes at 16 kHz - so the blocks arrive at the rate Whisper
 * wants and nothing downstream has to resample.
 */
export async function openMicrophone (
  onBlock: (block: Float32Array) => void,
): Promise<MicStream> {
  const stream = await step('getUserMedia', () => capture())

  const context = new AudioContext({ sampleRate: SAMPLE_RATE })
  try {
    // Autoplay policy suspends a fresh context; the click that got us here is
    // the gesture that lifts it.
    await step('AudioContext.resume', () => context.resume())

    const url = URL.createObjectURL(new Blob([WORKLET_CODE], { type: 'application/javascript' }))
    try {
      await step('audioWorklet.addModule', () => context.audioWorklet.addModule(url))
    } finally {
      URL.revokeObjectURL(url)
    }

    const source = context.createMediaStreamSource(stream)
    const worklet = await step(
      'new AudioWorkletNode',
      () => new AudioWorkletNode(context, 'audio-processor', {
        channelCount: 1,
        channelCountMode: 'explicit',
        channelInterpretation: 'speakers',
      }),
    )
    // `addEventListener` rather than an assigned `onmessage`, which means the
    // port has to be started explicitly - assigning the handler would have done
    // it implicitly.
    worklet.port.addEventListener('message', event => onBlock(event.data as Float32Array))
    worklet.port.start()
    source.connect(worklet)

    // A worklet with nothing downstream is never pulled, so the graph has to
    // reach the destination - silently, at zero gain.
    const mute = context.createGain()
    mute.gain.value = 0
    worklet.connect(mute).connect(context.destination)

    return {
      sampleRate: context.sampleRate,
      stop: async () => {
        worklet.port.close()
        source.disconnect()
        worklet.disconnect()
        mute.disconnect()
        for (const track of stream.getTracks()) {
          track.stop()
        }
        await context.close()
      },
    }
  } catch (error) {
    for (const track of stream.getTracks()) {
      track.stop()
    }
    await context.close()
    throw error
  }
}

async function capture (): Promise<MediaStream> {
  try {
    return await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        sampleRate: SAMPLE_RATE,
        echoCancellation: true,
        noiseSuppression: true,
      },
    })
  } catch (error) {
    if (error instanceof Error && error.name === 'NotAllowedError') {
      throw error
    }
    return navigator.mediaDevices.getUserMedia({ audio: true })
  }
}

async function step<T> (label: string, work: () => Promise<T> | T): Promise<T> {
  try {
    return await work()
  } catch (error) {
    const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error)
    throw new Error(`${label} failed: ${detail}`, { cause: error })
  }
}
