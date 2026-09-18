/**
 * lib/vad.ts
 *
 * Energy-envelope Voice Activity Detection: it takes frames of audio, decides
 * which of them carry speech, and hands back one utterance at a time.
 *
 * A frame is a small fixed-size slice of samples - the unit the VAD looks at
 * one at a time rather than all at once. At 16 kHz, 20 ms is 320 samples, and
 * every call to `feed()` takes exactly that many. 20 ms is the usual choice in
 * speech processing because it matches the time scale of a phoneme: short
 * enough that speech/silence transitions are caught quickly, long enough that
 * the RMS energy is stable rather than fooled by individual waveform peaks. At
 * 10 ms the energy estimate gets noisy; at 40 ms fast transitions start to slip
 * through.
 *
 * Two things keep a plain energy test usable, both still inside the energy
 * envelope rather than reaching for a model: a threshold that rises with the
 * room's noise floor, and pre-speech padding, so a segment does not begin midway
 * through its first word. Each is commented at the point it happens.
 *
 * The remaining weakness is inherent to measuring energy: RMS cannot tell speech
 * from any other sound at the same level, so a slammed door in a quiet room
 * still reaches Whisper. Telling them apart needs a model, and that is a trade
 * this page cannot make cheaply - it would mean a second ONNX Runtime session
 * where Transformers.js already chains every session onto one promise that a
 * single rejection poisons, competing for the same cores as Whisper decode on
 * exactly the device that has none to spare.
 */

export const SAMPLE_RATE = 16_000

const FRAME_MS = 20
const MIN_SPEECH_MS = 300

/**
 * The threshold is the old fixed `0.008`, raised to a multiple of the measured
 * noise floor when the room is louder than that allows. Too low for the room and
 * every frame reads as speech, so segments only ever end at the cap and Whisper
 * is handed 15 s of noise - which it does not return empty for, it hallucinates
 * straight into the transcript, and from there into the solve prompt.
 *
 * It never drops below `MIN_THRESHOLD`, and there is one threshold rather than a
 * lower one for keeping a segment open. Both were tried: a hold level of 0.006
 * lets a remote voice picked up from the speakers - quiet, and partly removed by
 * echo cancellation - bridge the 600 ms pause between turns, so an utterance runs
 * on into the other speaker until the cap cuts it mid-word. Whisper does worst
 * on exactly that audio, and its failure there is not silence but a loop
 * ("sad, sad, sad, ...") to the end of its token budget.
 */
const MIN_THRESHOLD = 0.008
const NOISE_MULTIPLE = 3

/**
 * The floor follows quiet quickly and loud slowly, which is what keeps a
 * speaker from being measured as their own noise. A rolling minimum was tried
 * first and is wrong: with no pause inside the window the minimum climbs into
 * the voice, the threshold follows it, and the VAD goes deaf in the middle of
 * the sentence it is listening to - silently, since a transcript that stops has
 * no way of saying why.
 *
 * Falling fast means a quiet room is measured almost immediately, and every
 * gap between words drags the estimate back down. Rising slowly means a fan
 * switching on is learned over about ten seconds, while no plausible unbroken
 * utterance lasts long enough to lift the floor to its own level.
 */
const FLOOR_FALL = 0.25
const FLOOR_RISE = 0.002

// The class default was 800 ms, but the server ran with `--min-silence-ms 600`,
// which is the value that actually saw use.
const MIN_SILENCE_MS = 600
const MIN_EMIT_MS = 700
const LONG_SILENCE_MS = 2000

// A cap rather than a preference: without it, someone talking continuously
// produces no transcript at all until they pause, and the segment would run
// past the 30 s window Whisper is trained on.
const MAX_SPEECH_MS = 15_000

/**
 * Audio kept from before the threshold was crossed. Word onsets are the quiet
 * part - a plosive is silent until its burst, a fricative ramps up - so a
 * segment that starts at the crossing starts a little way into its first word,
 * and Whisper does not degrade gracefully there: it guesses a plausible word,
 * which reads as fluent text that happens to be wrong. In an exam the first
 * word is disproportionately the one that matters, since it is often the
 * question word. This costs nothing at inference time, since Whisper pads every
 * input to 30 s regardless.
 */
const PAD_MS = 240

export interface VadOptions {
  frameMs?: number
  minSilenceMs?: number
  minSpeechMs?: number
  minEmitMs?: number
  longSilenceMs?: number
  maxSpeechMs?: number
}

export class VadAccumulator {
  readonly frameSamples: number

  readonly #minSilenceFrames: number
  readonly #minSpeechFrames: number
  readonly #minEmitFrames: number
  readonly #longSilenceFrames: number
  readonly #maxSpeechSamples: number
  readonly #padFrames: number

  #buffer: Float32Array[] = []
  #buffered = 0
  #speechFrames = 0
  #silenceFrames = 0
  #inSpeech = false

  // Frames seen but not yet part of a segment, kept so that a segment can begin
  // slightly before the threshold was crossed. Cleared whenever a segment is
  // emitted: the frames of trailing silence are already inside that segment, so
  // padding the next one with them would repeat audio, and the symptom would be
  // a word appearing at the end of one transcript line and the start of the next.
  #pad: Float32Array[] = []

