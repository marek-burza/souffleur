#!/usr/bin/env -S uv run --script
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "librosa",
#     "numpy",
#     "pillow",
#     "pytest",
#     "torch",
#     "transformers",
#     "typer",
# ]
# ///
# Records a live transcript of the microphone and/or speaker audio to a text file, and
# refreshes a screenshot file at an interval, optionally copying both to Google Drive.

import collections
import dataclasses
import enum
import logging
import queue
import subprocess
import threading
import warnings
from pathlib import Path

import numpy as np
import torch
import typer
from PIL import ImageGrab
from transformers import CohereAsrForConditionalGeneration, CohereAsrProcessor
from transformers.utils import logging as transformers_logging


warnings.filterwarnings('ignore', message='You are sending unauthenticated requests')
transformers_logging.set_verbosity_error()
logging.getLogger('huggingface_hub').setLevel(logging.ERROR)


SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # int16 / s16le


class Source(str, enum.Enum):
    MICROPHONE = 'microphone'
    SPEAKER = 'speaker'
    AUDIO = 'audio'
    SCREEN = 'screen'
    ALL = 'all'


# Gated on the Hub: accept its terms on the model page and set HF_TOKEN.
COHERE_HUGGINGFACE_ID = 'CohereLabs/cohere-transcribe-03-2026'
# Without a language the model guesses one per segment, and on short, quiet segments it
# guesses Portuguese or Arabic and can loop.
LANGUAGE = 'en'
MAX_NEW_TOKENS = 256


class CohereTranscriber:
    def __init__(self, model_id: str, device: str, dtype: torch.dtype) -> None:
        self._device = device
        self._dtype = dtype
        self._processor = CohereAsrProcessor.from_pretrained(model_id)
        self._model = CohereAsrForConditionalGeneration.from_pretrained(model_id, dtype=dtype).to(device).eval()

    @torch.inference_mode()
    def __call__(self, audio: np.ndarray) -> str:
        inputs = self._processor(audio, sampling_rate=SAMPLE_RATE, return_tensors='pt', language=LANGUAGE)
        inputs.to(self._device, dtype=self._dtype)
        outputs = self._model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS)
        return self._processor.decode(outputs, skip_special_tokens=True)[0].strip()


def load_transcriber() -> CohereTranscriber:
    print('Loading model...')
    device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
    dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
    return CohereTranscriber(COHERE_HUGGINGFACE_ID, device, dtype)


@dataclasses.dataclass
class Device:
    default: bool
    device: str
    name: str


@dataclasses.dataclass
class AudioChunk:
    origin: str
    data: np.ndarray


class TranscriptFile:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.write_text('')

    def add(self, text: str) -> None:
        with self._lock, open(self._path, 'a') as handle:
            handle.write(text + '\n')


"""
Energy-envelope Voice Activity Detection (VAD): it takes frames of audio, decides which of
them carry speech, and hands back one utterance at a time.

A frame is a small fixed-size slice of samples - the unit the VAD looks at one at a time
rather than all at once. At 16 kHz, 20 ms is 320 samples, and every call to feed() takes
exactly that many. 20 ms is the usual choice in speech processing because it matches the
time scale of a phoneme: short enough that speech/silence transitions are caught quickly,
long enough that the RMS energy is stable rather than fooled by individual waveform peaks.
At 10 ms the energy estimate gets noisy; at 40 ms fast transitions start to slip through.

Two things keep a plain energy test usable, both still inside the energy envelope rather
than reaching for a model: a threshold that rises with the room's noise floor, and
pre-speech padding, so a segment does not begin midway through its first word.

The remaining weakness is inherent to measuring energy: RMS cannot tell speech from any
other sound at the same level, so a slammed door in a quiet room still reaches Whisper.
A drop-in replacement using `silero-vad` (~1.8 MB JIT model, sub-ms inference per 32 ms
frame) would keep the same feed/flush contract (with the window fixed to 512 samples,
Silero's 16 kHz requirement, and the RMS check replaced with model inference).
"""

FRAME_MS = 20
MIN_SPEECH_MS = 300

