/**
 * scripts/vad-check.mts
 *
 * Behaviour checks for `src/lib/vad.ts`, run with `pnpm check:vad`.
 *
 * The project has no test runner, and this is not the start of one: the VAD is
 * the one piece here that is pure, deterministic, and impossible to eyeball,
 * since its output is an audio segment and its input is a room. The first seven
 * cases cover the segmentation state machine; the rest cover the adaptive floor,
 * the quiet-voice cut-off and the padding.
 *
 * Node runs this file directly by stripping the types, so there is no build
 * step and no dependency. That needs Node 22.6 or newer, which the project
 * already requires elsewhere.
 */

import { FrameSplitter, VadAccumulator } from '../src/lib/vad.ts'

const FRAME = 320

/** A 440 Hz tone: RMS is the amplitude over root two, so 0.05 gives ~0.035. */
function speech (amplitude = 0.05): Float32Array {
  const frame = new Float32Array(FRAME)
  for (let index = 0; index < FRAME; index++) {
    frame[index] = amplitude * Math.sin(2 * Math.PI * 440 * index / 16_000)
  }
  return frame
}

function silence (): Float32Array {
  return new Float32Array(FRAME)
}

/** Steady non-speech at a known RMS, standing in for a fan or an air vent. */
function room (level: number): Float32Array {
  const frame = new Float32Array(FRAME)
  for (let index = 0; index < FRAME; index++) {
    frame[index] = index % 2 === 0 ? level : -level
  }
  return frame
}

let failures = 0

function check (name: string, passed: boolean, detail = '') {
  console.log(`${passed ? 'ok  ' : 'FAIL'} ${name}${detail ? ` (${detail})` : ''}`)
  if (!passed) {
    failures += 1
  }
}

function frames (segment: Float32Array | undefined): string {
  return segment ? `${segment.length / FRAME} frames` : 'nothing'
}

// Segmentation: opening, holding, ending, discarding, and the cap.

{
  const vad = new VadAccumulator()
  let emitted = false
  for (let index = 0; index < 200; index++) {
    if (vad.feed(silence())) {
      emitted = true
    }
  }
  check('silence only emits nothing', !emitted && vad.flush() === undefined)
}

{
  const vad = new VadAccumulator()
  let early
  for (let index = 0; index < 25; index++) {
    early ||= vad.feed(speech())
  }
  let segment
  for (let index = 0; index < 40 && !segment; index++) {
    segment = vad.feed(silence())
  }
  check(
    'speech then silence emits a segment',
    !early && !!segment && segment.length >= 25 * FRAME,
    frames(segment),
  )
}

{
  const vad = new VadAccumulator({ minSilenceMs: 200, minSpeechMs: 500 })
  for (let index = 0; index < 5; index++) {
    vad.feed(speech())
  }
  let emitted = false
  for (let index = 0; index < 50; index++) {
    if (vad.feed(silence())) {
      emitted = true
    }
  }
  check('speech under the minimum is discarded', !emitted)
}

{
  const vad = new VadAccumulator()
  for (let index = 0; index < 25; index++) {
    vad.feed(speech())
  }
  let split
  for (let index = 0; index < 10; index++) {
    split ||= vad.feed(silence())
  }
  for (let index = 0; index < 25; index++) {
    split ||= vad.feed(speech())
  }
  let segment
  for (let index = 0; index < 40 && !segment; index++) {
    segment = vad.feed(silence())
  }
  check(
    'a brief pause does not split an utterance',
    !split && !!segment && segment.length >= 60 * FRAME,
    frames(segment),
  )
}

{
  const vad = new VadAccumulator({ maxSpeechMs: 1000 })
  let segment
  for (let index = 0; index < 75 && !segment; index++) {
    segment = vad.feed(speech())
  }
  check(
    'the cap forces a segment',
    !!segment && Math.abs(segment.length - 16_000) < 2 * FRAME,
    segment && `${segment.length} samples`,
  )
}