  #floor = 0
  #lastEnergy = 0

  constructor ({
    frameMs = FRAME_MS,
    minSilenceMs = MIN_SILENCE_MS,
    minSpeechMs = MIN_SPEECH_MS,
    minEmitMs = MIN_EMIT_MS,
    longSilenceMs = LONG_SILENCE_MS,
    maxSpeechMs = MAX_SPEECH_MS,
  }: VadOptions = {}) {
    this.frameSamples = Math.trunc(SAMPLE_RATE * frameMs / 1000)
    this.#minSilenceFrames = Math.trunc(minSilenceMs / frameMs)
    this.#minSpeechFrames = Math.trunc(minSpeechMs / frameMs)
    this.#minEmitFrames = Math.trunc(minEmitMs / frameMs)
    this.#longSilenceFrames = Math.trunc(longSilenceMs / frameMs)
    this.#maxSpeechSamples = Math.trunc(maxSpeechMs / 1000 * SAMPLE_RATE)
    this.#padFrames = Math.trunc(PAD_MS / frameMs)
  }

  /**
   * The level a frame currently has to reach, and the level of the last one.
   * Read only for display: an adaptive threshold is invisible in the transcript,
   * so without somewhere to see it, tuning it is guesswork.
   */
  get threshold (): number {
    return Math.max(MIN_THRESHOLD, this.#floor * NOISE_MULTIPLE)
  }

  get energy (): number {
    return this.#lastEnergy
  }

  /**
   * Takes one frame and returns a segment when the frame completed one:
   * either a pause long enough to be an utterance boundary, or the cap.
   */
  feed (frame: Float32Array): Float32Array | undefined {
    const energy = rms(frame)
    // Measured before the frame is classified, so the floor tracks the room
    // rather than the decision made about it.
    this.#observe(energy)

    if (energy >= this.threshold) {
      if (!this.#inSpeech) {
        // Opening a segment: everything held back comes with it, so the
        // recording starts before the first word rather than inside it.
        for (const held of this.#pad) {
          this.#append(held)
        }
        this.#pad = []
        this.#inSpeech = true
      }
      this.#append(frame)
      // Padding must not count here, or the minimum-speech gate is satisfied by
      // silence and short noises start producing segments.
      this.#speechFrames += 1
      this.#silenceFrames = 0
      if (this.#buffered >= this.#maxSpeechSamples) {
        return this.flush()
      }
    } else if (this.#inSpeech) {
      // Kept, not dropped: a pause shorter than the threshold is part of the
      // utterance, and Whisper transcribes better with it left in.
      this.#append(frame)
      this.#silenceFrames += 1
      const enough = this.#speechFrames >= this.#minEmitFrames
        ? this.#minSilenceFrames
        : this.#longSilenceFrames
      if (this.#silenceFrames >= enough) {
        return this.flush()
      }
    } else {
      this.#hold(frame)
    }
    return undefined
  }

  /** Emits whatever is buffered, if it holds enough speech to be worth it. */
  flush (): Float32Array | undefined {
    const segment = this.#speechFrames >= this.#minSpeechFrames
      ? concatenate(this.#buffer, this.#buffered)
      : undefined
    this.#buffer = []
    this.#buffered = 0
    this.#speechFrames = 0
    this.#silenceFrames = 0
    this.#inSpeech = false
    this.#pad = []
    return segment
  }

  #append (frame: Float32Array) {
    this.#buffer.push(frame)
    this.#buffered += frame.length
  }

  #hold (frame: Float32Array) {
    this.#pad.push(frame)
    if (this.#pad.length > this.#padFrames) {
      this.#pad.shift()
    }
  }

  #observe (energy: number) {
    this.#lastEnergy = energy
    const rate = energy < this.#floor ? FLOOR_FALL : FLOOR_RISE
    this.#floor += (energy - this.#floor) * rate
  }
}

function rms (frame: Float32Array): number {
  let total = 0
  for (const sample of frame) {
    total += sample * sample
  }
  return Math.sqrt(total / frame.length)
}

function concatenate (frames: Float32Array[], length: number): Float32Array {
  const segment = new Float32Array(length)
  let offset = 0
  for (const frame of frames) {
    segment.set(frame, offset)
    offset += frame.length
  }
  return segment
}

/**
 * Blocks arriving from the audio thread are 128 samples and never line up with
 * a 320-sample frame, so the remainder has to be carried between calls. This is
 * the `residue` loop.
 */
export class FrameSplitter {
  #residue = new Float32Array(0)

  readonly #frameSamples: number

  constructor (frameSamples: number) {
    this.#frameSamples = frameSamples
  }

  split (block: Float32Array, onFrame: (frame: Float32Array) => void) {
    const combined = new Float32Array(this.#residue.length + block.length)
    combined.set(this.#residue)
    combined.set(block, this.#residue.length)

    let offset = 0
    while (combined.length - offset >= this.#frameSamples) {
      onFrame(combined.subarray(offset, offset + this.#frameSamples))
      offset += this.#frameSamples
    }
    // Copied rather than kept as a view, so the combined buffer can be freed.
    this.#residue = combined.slice(offset)
  }

  reset () {
    this.#residue = new Float32Array(0)
  }
}