# The threshold is a fixed minimum, raised to a multiple of the measured noise floor when
# the room is louder than that allows. Too low for the room and every frame reads as
# speech, so segments only ever end at the cap and Whisper is handed 15 s of noise - which
# it does not return empty for, it hallucinates straight into the transcript.
#
# It never drops below MIN_THRESHOLD, and there is one threshold rather than a lower one
# for keeping a segment open. Both were tried: a lower hold level lets a remote voice
# picked up from the speakers bridge the pause between turns, so an utterance runs on into
# the other speaker until the cap cuts it mid-word, and Whisper loops on exactly that audio
# ("sad, sad, sad, ...") to the end of its token budget.
MIN_THRESHOLD = 0.008
NOISE_MULTIPLE = 3

# The floor follows quiet quickly and loud slowly, which is what keeps a speaker from being
# measured as their own noise. A rolling minimum is wrong: with no pause inside the window
# the minimum climbs into the voice, and the VAD goes deaf mid-sentence. Falling fast
# measures a quiet room almost immediately and lets every gap between words drag the
# estimate back down; rising slowly learns a fan in seconds, while no plausible unbroken
# utterance lifts the floor to its own level.
FLOOR_FALL = 0.25
FLOOR_RISE = 0.002

# A pause only ends a segment once it holds MIN_EMIT_MS of speech; below that the segment
# stays open until LONG_SILENCE_MS, so a fragment joins the utterance next to it instead of
# being decoded alone - Whisper returns a plausible wrong word for a one-word segment.
MIN_SILENCE_MS = 600
MIN_EMIT_MS = 700
LONG_SILENCE_MS = 2000

# A cap rather than a preference: without it, someone talking continuously produces no
# transcript until they pause, and the segment would run past Whisper's 30 s window.
MAX_SPEECH_MS = 15000

# Audio kept from before the threshold was crossed. Word onsets are quiet, so a segment that
# starts at the crossing starts inside its first word, and Whisper guesses a plausible word
# there. Free at inference time, since Whisper pads every input to 30 s regardless.
PAD_MS = 240


