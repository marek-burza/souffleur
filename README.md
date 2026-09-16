# Souffleur

Proof-of-concept for use when practicing interviews or exams - [try it out](https://marek-burza.github.io/souffleur/) (deployed as a static site on GitHub Pages)!

An Anthropic or OpenAI API key is entered in a dialog and kept in `LocalStorage`.

## 🏛️ Architecture

`src/App.vue` is the only stateful component. It owns the composables and passes
plain values down; the child components hold no session state.

```text
useRecognition(addLine) ──> useTranscript ──> TranscriptPane (editable)
useCamera ──> CameraPreview + capture() ──┐
                                          ├─> lib/solver solve() ──> AnswerPane
SettingsDialog (key, model, file upload) ─┘
```

Two transcription paths, one `addLine` contract: each emits a line per utterance
and neither knows about the other. `App.vue` stops a running live session before
starting anything else, since both paths want the same microphone.

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
  - *Adaptive floor.* The threshold is `0.01` or 3x a measured noise floor,
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
it afford the larger model of each pair.

Both paths share `src/lib/whisper.ts` for loading. Notes that matter:

- The `@huggingface/transformers` import is **dynamic** so the ~500 kB chunk and the
  22 MB ONNX Runtime WASM stay out of the initial load. Keep it that way.
- **One model per device type, per path.** File upload gets
  `onnx-community/whisper-small.en` on WebGPU and `onnx-community/whisper-base.en`
  on WASM; the live path steps both down, to base.en and tiny.en. The
  WASM path is single-threaded (see cross-origin isolation below), and small.en's
  encoder is ~350 GFLOP per 30 s window against base's ~90, so on CPU small runs
  1.5-3x slower than real time and base comfortably under it. That is survivable
  for a file, which only has to finish, and fatal live, where decoding has to keep
  ahead of speech - hence tiny.en on the CPU fallback, which is not a guess:
  base.en was tried there on an iPad and did not work. All four are English-only,
  so none needs a language token, and all are plain Whisper, so all want the 30 s
  window rather than distil-whisper's longer one.
- Every variant uses an `fp32` encoder (353 MB for small, 82 MB for base), because
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
output is an audio segment. The first seven cases cover the segmentation state
machine; the rest cover the adaptive floor, the hysteresis and the padding. Two
of them were written after catching real bugs, so if you change `vad.ts`, run it.

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