{
  const vad = new VadAccumulator()
  for (let index = 0; index < 25; index++) {
    vad.feed(speech())
  }
  check('flush returns accumulated speech', vad.flush()?.length === 25 * FRAME)
  check('flush on empty returns nothing', new VadAccumulator().flush() === undefined)
}

// Pre-speech padding.

{
  const vad = new VadAccumulator()
  // More than the ring holds, so it is full and rolling when speech starts.
  for (let index = 0; index < 30; index++) {
    vad.feed(silence())
  }
  for (let index = 0; index < 25; index++) {
    vad.feed(speech())
  }
  const segment = vad.flush()
  check(
    'a segment starts before the first loud frame',
    segment?.length === (12 + 25) * FRAME,
    frames(segment),
  )
}

{
  const vad = new VadAccumulator()
  for (let index = 0; index < 25; index++) {
    vad.feed(speech())
  }
  let first
  for (let index = 0; index < 40 && !first; index++) {
    first = vad.feed(silence())
  }
  for (let index = 0; index < 25; index++) {
    vad.feed(speech())
  }
  // The trailing silence went out with the first segment; if the ring survived,
  // the second segment would begin by repeating it.
  const second = vad.flush()
  check('padding is cleared on emit', second?.length === 25 * FRAME, frames(second))
}

// A quiet second voice. Below the minimum threshold, a remote speaker leaking
// through the speakers must not keep an utterance open after a short turn gap:
// when it did, segments ran on to the cap and Whisper looped on the result.

{
  const vad = new VadAccumulator()
  let segment
  for (let index = 0; index < 50; index++) {
    segment ??= vad.feed(speech())
  }
  for (let index = 0; index < 10; index++) {
    segment ??= vad.feed(silence())
  }
  for (let index = 0; index < 750 && !segment; index++) {
    segment = vad.feed(speech(0.0106))
  }
  check(
    'a quiet voice after a turn gap does not extend the segment',
    !!segment && segment.length < 2 * 16_000,
    frames(segment),
  )
}

// The adaptive floor, in both directions.

{
  const vad = new VadAccumulator()
  let learnedAfter = -1
  for (let index = 0; index < 1500; index++) {
    vad.feed(room(0.02))
    if (learnedAfter < 0 && vad.threshold > 0.02) {
      learnedAfter = index / 50
    }
  }
  check(
    'a noisy room stops triggering once learned',
    learnedAfter > 0 && learnedAfter < 15,
    `ignored after ${learnedAfter.toFixed(1)} s, threshold now ${vad.threshold.toFixed(3)}`,
  )
}

{
  // Sixty seconds of speech with only the gaps between words in it. A rolling
  // minimum over a window fails here: with no pause in the window the floor
  // climbs into the voice and the VAD goes deaf mid-sentence.
  const vad = new VadAccumulator()
  let kept = 0
  let segments = 0
  for (let cycle = 0; cycle < 150; cycle++) {
    for (let index = 0; index < 15; index++) {
      const segment = vad.feed(speech())
      if (segment) {
        segments += 1
        kept += segment.length
      }
    }
    for (let index = 0; index < 5; index++) {
      const segment = vad.feed(speech(0.011))
      if (segment) {
        segments += 1
        kept += segment.length
      }
    }
  }
  const tail = vad.flush()
  if (tail) {
    segments += 1
    kept += tail.length
  }
  check(
    'unbroken speech does not go deaf',
    kept / 16_000 > 58,
    `${segments} segments, ${(kept / 16_000).toFixed(1)} s of 60 s kept`,
  )
}

{
  const splitter = new FrameSplitter(FRAME)
  const sizes: number[] = []
  // Ten worklet blocks of 128 samples is 1280, which is four frames and a
  // remainder that has to survive to the next call.
  for (let index = 0; index < 10; index++) {
    splitter.split(new Float32Array(128), frame => sizes.push(frame.length))
  }
  check(
    'blocks are regrouped into whole frames',
    sizes.length === 4 && sizes.every(size => size === FRAME),
    `${sizes.length} frames from 1280 samples`,
  )
}

console.log(failures ? `\n${failures} failed` : '\nall checks passed')
if (failures) {
  globalThis.process.exitCode = 1
}