class VadAccumulator:
    def __init__(
        self,
        frame_ms: int = FRAME_MS,
        min_silence_ms: int = MIN_SILENCE_MS,
        min_speech_ms: int = MIN_SPEECH_MS,
        min_emit_ms: int = MIN_EMIT_MS,
        long_silence_ms: int = LONG_SILENCE_MS,
        max_speech_ms: int = MAX_SPEECH_MS,
    ) -> None:
        self.frame_samples = SAMPLE_RATE * frame_ms // 1000
        self._min_silence_frames = min_silence_ms // frame_ms
        self._min_speech_frames = min_speech_ms // frame_ms
        self._min_emit_frames = min_emit_ms // frame_ms
        self._long_silence_frames = long_silence_ms // frame_ms
        self._max_speech_samples = int(max_speech_ms / 1000 * SAMPLE_RATE)
        self._buffer: list[np.ndarray] = []
        self._buffered = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._in_speech = False
        # Cleared whenever a segment is emitted: its trailing silence is already inside that
        # segment, so padding the next one with it would repeat audio across two lines.
        self._pad: collections.deque[np.ndarray] = collections.deque(maxlen=PAD_MS // frame_ms)
        self._floor = 0.0
        self.energy = 0.0

    @property
    def threshold(self) -> float:
        return max(MIN_THRESHOLD, self._floor * NOISE_MULTIPLE)

    def feed(self, frame: np.ndarray) -> np.ndarray | None:
        energy = float(np.sqrt(np.mean(frame ** 2)))
        # Measured before the frame is classified, so the floor tracks the room rather than
        # the decision made about it.
        self._observe(energy)
        if energy >= self.threshold:
            if not self._in_speech:
                for held in self._pad:
                    self._append(held)
                self._pad.clear()
                self._in_speech = True
            self._append(frame)
            # Padding must not count here, or the minimum-speech gate is satisfied by silence.
            self._speech_frames += 1
            self._silence_frames = 0
            if self._buffered >= self._max_speech_samples:
                return self.flush()
        elif self._in_speech:
            self._append(frame)
            self._silence_frames += 1
            enough = (
                self._min_silence_frames
                if self._speech_frames >= self._min_emit_frames
                else self._long_silence_frames
            )
            if self._silence_frames >= enough:
                return self.flush()
        else:
            self._pad.append(frame)
        return None

    def flush(self) -> np.ndarray | None:
        segment: np.ndarray | None = None
        if self._speech_frames >= self._min_speech_frames:
            segment = np.concatenate(self._buffer)
        self._buffer = []
        self._buffered = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._in_speech = False
        self._pad.clear()
        return segment

    def _append(self, frame: np.ndarray) -> None:
        self._buffer.append(frame)
        self._buffered += len(frame)

    def _observe(self, energy: float) -> None:
        self.energy = energy
        rate = FLOOR_FALL if energy < self._floor else FLOOR_RISE
        self._floor += (energy - self._floor) * rate


def pulse_devices(flag: str) -> list[Device]:
    result = subprocess.run(
        ['ffmpeg', flag, 'pulse'],
        capture_output=True,
        text=True,
    )
    devices = []
    listing = False
    for line in result.stdout.splitlines():
        if line.startswith('Auto-detected'):
            listing = True
            continue
        if listing:
            default = line.startswith('*')
            line = line[2:]
            device = line[:line.index(' ')]
            name = line[line.index('[') + 1 : line.index(']')]
            devices.append(Device(default, device, name))
    return devices


def pulse_sources() -> list[Device]:
    return pulse_devices('-sources')


def pulse_sinks() -> list[Device]:
    return pulse_devices('-sinks')


def default_microphone_device(sources: list[Device], override: int | None) -> str:
    try:
        if override is not None:
            return sources[override].device
        return [source.device for source in sources if source.default][0]
    except IndexError:
        raise RuntimeError(
            'No microphone source found. Specify one with --microphone-device.\n'
            'Run with --list-devices to see available sources.'
        )


def default_speaker_device(sources: list[Device], override: int | None) -> str:
    try:
        if override is not None:
            return sources[override].device
        default = [sink for sink in pulse_sinks() if sink.default][0].device + '.monitor'
        return [s.device for s in sources if s.device == default][0]
    except IndexError:
        raise RuntimeError(
            'No monitor source found. Specify one with --speaker-device.\n'
            'Run with --list-devices to see available sources.'
        )


def transcribe_worker(
    transcribe: CohereTranscriber,
    multi_source: bool,
    audio: queue.Queue[AudioChunk | None],
    transcript: TranscriptFile,
) -> None:
    while True:
        chunk = audio.get()
        if chunk is None:
            break
        text = transcribe(chunk.data)
        if text:
            line = f'[{chunk.origin}] {text}' if multi_source else text
            print(line)
            transcript.add(line)


def capture_worker(
    device: str,
    origin: str,
    audio: queue.Queue[AudioChunk | None],
    exit: threading.Event,
    vad: VadAccumulator,
) -> None:
    cmd = [
        'ffmpeg', '-loglevel', 'quiet',
        '-f', 'pulse', '-i', device,
        '-f', 's16le', '-ar', str(SAMPLE_RATE), '-ac', '1',
        'pipe:1',
    ]
    frame_bytes = vad.frame_samples * BYTES_PER_SAMPLE
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            while not exit.is_set():
                raw = process.stdout.read(frame_bytes)
                if not raw:
                    break
                frame = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                segment = vad.feed(frame)
                if segment is not None:
                    audio.put(AudioChunk(origin, segment))
            remainder = vad.flush()
            if remainder is not None:
                audio.put(AudioChunk(origin, remainder))
        finally:
            process.terminate()
            process.wait()


def decode_file(path: Path) -> np.ndarray:
    cmd = [
        'ffmpeg', '-loglevel', 'error',
        '-i', str(path),
        '-f', 's16le', '-ar', str(SAMPLE_RATE), '-ac', '1',
        'pipe:1',
    ]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def speech_segments(audio: np.ndarray, vad: VadAccumulator) -> list[np.ndarray]:
    segments = []
    for start in range(0, len(audio) - vad.frame_samples + 1, vad.frame_samples):
        segment = vad.feed(audio[start:start + vad.frame_samples])
        if segment is not None:
            segments.append(segment)
    remainder = vad.flush()
    if remainder is not None:
        segments.append(remainder)
    return segments


def screen_worker(
    path: Path,
    exit: threading.Event,
    interval: float,
) -> None:
    while not exit.is_set():
        try:
            partial = path.with_name(f'.{path.stem}.partial{path.suffix}')
            ImageGrab.grab().save(partial)
            partial.replace(path)
        except Exception as e:
            print(f'Screen capture error: {e}')
        exit.wait(interval)


RCLONE_REMOTE = 'transcriptor'
# drive.file: rclone sees only the files it uploaded itself, not the rest of the Drive.
RCLONE_DRIVE_SCOPE = 'drive.file'
# The last sync pass runs on Ctrl+C, and an upload takes longer than the 1 s other threads get.
SYNC_STOP_TIMEOUT = 15.0


def ensure_rclone_remote() -> None:
    remotes = subprocess.run(['rclone', 'listremotes'], capture_output=True, text=True, check=True).stdout.split()
    if f'{RCLONE_REMOTE}:' in remotes:
        return
    print(f'No rclone remote "{RCLONE_REMOTE}", authorising Google Drive in the browser...')
    subprocess.run(['rclone', 'config', 'create', RCLONE_REMOTE, 'drive', f'scope={RCLONE_DRIVE_SCOPE}'], check=True)


def upload(path: Path) -> bool:
    result = subprocess.run(
        ['rclone', 'copyto', str(path), f'{RCLONE_REMOTE}:{path.name}'],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f'Sync error for {path.name}: {result.stderr.strip().splitlines()[-1:]}')
    return result.returncode == 0


def modification(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


def sync_pass(paths: list[Path], synced: dict[Path, tuple[int, int]]) -> None:
    for path in paths:
        state = modification(path)
        if state is not None and synced.get(path) != state and upload(path):
            synced[path] = state


def sync_worker(
    paths: list[Path],
    exit: threading.Event,
    interval: float,
) -> None:
    synced: dict[Path, tuple[int, int]] = {}
    while not exit.wait(interval):
        sync_pass(paths, synced)
    sync_pass(paths, synced)


app = typer.Typer(no_args_is_help=True)


@app.command()
def devices() -> None:
    """List available PulseAudio sources, numbered as --microphone-device and --speaker-device expect."""
    print('\n'.join([f'{i}. {source.name}' for i, source in enumerate(pulse_sources())]))


@app.command()
def live(
    source: Source = typer.Option(
        Source.AUDIO,
        '--source',
        help='What to record: microphone, speaker (loopback), audio (both), screen or all.',
    ),
    microphone_device: int | None = typer.Option(
        None,
        '--microphone-device',
        help='PulseAudio source index for microphone. Auto-detected if not provided.',
    ),
    speaker_device: int | None = typer.Option(
        None,
        '--speaker-device',
        help='PulseAudio source index for speaker (loopback). Auto-detected if not provided.',
    ),
    min_silence_ms: int = typer.Option(
        MIN_SILENCE_MS,
        '--min-silence-ms',
        help='Milliseconds of silence required to end a speech segment.',
    ),
    max_speech_ms: int = typer.Option(
        MAX_SPEECH_MS,
        '--max-speech-ms',
        help='Maximum milliseconds of speech before forcing a speech segment boundary.',
    ),
    transcript_path: Path = typer.Option(
        Path('transcript.txt'),
        '--transcript',
        help='File the transcript is written to, one line per speech segment, prefixed [local] or [remote] when both are recorded. Truncated on start.',
    ),
    screenshot_path: Path = typer.Option(
        Path('screenshot.png'),
        '--screenshot',
        help='File the latest screenshot is written to, overwritten on every capture.',
    ),
    screenshot_interval: float = typer.Option(
        2.0,
        '--screenshot-interval',
        help='Seconds between screenshots.',
    ),
    sync: bool = typer.Option(
        False,
        '--sync',
        help='Copy the transcript and screenshot to the root of Google Drive through rclone. The first run signs in through the browser.',
    ),
    sync_interval: float = typer.Option(
        3.0,
        '--sync-interval',
        help='Seconds between sync passes; each pass uploads only the files that changed.',
    ),
) -> None:
    """Transcribe live audio and capture screenshots at an interval."""
    if sync:
        ensure_rclone_remote()
    sources = pulse_sources()
    exit = threading.Event()
    audio: queue.Queue[AudioChunk | None] = queue.Queue()
    threads: list[threading.Thread] = []

    capture_devices: list[tuple[str, str]] = []
    if source in [Source.MICROPHONE, Source.AUDIO, Source.ALL]:
        capture_devices.append(('local', default_microphone_device(sources, microphone_device)))
    if source in [Source.SPEAKER, Source.AUDIO, Source.ALL]:
        capture_devices.append(('remote', default_speaker_device(sources, speaker_device)))

    if capture_devices:
        transcribe = load_transcriber()
        threads.append(threading.Thread(
            target=transcribe_worker,
            args=(transcribe, len(capture_devices) > 1, audio, TranscriptFile(transcript_path)),
            daemon=True,
        ))
        for origin, capture_device in capture_devices:
            vad = VadAccumulator(min_silence_ms=min_silence_ms, max_speech_ms=max_speech_ms)
            threads.append(threading.Thread(
                target=capture_worker,
                args=(capture_device, origin, audio, exit, vad),
                daemon=True,
            ))
    if source in [Source.SCREEN, Source.ALL]:
        threads.append(threading.Thread(
            target=screen_worker,
            args=(screenshot_path, exit, screenshot_interval),
            daemon=True,
        ))
    sync_thread: threading.Thread | None = None
    if sync:
        synced_paths = ([transcript_path] if capture_devices else []) + (
            [screenshot_path] if source in [Source.SCREEN, Source.ALL] else []
        )
        sync_thread = threading.Thread(
            target=sync_worker,
            args=(synced_paths, exit, sync_interval),
            daemon=True,
        )
        threads.append(sync_thread)

    print('Running... (Ctrl+C to exit)')
    for thread in threads:
        thread.start()

    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        print('\nStopping...')
        exit.set()
        for thread in threads:
            thread.join(timeout=SYNC_STOP_TIMEOUT if thread is sync_thread else 1)
    finally:
        audio.put(None)


@app.command()
def recorded(
    recording: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        help='Audio or video file to transcribe, in any format ffmpeg reads.',
    ),
    transcript_path: Path = typer.Option(
        Path('transcript.txt'),
        '--transcript',
        help='File the transcript is written to, one line per speech segment. Overwritten.',
    ),
    min_silence_ms: int = typer.Option(
        MIN_SILENCE_MS,
        '--min-silence-ms',
        help='Milliseconds of silence required to end a speech segment.',
    ),
    max_speech_ms: int = typer.Option(
        MAX_SPEECH_MS,
        '--max-speech-ms',
        help='Maximum milliseconds of speech before forcing a speech segment boundary.',
    ),
) -> None:
    """Transcribe a recording made elsewhere."""
    transcribe = load_transcriber()
    print(f'Transcribing {recording}...')
    vad = VadAccumulator(min_silence_ms=min_silence_ms, max_speech_ms=max_speech_ms)
    transcript = TranscriptFile(transcript_path)
    lines = 0
    for segment in speech_segments(decode_file(recording), vad):
        text = transcribe(segment)
        if text:
            transcript.add(text)
            lines += 1
    print(f'Wrote {lines} lines to {transcript_path}')


if __name__ == '__main__':
    app()


# Tests - run with: uv run --with pytest --with numpy --with pillow --with torch --with transformers --with typer --with librosa python -m pytest transcriptor.py -v
class TestVadAccumulator:
    _FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 320 samples per 20ms frame

    @staticmethod
    def _speech(amplitude: float = 0.05) -> np.ndarray:
        t = np.arange(TestVadAccumulator._FRAME_SAMPLES) / SAMPLE_RATE
        return (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    @staticmethod
    def _silence() -> np.ndarray:
        return np.zeros(TestVadAccumulator._FRAME_SAMPLES, dtype=np.float32)

    @staticmethod
    def _room(level: float) -> np.ndarray:
        """Steady non-speech at a known RMS, standing in for a fan or an air vent."""
        frame = np.full(TestVadAccumulator._FRAME_SAMPLES, level, dtype=np.float32)
        frame[1::2] = -level
        return frame

    @staticmethod
    def _first(vad: VadAccumulator, frame: np.ndarray, count: int) -> np.ndarray | None:
        for _ in range(count):
            segment = vad.feed(frame)
            if segment is not None:
                return segment
        return None

    def test_silence_only_emits_nothing(self):
        vad = VadAccumulator()
        assert self._first(vad, self._silence(), 200) is None
        assert vad.flush() is None

    def test_speech_then_silence_emits_segment(self):
        vad = VadAccumulator()
        assert self._first(vad, self._speech(), 60) is None
        segment = self._first(vad, self._silence(), 40)
        assert segment is not None
        assert len(segment) >= 60 * self._FRAME_SAMPLES

    def test_short_speech_below_min_is_discarded(self):
        vad = VadAccumulator(min_silence_ms=200, min_speech_ms=500)
        self._first(vad, self._speech(), 5)
        assert self._first(vad, self._silence(), 50) is None

    def test_brief_silence_does_not_split(self):
        vad = VadAccumulator()
        assert self._first(vad, self._speech(), 35) is None
        assert self._first(vad, self._silence(), 10) is None
        assert self._first(vad, self._speech(), 35) is None
        segment = self._first(vad, self._silence(), 40)
        assert segment is not None
        assert len(segment) >= 60 * self._FRAME_SAMPLES

    def test_max_speech_cap_forces_emit(self):
        vad = VadAccumulator(max_speech_ms=1000)
        segment = self._first(vad, self._speech(), 75)
        assert segment is not None
        assert abs(len(segment) - SAMPLE_RATE) < 2 * self._FRAME_SAMPLES

    def test_fragment_waits_for_next_utterance(self):
        vad = VadAccumulator()
        assert self._first(vad, self._speech(), 30) is None
        assert self._first(vad, self._silence(), 40) is None
        assert self._first(vad, self._speech(), 60) is None
        segment = self._first(vad, self._silence(), 40)
        assert segment is not None
        assert len(segment) >= 130 * self._FRAME_SAMPLES

    def test_flush_returns_accumulated_speech(self):
        vad = VadAccumulator()
        self._first(vad, self._speech(), 25)
        segment = vad.flush()
        assert segment is not None
        assert len(segment) == 25 * self._FRAME_SAMPLES

    def test_flush_on_empty_returns_none(self):
        assert VadAccumulator().flush() is None

    def test_segment_starts_before_first_loud_frame(self):
        vad = VadAccumulator()
        self._first(vad, self._silence(), 30)
        self._first(vad, self._speech(), 25)
        segment = vad.flush()
        assert segment is not None
        assert len(segment) == (PAD_MS // FRAME_MS + 25) * self._FRAME_SAMPLES

    def test_padding_is_cleared_on_emit(self):
        vad = VadAccumulator()
        self._first(vad, self._speech(), 60)
        assert self._first(vad, self._silence(), 40) is not None
        self._first(vad, self._speech(), 60)
        segment = vad.flush()
        assert segment is not None
        assert len(segment) == 60 * self._FRAME_SAMPLES

    def test_quiet_voice_after_turn_gap_does_not_extend_segment(self):
        vad = VadAccumulator()
        segment = self._first(vad, self._speech(), 60)
        if segment is None:
            segment = self._first(vad, self._silence(), 10)
        if segment is None:
            segment = self._first(vad, self._speech(0.0106), 750)
        assert segment is not None
        assert len(segment) < 2 * SAMPLE_RATE

    def test_noisy_room_stops_triggering_once_learned(self):
        vad = VadAccumulator()
        learned_after = None
        for index in range(1500):
            vad.feed(self._room(0.02))
            if learned_after is None and vad.threshold > 0.02:
                learned_after = index / 50
        assert learned_after is not None
        assert 0 < learned_after < 15

    def test_unbroken_speech_does_not_go_deaf(self):
        vad = VadAccumulator()
        kept = 0
        for _ in range(150):
            for frame, count in [(self._speech(), 15), (self._speech(0.011), 5)]:
                for _ in range(count):
                    segment = vad.feed(frame)
                    if segment is not None:
                        kept += len(segment)
        tail = vad.flush()
        if tail is not None:
            kept += len(tail)
        assert kept / SAMPLE_RATE > 58


# The model is gated - accept its terms on the Hub and export HF_TOKEN first.
# Frequently used: uv run transcriptor.py live --source all
# Copy to the root of Google Drive as it records: uv run transcriptor.py live --source all --sync
# Pick devices: uv run transcriptor.py devices, then live --microphone-device N --speaker-device M
# Transcribe a file: uv run transcriptor.py recorded recording.m4a --transcript recording.txt
