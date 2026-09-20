# Souffleur

Proof-of-concept for use when practicing interviews or exams - [try it out](https://marek-burza.github.io/souffleur/) (deployed as a static site on GitHub Pages)!

An Anthropic or OpenAI API key is entered in a dialog and kept in `LocalStorage`.

## 🏛️ Architecture

`src/App.vue` is the only stateful component. It owns the composables and passes
plain values down; the child components hold no session state.

```text
useRecognition(addLine) ──> useTranscript ──> TranscriptPane (editable)
useCamera ──> CameraPreview + capture() ──┐
prompt picker ──> lib/prompt ─────────────┼─> lib/solver solve() ──> AnswerPane
SettingsDialog (key, model, file upload) ─┘
```

Two transcription paths, one `addLine` contract: each emits a line per utterance
and neither knows about the other. `App.vue` stops a running live session before
starting anything else, since both paths want the same microphone.

### 📝 Prompts

`src/lib/prompt.ts` holds the solve prompts and nothing else; `solve()` takes one as a
string and knows nothing about which. The picker sits between `Capture` and `Solve`
because that is the order the two are used in, and the choice is stored under
`souffleur.prompt` so it survives the reload the isolation service worker performs.
`saveSettings()` therefore merges rather than replaces: the toolbar writes the prompt
without holding the key, and the dialog writes the key without holding the prompt.

There are three, and they differ in kind rather than in wording.

**Generic** is the original: find the most recent question, answer it, `QUESTION` then
`TL;DR` then bullets. It assumes the answer is the deliverable.

**ML/AI architecture** is for a Google ML system design round, where the answer is not the
deliverable - sixty minutes of talking is. That distinction is the whole design:

- **It is a glance sheet, not an essay.** He reads it while speaking, so there is a hard cap
  of 45 lines and 20 words per line. Breadth is assumed and coverage he cannot deliver in an
  hour scores nothing, so the instruction is to spend lines where the problem is hard and
  drop the rest.
- **The clarifying questions are an exchange, not a monologue.** They are scored, but only if
  actually asked. An earlier version put `-> assume:` on every one, which invited him to read
  the question and his own answer to it in the same breath and never let the interviewer
  steer. Now the questions stand alone and a single `if deflected, assume:` line carries the
  fallbacks together.
- **Numbers are derived, never asserted.** `50 QPS x 100 candidates = 5k reranker calls/s`,
  not `5k reranker calls/s`. The calculation is what is scored, and a number he cannot
  rebuild is a trap the moment he is probed. At least three lines must show their arithmetic:
  the load, whatever dominates cost or latency, and the money.
- **Every acronym is expanded at first use**, model and metric names included. He learned this
  working alongside data scientists, so a bare `GBDT` or `NDCG` is worse than the plain
  phrase - he will be asked to expand it.
- **A question that names its own deliverables overrides the skeleton.** Many do: *architecture,
  trade-offs for ingestion and storage, safety mechanisms, a multi-cloud plan*. Those become
  the headings, and the skeleton drops to a checklist of what to cover inside them. Without
  this the template crowded out whatever it had no heading for - storage and multi-cloud got
  zero lines and one line respectively on a question that named both.
- **Every ML choice carries its reason.** Systems, serving, scale and cost are named and left
  alone, but a model family, a loss, a sampling scheme, a metric or a training trick arrives
  with its because-clause in under ten words.
- **A probe is not a question.** Once the answer is under way the interviewer stops opening new
  problems and starts pushing on one box in the diagram, so the prompt detects a transcript
  ending on a follow-up and answers only that - one heading, eight to fifteen lines.

Measured over thirteen questions scraped from five companies' ML-system-design sets, against
the previous version of the prompt:

| | before | after |
| --- | --- | --- |
| words per answer | 723 | 582 |
| lines per answer | 66 | 50 |
| bare acronyms per answer | 5.7 | 1.3 |
| answers with no worked arithmetic | 12/13 | 1/13 |
| answers carrying more than three bare acronyms | 9/13 | 1/13 |

`AnswerPane` is a `<pre>` at 9pt, so the prompt bans markdown outright - asterisks and fences
render literally and cost lines.

Both Sonnet 5 and Opus 5 hold the format; Opus runs about 30% longer, in more lines rather
than longer ones. Haiku 4.5 keeps the structure but breaks the line cap and drops the
`(assume)` markers, which is why it is labelled weakest in the picker.

**System design** is the other interview in the loop: the classic distributed-systems round,
not the ML one. It shares the glance-sheet rules above - the line cap, derived arithmetic,
acronyms expanded, the deliverables override, the follow-up branch - and then adds what this
round scores differently.

The one that reshapes the whole answer is that **Google penalises naming the product instead
of explaining the mechanism**, harder than any other panel. "A log with per-partition offsets
so consumers replay independently" scores; "use Kafka" invites a question he cannot answer.
The prompt states that rule before anything else and re-checks it at the end, and across
twelve scraped questions it produced zero unaccompanied product names.

The rest follows the round's rubric: drive the scope rather than wait for it, commit instead
of saying "it depends", go three or four layers deep on two components rather than one layer
on eight, and cover what breaks at ten times the load. `DEEP DIVE` therefore gets more lines
than any other section, and `HARD PART` at the top names which two components it should be.

Three things were measured across those twelve questions while tuning it, and two of them
fought each other. Per-section line budgets tightened the sheet (46 lines to 44, and long
lines from 3.2 per answer to 1.9) but squeezed out the acronym glosses, taking the worst
answer from 4 bare acronyms to 10; exempting glosses from the budget did not fix it and cost
arithmetic as well. The budgets were dropped, because an unexpandable acronym is the defect
he actually gets caught by. What shipped averages 4.1 worked calculations and 1.8 bare
acronyms per answer, with every answer covering failure modes, scaling limits and trade-offs.

### 🔑 Providers

`src/lib/solver.ts` talks to both providers through LangChain, and the key picks
which one: `sk-ant-` means `ChatAnthropic`, anything else `ChatOpenAI`. There is
deliberately no provider toggle, since a toggle can disagree with the key. The
model dropdown follows the same inference, and `resolveModel()` drops a stored
model that belongs to the other provider (so replacing the key cannot leave a
model name the new provider would reject).

Calling either API from a page means acknowledging it, and each SDK spells that
differently: `clientOptions: { dangerouslyAllowBrowser: true }` for Anthropic,
`configuration: { dangerouslyAllowBrowser: true }` for OpenAI. LangChain forwards
both verbatim to the underlying client constructor. Nothing about this is a
proxy - the build stays a static site.

Both effort controls are native LangChain constructor fields, not passthrough
kwargs: `thinking: { type: 'adaptive' }` with `outputConfig: { effort }` on
`ChatAnthropic`, and `reasoning: { effort }` on `ChatOpenAI` (the flat
`reasoningEffort` is deprecated in favour of it). They reach the wire as
`thinking` + `output_config` and as `reasoning_effort` respectively. Note that
`reasoning.effort` alone does **not** move OpenAI onto the Responses API:
`_useResponsesApi()` switches only on `reasoning.summary`, built-in or custom
tools, or a model name matching its `-pro`/`codex` list. `gpt-5.6-*` therefore
goes to `/v1/chat/completions`, which is fine - `maxTokens` becomes
`max_completion_tokens` there.

### 🛣️ Transcription Paths

**Live** (the `Record` button, `src/composables/useRecognition.ts`) needs nothing
but a microphone: it captures the mic itself, segments it with the VAD, and
transcribes on the device, so it runs in any browser `getUserMedia` does. What
that costs is a model download on first use and a line that appears when its
utterance ends rather than while it is being spoken.

- `src/lib/micStream.ts` is the `getUserMedia` + `AudioWorklet` capture, with the
  worklet posting blocks of mono float samples to a callback. Two details are
  load-bearing: the worklet is published as a **blob URL** rather than a bundled
  asset, which sidesteps `base: '/souffleur/'` entirely, and the worklet is
  connected through a zero-gain node to `destination`, because a graph that reaches
  no destination is never pulled. The `AudioContext` is fixed at 16 kHz so
  resampling happens in the graph and nothing downstream has to do it.
- `src/lib/vad.ts` is the energy VAD: 20 ms frames, 300 ms minimum speech, 600 ms
  of silence to end a segment, and a 15 s cap. The cap is not a preference:
  without it someone talking continuously produces no transcript until they pause,
  and the segment would outrun Whisper's 30 s window. `FrameSplitter` regroups
  what arrives into whole frames - worklet blocks are 128 samples and never align
  with a 320-sample frame.
- **A single fixed energy threshold is not enough.** Two additions, both still
  inside the energy envelope:
  - *Adaptive floor.* The threshold is `0.008` or 3x a measured noise floor,
    whichever is higher: too low for the room and every frame reads as speech, so
    Whisper gets 15 s of room noise and hallucinates ("Thank you.", "Subtitles by
    ...") into the transcript and from there into the solve prompt.
  - *No hysteresis, and no lower minimum.* Both were tried. A 2x hold level with
    the floor clamped at 0.003 let a quiet remote voice from the speakers bridge
    the pause between turns, so segments ran to the 15 s cap, were cut mid-word,
    and Whisper looped on them ("sad, sad, sad, ...").
  - *Pre-speech padding.* 240 ms held in a ring and prepended on speech start.
    Word onsets are quiet, and a segment that begins at the crossing begins inside
    its first word; Whisper's failure there is to guess a plausible word, which
    reads as fluent text that is wrong. Free at inference time, since Whisper pads
    to 30 s regardless. The ring **must** be cleared on emit - trailing silence is
    already inside the emitted segment, so a surviving ring repeats audio, and the
    symptom is a word appearing at the end of one line and the start of the next.
- **The floor tracks quiet fast and loud slowly, and that asymmetry is the whole
  point.** A rolling minimum was tried first and is wrong: with no pause inside the
  window the minimum climbs into the speaker's own voice and the VAD goes deaf
  mid-sentence, silently. Falling at `FLOOR_FALL` measures a room almost at once
  and lets every gap between words drag the estimate back down; rising at
  `FLOOR_RISE` learns a fan in about five seconds while no plausible unbroken
  utterance lifts the floor to its own level.
- **A boundary needs something to end.** A pause only closes a segment once it
  holds 700 ms of speech; below that the segment stays open until a 2 s pause, so
  a fragment joins the utterance next to it instead of being decoded alone.
  Whisper does not decline a one-word segment, it returns a plausible word, and a
  plausible wrong word reads as transcript rather than as an error. Measured over
  0, 400, 500, 600, 700, 800, 1000 and 1200 ms on the first ten minutes with
  base.en: 19.8, 19.8, 19.3, 19.3, 19.2, 19.2, 19.4, 23.5. On the full 28 minutes
  with tiny.en it is 34.9% without and 33.5% with.

  Both ends of that sweep matter. There is no gain below 500 ms because nothing
  merges, and 1.2 s is a collapse rather than a slide: the quiet second speaker
  rarely clears it, so their turns merge into one long segment, and 13.6 s of
  quiet speech comes back as the single word "Well,". The cost of the 700 ms that
  does work is that a genuinely short answer lands one pause later.
- **Cutting the cap at a quiet point was tried and is worse.** Continuous speech
  reaches the 15 s cap far more often than it reaches a 600 ms pause - this VAD
  calls 97% of that recording speech and hits the cap 27 times - and the cut
  lands wherever the buffer happened to fill, which is usually inside a word. So
  the cap was made to cut at the quietest 20 ms frame within the preceding 3 s,
  and then at the middle of the longest sub-threshold run there, with the
  remainder held over to open the next segment. Both scored worse than cutting
  where the cap lands: 22.5% and 22.5% against 19.8%, base.en, first ten minutes.
  The reason is phonetic. The quietest moment inside continuous speech is usually
  a stop closure, which is *inside* a word rather than between two, and Whisper
  recovers from an arbitrary cut better than from a segment that begins halfway
  through a plosive.
- **Whisper loops rather than going quiet** on audio it cannot make out, and
  Transformers.js has none of the reference implementation's fallbacks for it.
  Two guards: `max_new_tokens` scales with the segment's length instead of
  defaulting to 448, and `withoutLoops()` in `src/lib/whisper.ts` collapses a
  phrase repeated four or more times running to one copy.
- Segments are decoded **one at a time** - the pipeline is not reentrant - and the
  queue drops its oldest entry past `MAX_PENDING`, so a device that cannot keep up
  loses an utterance instead of drifting further behind on every one after it. The
  segment is handed over with no `chunk_length_s`: the VAD already capped it below
  the 30 s window, so chunking would only add cost.

**File upload** (`src/lib/transcribeFile.ts`) transcribes a recording made
elsewhere. It runs the whole file through Whisper in one call, which is what lets
it afford the larger model of each tier.

Both paths load through `src/lib/whisper.ts`, which owns the dynamic import and
the capability check; `src/lib/liveModel.ts` picks the live model and
`src/lib/parakeet.ts` runs it. Notes that matter:

- The `@huggingface/transformers` import is **dynamic** so the ~500 kB chunk and the
  22 MB ONNX Runtime WASM stay out of the initial load. Keep it that way.
- **One model per capability tier, per path.** The tier is not the browser, it is
  what the runtime can actually use: WebGPU, WASM with threads, or WASM on one
  thread. `src/lib/liveModel.ts` makes that choice for the live path and
  `transcribeFile.ts` for the file path:

  |                  | live                   | file upload |
  | ---------------- | ---------------------- | ----------- |
  | WebGPU           | `parakeet-ctc-0.6b` q4 | small.en    |
  | WASM, threaded   | `parakeet-ctc-0.6b` q4 | small.en    |
  | WASM, one thread | tiny.en                | base.en     |

  Every number below comes from replaying one 28-minute mock interview - hard
  audio: two speakers, one of them quiet and remote, continuous speech with few
  pauses - through this project's own VAD, decoding the segments one at a time as
  the live path does, and scoring against a `whisper-large-v3-turbo` transcript of
  the same recording. Word error rate, with case and punctuation stripped on both
  sides, 3301 reference words:

  | live configuration                                | WER   |
  | ------------------------------------------------- | ----- |
  | tiny.en, before the boundary rule below (shipped) | 34.9% |
  | tiny.en                                           | 33.5% |
  | base.en                                           | 27.4% |
  | small.en                                          | 25.8% |
  | `parakeet-ctc-0.6b` q4                            | 22.8% |
  | file upload path, small.en in 30 s chunks         | 18.6% |

  The live column has to keep ahead of speech rather than merely finish, so the
  speeds were measured the same way, in a browser, over the same segments (Chrome,
  WASM, on a Ryzen 9 7950X - divide by roughly 1.5-2 for an iPad Air M1):

  | model                  | 1 thread | 4 threads |
  | ---------------------- | -------- | --------- |
  | tiny.en                | 4.0x     | 9.6x      |
  | base.en                | 2.0x     | 5.1-6.0x  |
  | small.en               | -        | 0.8x      |
  | `parakeet-ctc-0.6b` q4 | 1.7x     | 7.2x      |

  One thread is where base.en was tried on an iPad and did not work, and the table
  says why: 2.0x on a desktop core is about 1x on that device, which is not a
  margin. It is also why the one-thread tier keeps tiny.en rather than the better
  model - Parakeet is 1.7x there, no better off than base.en - and why small.en
  never reaches the live path at all.
- **The live model is not Whisper.** `parakeet-ctc-0.6b`
  (`onnx-community/parakeet-ctc-0.6b-ONNX`, NVIDIA's FastConformer with a CTC
  head, run through Transformers.js like everything else here) is 4.6 WER points
  better than base.en on the same tier and faster than it, because its encoder
  takes the segment at its real length instead of padding everything to 30 s, and
  because a CTC head is one pass with no autoregressive decode - so it cannot loop
  the way Whisper does on audio it cannot make out. `src/lib/parakeet.ts` is the
  whole of it, and two of its lines are not decoration:
  - **The CTC collapse is ours.** Transformers.js routes `parakeet_ctc` through
    its wav2vec2 path, which takes the per-frame argmax and hands it straight to
    the tokenizer **without collapsing repeated tokens or dropping the blank**.
    That works for wav2vec2, whose tokenizer groups characters itself, and fails
    for a SentencePiece vocabulary: it produced "datatababase",
    "environonmental", "need needed", and cost some fifteen WER points. So the
    argmax, the collapse and the blank (`pad_token_id`, 1024) are done here.
  - **An empty decode is silence, not an error.** A segment in which every frame
    comes back blank - a door, a keyboard, a breath over the VAD threshold -
    yields no token ids at all, and `tokenizer.decode([])` throws
    `token_ids must be non-empty array of integers`. That is a line with nothing
    in it, so it is returned as one.
  - **Every segment gets 0.5 s of silence, and a retry.** The export degenerates
    to all-`<unk>` on particular inputs - 16 of 78 segments - regardless of
    content, length or gain, and appending silence cures it. Padding every
    segment removes most of them; the retry with a little more padding removes
    the rest. Without this the affected lines come back empty.

  What it costs: a 643 MB download against 110 MB for base.en (`q4` is the only
  quantisation worth having: on the first ten minutes it scored 17.2% against
  23.4% for `int8` and 18.4% for `fp32`, and `fp32` is a 2.4 GB download), and no punctuation or capitalisation, which the WER above does not see
  because the scoring strips both, but a reader of the transcript does. Reverting
  is a two-line edit in `liveModel.ts`: hand `loadTranscriber` the Whisper table
  for every tier.
- **Parakeet does not replace Whisper on the file path.** It is tempting, since it
  is the better live model, but the file path is not the live path: it can afford
  small.en and it can afford 30 s chunks with 5 s of overlap, and that context is
  worth more than the model. Whisper gains from it and Parakeet cannot, having no
  decoder to carry anything across a window - merging the same segments up to 30 s
  made Parakeet *worse*, 24.0% against 22.8%, and it falls off a cliff from there -
  31.5% at 60 s, 44.1% at 120 s - while small.en in 30 s chunks reaches 18.6%. So the file path stays
  Whisper, and the two paths differ in more than model size.

  Moonshine (`onnx-community/moonshine-base-ONNX`) also sizes its encoder to the
  audio and is cheaper than either, but transcribed this recording worse than
  tiny.en. A shorter Whisper encoder window is not an option: the positional
  embedding is a 1500-frame constant in the exported graph, so anything but 30 s
  fails to broadcast.
- Every Whisper variant uses an `fp32` encoder (353 MB for small, 82 MB for base), because
  the encoder is where a Whisper model's accuracy lives, and a `q4` decoder on
  **both** devices - the pairing Hugging Face's own WebGPU Whisper demos use.
- **Do not "fix" the WASM decoder to `q8`.** It is the documented WASM default and
  it does not load: `onnxruntime-web` 1.25, which Transformers.js 4.x pulls in,
  rewrites int8 QDQ weights into `MatMulNBits`, and the Whisper exports on the Hub
  predate that and carry no scale tensors for it. Session creation fails with
  `ERROR_CODE: 1, qdq_actions.cc:137 TransposeDQWeightsForMatMulNBits Missing
  required scale`, on every browser, WASM only
  ([transformers.js#1707](https://github.com/huggingface/transformers.js/issues/1707),
  [onnxruntime#28306](https://github.com/microsoft/onnxruntime/issues/28306)).
  The name says 4-bit, which reads like a WebGPU-only problem and is not: the
  optimiser *converts to* `MatMulNBits`, so 8-bit weights reach it too. `fp32` is
  the other loadable option, at roughly 4x the download.
- `navigator.gpu` is a browser capability, not an ONNX Runtime one, and the gap
  between the two is a trap. The WebGPU execution provider is compiled only into
  the `asyncify` and `jspi` runtime builds; Transformers.js deliberately points
  Safari at the plain build (`backends/onnx.js`, the `IS_SAFARI` branch) to dodge
  an Asyncify memory leak on Apple devices, so on iPad Safari `navigator.gpu`
  exists but session creation throws `webgpuInit is not a function`.
  `webgpuUsable()` therefore asks the *runtime* which build it loaded rather than
  sniffing the user agent, which means it re-enables itself if upstream ever
  lifts the carve-out.
- A failed load is re-thrown naming the model and device, because the runtime's
  own message names a graph node rather than the choice that has to change.
- **That has to be checked before the first session, not caught around it.**
  Transformers.js chains every session creation onto one module-level promise
  with no `.catch` (`backends/onnx.js`, `webInitChain`). One rejection poisons
  that promise for the lifetime of the page: `rejected.then(load)` never runs
  `load`, so every later attempt re-throws the *first* error. A try/catch that
  retries on another device therefore reports the original WebGPU failure and
  looks like the fallback silently never ran. Do not replace the up-front check
  with a retry.
- `src/lib/audio.ts` decodes inside an `AudioContext({ sampleRate: 16000 })` so
  resampling happens *during* decode. An hour of 48 kHz stereo lands at ~440 MB
  instead of ~1.3 GB. Do not "simplify" this to a default AudioContext.

### 🧵 Threads On A Static Site

Multi-threaded WASM needs `SharedArrayBuffer`, which needs the page to be
cross-origin isolated, which needs two response headers - and GitHub Pages sends
no headers it is not told to, which is to say none. That is the whole reason the
WASM tier was single-threaded, and it is worth several times the throughput on
the one device that has none to spare. How many threads depends on the device:
`wasmThreads()` takes half of `navigator.hardwareConcurrency`, capped at four,
and an iPad Air 5 reports four cores, so it runs on two.

A service worker can send them. `public/coi-serviceworker.js` intercepts every
fetch and re-issues the response with `Cross-Origin-Opener-Policy: same-origin`
and `Cross-Origin-Embedder-Policy: require-corp`; `src/lib/isolate.ts` registers
it before the app mounts and reloads once, because the document that registered
the worker was itself delivered without the headers. Load-bearing details:

- **It covers workers too**, which is the point: a service worker is in front of
  every same-origin request, not just the document, and isolation is a property
  of the whole agent cluster - so the ONNX Runtime proxy worker and the pthreads
  it spawns are isolated as well. Verified end to end in Chrome against a plain
  static server sending no headers: `crossOriginIsolated` false on first load,
  true after the reload, `SharedArrayBuffer` present, Whisper then running on
  four threads.
- **The reload happens once per session.** A `sessionStorage` marker is set
  before reloading, so a browser that refuses to isolate - or a private window
  where registration throws - gets one wasted reload rather than a loop.
- **Failure is a downgrade, never a break.** `wasmThreads()` asks the page
  whether it actually is isolated, so a device that cannot be gets one thread and
  the one-thread model. This can add capability and cannot remove it, which is
  what makes it safe to ship to a device it has not been tried on.
- **`require-corp` is the only option**, since Safari has no `credentialless`,
  and it means every cross-origin subresource has to be CORS-clean. Both are:
  the model weights from the Hugging Face CDN and the ONNX Runtime build from
  jsdelivr are fetched in CORS mode and answer with `access-control-allow-origin`.
  Adding a cross-origin asset that is not would break the app rather than just
  that asset.
- **Decode moves off the main thread** with it (`wasm.proxy = true`, which
  Transformers.js leaves off). A decode is one to two seconds of blocking work,
  and the main thread is where the UI and the worklet's `postMessage` handler
  live.

## 🚀 Bootstrap

Scaffolded with Vuetify CLI.

First:

```shell
podman run -it --rm --network host -v $PWD:/w -w /w --entrypoint /bin/sh node:26-alpine3.23
```

Then:

```shell
export PNPM_HOME=/root/.local/share/pnpm
export PATH="$PNPM_HOME:$PATH"
export SHELL=sh
touch ~/.shrc
export ENV=~/.shrc
npx get-pnpm
source ~/.shrc
pnpm create vuetify
# - Start from a preset? Start from scratch
# - Which framework would you like to use? Vue
# - Which CSS framework? Tailwind CSS
# - Select features to install? ESLint
```

## ❗️ Documentation

- Primary docs: https://vuetifyjs.com/
- Getting started guide: https://vuetifyjs.com/en/getting-started/installation/
- Community support: https://community.vuetifyjs.com/
- Issue tracker: https://issues.vuetifyjs.com/

## 📜 Project Rules & Conventions

- Follow the existing code style and patterns.
- Use pnpm for running all project commands.
- Keep code in TypeScript.

## 🧱 Stack

- Framework: Vue 3 + Vite
- UI Library: Vuetify
- Language: TypeScript
- Package manager: pnpm
- Enabled Features: ESLint, Tailwind CSS

## 🧭 Start Here

- Main entry: `src/main.ts`
- Main app component: `src/App.vue`
- Main styles: `src/styles/`
- Plugin setup: `src/plugins/`

## 📁 Project Structure

- `src/main.ts` - application entry point
- `src/App.vue` - root component
- `src/components/` - reusable Vue components
- `src/plugins/` - plugin registration and setup
- `src/styles/` - global styles and theme settings
- `public/` - static public files

## ✨ Enabled Features

- ESLint
- Tailwind CSS

## 💿 Install

Use your selected package manager (pnpm) to install dependencies:

```bash
pnpm install
```

## 🚀 Quick Start

```bash
pnpm install
pnpm dev
```

## 🏗️ Build

```bash
pnpm build
```

## 🧪 Available Scripts

- `pnpm dev`
- `pnpm build`
- `pnpm preview`
- `pnpm build-only`
- `pnpm type-check`
- `pnpm check:vad`
- `pnpm lint`
- `pnpm lint:fix`

There is no test suite and no test runner, with one exception: `pnpm check:vad`
runs `scripts/vad-check.mts` against `src/lib/vad.ts`. It is plain Node with no
dependency and no build step - Node strips the types and runs the file, which is
why it can import a `.ts` source directly - and it is not the seed of a test
framework. The VAD earns it by being the one piece here that is pure,
deterministic, and impossible to eyeball, since its input is a room and its
output is an audio segment. The first cases cover the segmentation state
machine, including what a boundary needs before it ends a segment; the rest
cover the adaptive floor, the hysteresis and the padding. Two of them were written after
catching real bugs, so if you change `vad.ts`, run it.

Everything else is verified with `lint`, `type-check`, `build`, and by driving the
app in a browser. CI runs `install --frozen-lockfile`, `lint`, `check:vad`, then
`build` - matching that sequence locally is the closest thing to a pre-flight
check.

## 💥 Gotchas

**Fonts flow through CSS variables.** `src/styles/tailwind.css` defines
`--font-heading/body/mono`, and `settings.scss` maps Vuetify's `$body-font-family` to
them. Changing the font is a three-line edit there, not SCSS surgery. Faces are
bundled by `unplugin-fonts` from `@fontsource-variable/*` - there are no CDN requests
anywhere in the build, and it should stay that way.

**`base: '/souffleur/'`** in `vite.config.mts` is required for project-page hosting.
Assets 404 without it.

**The layout has a stability contract.** `.shell` is `100dvh`, controls are
`flex: 0 0 auto`, and `.content` is `flex: 1 1 0; min-height: 0` so only the pane
inside the active tab scrolls. This keeps an arriving answer from shifting the
toolbar. `CameraPreview` has a fixed 64×40 box for the same reason: a `<video>` has no
intrinsic size until its stream starts.

**`eslint.config.js` ignores `pnpm-workspace.yaml`.** `eslint-config-vuetify` lints
YAML with a JavaScript comment rule, so *any* `#` comment there is reported as a
malformed block comment.

## 🔗 Dependencies

`pnpm-workspace.yaml` enforces the recommendations from
https://pnpm.io/supply-chain-security. The one that bites:

**`minimumReleaseAge: 10080`** - a package version must be a week old before it can be
resolved, and pnpm 11 checks this against the committed lockfile too. Adding a
freshly published dependency fails with `ERR_PNPM_NO_MATURE_MATCHING_VERSION`. Either
target an older version, or add a specific `pkg@version` to `minimumReleaseAgeExclude`
if the exemption is genuinely justified.

`allowBuilds` denies install scripts for four Node-side transitive packages
(`@parcel/watcher`, `onnxruntime-node`, `protobufjs`, `sharp`) that a browser build
never loads. pnpm 11 treats an unapproved install script as a hard error, so a new
dependency with a postinstall must be added to that map explicitly.

The three `@langchain/*` packages release several times a week, so the version
ranges in `package.json` are floored at the newest release that was already a week
old when they were added. `@anthropic-ai/sdk` is no longer a direct dependency:
`@langchain/anthropic` pins its own (`^0.115.0`, which under a `0.x` caret means
`0.115.x` and not the `0.117` this project used to carry), and `@langchain/openai`
brings `openai` the same way. None of the five has an install script.

## 🚦 CI

`.github/workflows/souffleur.yml` builds and deploys to Pages on push to `main`. It
does **not** pin a pnpm version - `pnpm/action-setup` reads `packageManager` from
`package.json`, so bump that field rather than the workflow. Its `node-version: 22`
resolves to the newest 22.x, which matters for `check:vad`: that script is run
straight from TypeScript, and Node only strips types without a flag from 22.18
onwards. Pinning a specific older 22.x would break that step and nothing else. `codeql.yml` scans
`actions,javascript-typescript`.
