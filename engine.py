#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# VibeVoice — MIT
# Copyright (c) 2026 VibeVoice contributors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# ---------------------------------------------------------------------------
# engine.py — standalone speech-to-text engine for VibeVoice (macOS).
#
# Captures the microphone, decides speech/non-speech with Silero VAD (falling
# back to an energy threshold), transcribes with mlx_whisper (Apple Silicon),
# then optionally pastes the result into the frontmost application.
#
# It communicates with the companion UI ("the pill") exclusively through three
# small files in ~/.vibevoice/ (the STATE-FILE CONTRACT below). The engine is
# the sole writer of those files; the pill only reads them.
#
# STATE-FILE CONTRACT (shared pill <-> engine):
#   ~/.vibevoice/state       text file, one of: idle | recording | transcribing
#   ~/.vibevoice/levels.bin  60 float32 little-endian RMS values (0..1),
#                            written atomically (tmp + os.replace)
#   ~/.vibevoice/raw.txt     last transcription, plain text (sentence only)
#   ~/.vibevoice/history.jsonl  last 20 transcriptions, JSONL {"ts","text"}, newest last
#
# CONTROL FILES (written by the pill / external tools, read by the engine — the
# same external-control pattern as autosend's pause flag, NOT engine-owned state):
#   ~/.vibevoice/muted       presence = mic paused: the engine stays alive but
#                            ignores the microphone (no recording/transcription)
#
# Environment variables:
#   VIBEVOICE_LANG            transcription language code (default: "it")
#   VIBEVOICE_MODEL           mlx_whisper model (default: mlx-community/whisper-turbo)
#   VIBEVOICE_AUTOSEND        "1" to paste into frontmost app (default: "1")
#   VIBEVOICE_AUTOSEND_RETURN "1" to press Return after pasting (default: "0")
#   VIBEVOICE_VP              "1" to capture via macOS voice processing (Apple
#                             AEC/NS/AGC — the full-duplex prerequisite);
#                             "0" forces sounddevice (default: "1"; any
#                             voice-processing failure falls back to sounddevice)
#   VIBEVOICE_SILERO_MODEL    path to a Silero VAD ONNX model; overrides the
#                             copy shipped with the `silero_vad` package. With
#                             neither the model nor onnxruntime available the
#                             speech decision falls back to the RMS threshold.
# ---------------------------------------------------------------------------

import os
import queue
import struct
import sys
import tempfile
import threading
import time
import wave
from collections import deque
from pathlib import Path

import numpy as np

try:
    import sounddevice as sd
except Exception as _sd_err:  # pragma: no cover - environment dependent
    sys.stderr.write(
        "VibeVoice: 'sounddevice' is required for microphone capture.\n"
        "Install it with:  pip install sounddevice\n"
        f"Import error: {_sd_err}\n"
    )
    raise


# ── State directory & contract files ─────────────────────────────────────────
STATE_DIR = Path(os.path.expanduser("~")) / ".vibevoice"
STATE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = STATE_DIR / "state"        # idle | recording | transcribing
LEVELS_FILE = STATE_DIR / "levels.bin"  # 60 float32 LE, RMS 0..1
LEVELS_TMP = STATE_DIR / "levels.tmp"   # staging for atomic replace
RAW_FILE = STATE_DIR / "raw.txt"        # last transcription, plain text
HISTORY_FILE = STATE_DIR / "history.jsonl"  # last 20 transcriptions, JSONL {"ts","text"}
MUTED_FILE = STATE_DIR / "muted"        # control file: presence = mic paused (pill writes, engine reads)
DICT_FILE = STATE_DIR / "dictionary.txt"  # control file: personal terms, one per line (user/tools write, engine reads)
METRICS_FILE = STATE_DIR / "metrics.jsonl"  # per-utterance latency telemetry, JSONL, capped
CORRECTIONS_FILE = STATE_DIR / "corrections.jsonl"  # control file: user corrections (tools write, engine reads)


# ── Configuration ─────────────────────────────────────────────────────────────
LANG = os.environ.get("VIBEVOICE_LANG", "it")
MODEL = os.environ.get("VIBEVOICE_MODEL", "mlx-community/whisper-turbo")
AUTOSEND = os.environ.get("VIBEVOICE_AUTOSEND", "1") == "1"
AUTOSEND_RETURN = os.environ.get("VIBEVOICE_AUTOSEND_RETURN", "0") == "1"
VP_ENABLED = os.environ.get("VIBEVOICE_VP", "1") == "1"  # macOS voice-processing capture

# LLM cleanup pass (the Wispr-style rewrite). OFF by default: with
# VIBEVOICE_CLEANUP unset the dictation path is byte-identical to today's.
CLEANUP_ENABLED = os.environ.get("VIBEVOICE_CLEANUP", "0") == "1"
CLEANUP_URL = os.environ.get(
    "VIBEVOICE_CLEANUP_URL", "https://api.groq.com/openai/v1/chat/completions"
)
CLEANUP_MODEL = os.environ.get("VIBEVOICE_CLEANUP_MODEL", "llama-3.1-8b-instant")
CLEANUP_TIMEOUT = float(os.environ.get("VIBEVOICE_CLEANUP_TIMEOUT", "2.5"))
CLEANUP_API_KEY = (
    os.environ.get("VIBEVOICE_CLEANUP_API_KEY") or os.environ.get("GROQ_API_KEY") or ""
)

SAMPLE_RATE = 16000     # mlx_whisper expects 16 kHz mono
CHANNELS = 1
BLOCKSIZE = 1600        # ~100 ms per audio block at 16 kHz

LEVELS_LEN = 60         # number of float32 RMS samples in levels.bin
LEVELS_HZ = 10          # target write frequency for levels.bin (Hz)

HISTORY_MAX = 20        # max lines in history.jsonl

DICT_MAX_TERMS = 64     # terms fed to Whisper's initial_prompt (its context window is ~224 tokens)
METRICS_MAX = 500       # max lines in metrics.jsonl

VAD_THRESHOLD = 0.015   # RMS above this starts/sustains "recording"
SILERO_ONSET = 0.5      # speech probability that turns the decision ON (hysteresis high)
SILERO_OFFSET = 0.35    # probability that turns it OFF (hysteresis low; in between = hold)
SILERO_FRAME = 512      # samples per Silero inference frame at 16 kHz (model contract)
SILERO_CONTEXT = 64     # context samples prepended to each frame (v5 ONNX contract)
SILERO_QUEUE_MAX = 32   # submit()→worker backlog cap; beyond this, blocks are dropped
SILENCE_SEC = 1.5       # trailing silence that ends an utterance
MIN_DUR = 0.4           # discard utterances shorter than this (seconds)
MAX_DUR = 15.0          # force finalize after this many seconds (short enough to keep each blob within the recognizer's comfort window + sustain rhythm on long dictation)
PRE_ROLL_BLOCKS = 5     # blocks of audio kept before speech onset

RETURN_DELAY = 1.5      # seconds between paste and Return keypress


# ── State file writers (engine is the sole writer) ───────────────────────────

def write_state(state: str) -> None:
    """Write the current engine state. One of: idle | recording | transcribing."""
    try:
        STATE_FILE.write_text(state)
    except Exception:
        # State reporting must never crash the audio loop.
        pass


def write_levels(rms_history: deque) -> None:
    """Write LEVELS_LEN float32 RMS values atomically (tmp + os.replace).

    The history deque holds the most recent RMS values; we left-pad with zeros
    so the file always contains exactly LEVELS_LEN samples.
    """
    try:
        values = list(rms_history)[-LEVELS_LEN:]
        if len(values) < LEVELS_LEN:
            values = [0.0] * (LEVELS_LEN - len(values)) + values
        data = struct.pack(f"<{LEVELS_LEN}f", *values)
        LEVELS_TMP.write_bytes(data)
        os.replace(LEVELS_TMP, LEVELS_FILE)
    except Exception:
        pass


def write_raw(text: str) -> None:
    """Write the last transcription as plain text (sentence only, no metadata)."""
    try:
        RAW_FILE.write_text(text)
    except Exception:
        pass


def _append_history(text: str) -> None:
    """Append to history.jsonl, newest last, capped. Never raises (transcription path)."""
    try:
        import json
        import time
        lines = []
        try:
            lines = HISTORY_FILE.read_text().splitlines()
        except OSError:
            pass
        lines.append(json.dumps({"ts": time.time(), "text": text}))
        HISTORY_FILE.write_text("\n".join(lines[-HISTORY_MAX:]) + "\n")
    except Exception:
        pass


def _append_metrics(entry: dict) -> None:
    """Append one telemetry entry to metrics.jsonl, newest last, capped.

    Same shape and guarantees as _append_history: never raises (it sits on the
    transcription path). You don't improve what you don't measure — this is the
    p99 ledger for the end-of-speech → text budget.
    """
    try:
        import json
        lines = []
        try:
            lines = METRICS_FILE.read_text().splitlines()
        except OSError:
            pass
        lines.append(json.dumps(entry))
        METRICS_FILE.write_text("\n".join(lines[-METRICS_MAX:]) + "\n")
    except Exception:
        pass


def load_dictionary() -> list:
    """Read the personal dictionary (one term per line, `#` starts a comment).

    Missing or unreadable file → empty list: the dictionary is a bias, never a
    dependency. Capped at DICT_MAX_TERMS so the joined prompt stays within
    Whisper's initial_prompt context budget.
    """
    try:
        terms = []
        for line in DICT_FILE.read_text().splitlines():
            term = line.strip()
            if term and not term.startswith("#"):
                terms.append(term)
        return terms[:DICT_MAX_TERMS]
    except Exception:
        return []


def is_muted() -> bool:
    """Return True while the mute control file is present.

    Unlike the three state files above (which the engine owns), `muted` is
    written by the pill's master switch and read here — the same external-control
    pattern as autosend's pause flag. When muted, the engine keeps running but
    ignores the microphone: a pause, not a kill (no TCC re-grant on the way back).
    """
    try:
        return MUTED_FILE.exists()
    except Exception:
        return False


# ── Transcription (mlx_whisper) ───────────────────────────────────────────────

_MLX_WHISPER = None        # lazily imported module
_MLX_AVAILABLE = None      # tri-state: None=unknown, True/False once checked


def _ensure_mlx_whisper() -> bool:
    """Import mlx_whisper lazily. Returns True if available, else prints help."""
    global _MLX_WHISPER, _MLX_AVAILABLE
    if _MLX_AVAILABLE is not None:
        return _MLX_AVAILABLE
    try:
        import mlx_whisper  # type: ignore
        _MLX_WHISPER = mlx_whisper
        _MLX_AVAILABLE = True
    except Exception as err:
        _MLX_AVAILABLE = False
        sys.stderr.write(
            "VibeVoice: 'mlx_whisper' is not available — transcription disabled.\n"
            "It runs Whisper on Apple Silicon via MLX. Install it with:\n"
            "    pip install mlx-whisper\n"
            "On first use it downloads the model (default: "
            f"{MODEL}).\n"
            f"Import error: {err}\n"
        )
    return _MLX_AVAILABLE


def save_wav(audio: np.ndarray, rate: int = SAMPLE_RATE) -> str:
    """Write a float32 [-1, 1] mono signal to a temporary 16-bit WAV file."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    with wave.open(tmp.name, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())
    return tmp.name


def transcribe(audio: np.ndarray) -> str:
    """Transcribe a float32 mono buffer with mlx_whisper. Returns plain text."""
    if not _ensure_mlx_whisper():
        return ""
    wav_path = None
    try:
        wav_path = save_wav(audio)
        kwargs: dict = {"path_or_hf_repo": MODEL, "language": LANG}
        try:
            terms = load_dictionary()
        except Exception:
            terms = []
        if terms:
            # initial_prompt biases decoding toward these spellings — it is the
            # cheap half of Wispr-style context conditioning (names, jargon).
            kwargs["initial_prompt"] = "Glossario: " + ", ".join(terms) + "."
        result = _MLX_WHISPER.transcribe(wav_path, **kwargs)
        text = (result.get("text") or "").strip()
        return text
    except Exception as err:
        sys.stderr.write(f"VibeVoice: transcription failed: {err}\n")
        return ""
    finally:
        if wav_path:
            try:
                os.unlink(wav_path)
            except Exception:
                pass


# ── LLM cleanup pass (optional; degradation is contract) ─────────────────────

_CLEANUP_KEY_WARNED = False


def _load_corrections(max_n: int = 5) -> list:
    """Read the most recent user corrections (newest last); [] on any failure.

    Written by tools/vibevoice_correct.py — the corrections loop. Fed to the
    cleanup prompt as few-shot examples so a mistake, once corrected, stops
    recurring.
    """
    try:
        import json
        pairs = []
        for line in CORRECTIONS_FILE.read_text().splitlines()[-max_n:]:
            try:
                entry = json.loads(line)
                if entry.get("raw") and entry.get("corrected"):
                    pairs.append(entry)
            except Exception:
                pass
        return pairs
    except Exception:
        return []


def _build_cleanup_prompt() -> str:
    """System prompt for the cleanup LLM: literal post-processing, no invention.

    Derived from freeflow's battle-tested prompt; the personal dictionary is
    injected as a glossary so misheard names snap to their canonical spelling.
    """
    prompt = (
        "Sei un post-processore di dettatura vocale. Ricevi l'output grezzo di uno "
        "speech-to-text in italiano e restituisci il testo pulito, pronto da incollare.\n"
        "Compiti:\n"
        "- Rimuovi i filler (ehm, cioè, tipo, diciamo) quando non portano significato.\n"
        "- Correggi ortografia, grammatica e punteggiatura.\n"
        "- Se una parola è una storpiatura evidente di un termine del glossario, "
        "correggila con la grafia del glossario. Non inserire mai termini che il "
        "parlante non ha detto.\n"
        "- Preserva esattamente intento, tono e significato.\n"
        "Regole di output:\n"
        "- Restituisci SOLO il testo pulito, senza preamboli né commenti.\n"
        "- Non aggiungere contenuti assenti dalla trascrizione."
    )
    terms = load_dictionary()
    if terms:
        prompt += "\nGlossario: " + ", ".join(terms) + "."
    pairs = _load_corrections()
    if pairs:
        prompt += "\nEsempi di correzioni fatte dall'utente (grezzo → corretto):"
        for pair in pairs:
            prompt += f'\n- "{pair["raw"]}" → "{pair["corrected"]}"'
    return prompt


def cleanup_text(text: str) -> str:
    """Polish a raw transcription with the configured LLM endpoint.

    Returns the original text unchanged when disabled, unconfigured, on any
    network/parse failure, or when the reply smells hallucinated (empty or
    disproportionately long) — the cleanup is a bonus, never a dependency.
    """
    global _CLEANUP_KEY_WARNED
    if not CLEANUP_ENABLED or not text.strip():
        return text
    if not CLEANUP_API_KEY:
        if not _CLEANUP_KEY_WARNED:
            _CLEANUP_KEY_WARNED = True
            sys.stderr.write(
                "VibeVoice: cleanup enabled but no API key "
                "(VIBEVOICE_CLEANUP_API_KEY or GROQ_API_KEY) — skipping.\n"
            )
        return text
    import json
    import urllib.request
    try:
        payload = {
            "model": CLEANUP_MODEL,
            "temperature": 0,
            "max_tokens": 512,
            "messages": [
                {"role": "system", "content": _build_cleanup_prompt()},
                {"role": "user", "content": text},
            ],
        }
        req = urllib.request.Request(
            CLEANUP_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {CLEANUP_API_KEY}",
            },
        )
        with urllib.request.urlopen(req, timeout=CLEANUP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = (data["choices"][0]["message"]["content"] or "").strip()
        # Anti-hallucination guard: a rewrite much longer than the source is
        # the model adding content, not cleaning it.
        if not out or len(out) > 3 * len(text) + 40:
            return text
        return out
    except Exception as err:
        sys.stderr.write(f"VibeVoice: cleanup failed, using raw text: {err}\n")
        return text


def process_utterance(audio: np.ndarray, t_end: float | None = None) -> str:
    """Transcribe one finalized utterance and publish text + telemetry.

    The measured pipeline (STT now; the LLM cleanup pass hooks in here) —
    kept module-level so it is testable without an Engine/mic. `t_end` is the
    end-of-speech timestamp on the audio loop's clock (time.monotonic); with it
    the metrics line carries total_ms, the end-of-speech → text-ready budget.
    Returns the final text ("" when transcription produced nothing).
    """
    t0 = time.monotonic()
    text = transcribe(audio)
    stt_ms = (time.monotonic() - t0) * 1000.0
    if not text:
        return ""
    cleanup_ms = None
    if CLEANUP_ENABLED:
        t1 = time.monotonic()
        text = cleanup_text(text)
        cleanup_ms = (time.monotonic() - t1) * 1000.0
    write_raw(text)
    _append_history(text)
    entry = {
        "ts": time.time(),
        "audio_s": round(len(audio) / SAMPLE_RATE, 3),
        "chars": len(text),
        "stt_ms": round(stt_ms, 1),
    }
    if cleanup_ms is not None:
        entry["cleanup_ms"] = round(cleanup_ms, 1)
    if t_end is not None:
        entry["total_ms"] = round((time.monotonic() - t_end) * 1000.0, 1)
    _append_metrics(entry)
    return text


# ── Paste into frontmost app (pbcopy + CGEvent Cmd+V) ─────────────────────────

def _press_key_cg(key_code: int, with_command: bool = False) -> bool:
    """Synthesize a key down+up event via Quartz CGEvent. V=9, Return=36.

    CGEvent is posted at the HID tap so it reaches the frontmost app reliably,
    including sandboxed Electron-based editors. Returns False if Quartz is
    unavailable (e.g. PyObjC not installed).
    """
    try:
        from Quartz import (  # type: ignore
            CGEventCreateKeyboardEvent,
            CGEventSetFlags,
            CGEventPost,
            kCGEventFlagMaskCommand,
            kCGHIDEventTap,
        )
        for is_down in (True, False):
            event = CGEventCreateKeyboardEvent(None, key_code, is_down)
            if with_command:
                CGEventSetFlags(event, kCGEventFlagMaskCommand)
            CGEventPost(kCGHIDEventTap, event)
            time.sleep(0.01)
        return True
    except Exception:
        return False


def autosend(text: str) -> None:
    """Copy `text` to the clipboard and paste it into the frontmost app.

    Optionally presses Return afterwards when VIBEVOICE_AUTOSEND_RETURN=1.
    Errors are swallowed so a paste failure never crashes the engine.
    """
    import subprocess

    # 1) Put text on the clipboard.
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=3)
    except Exception as err:
        sys.stderr.write(f"VibeVoice: pbcopy failed, cannot paste: {err}\n")
        return

    # 2) Paste with Cmd+V into whatever app is frontmost (key code V = 9).
    pasted = _press_key_cg(9, with_command=True)
    if not pasted:
        # Fallback for environments without PyObjC/Quartz.
        try:
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to keystroke "v" using command down'],
                timeout=5, capture_output=True,
            )
            pasted = True
        except Exception as err:
            sys.stderr.write(f"VibeVoice: paste failed: {err}\n")

    # 3) Optionally press Return after a short delay (key code Return = 36).
    if pasted and AUTOSEND_RETURN:
        time.sleep(RETURN_DELAY)
        if not _press_key_cg(36, with_command=False):
            try:
                subprocess.run(
                    ["osascript", "-e",
                     'tell application "System Events" to key code 36'],
                    timeout=3, capture_output=True,
                )
            except Exception:
                pass


# ── Capture backends ──────────────────────────────────────────────────────────

class _SounddeviceCapture:
    """Microphone capture via sounddevice (PortAudio). Context manager.

    Feeds float32 mono blocks to `callback` with the sounddevice signature
    (indata, frames, time_info, status). This is the seam behind which the
    voice-processing (AVAudioEngine) backend will slot in; the callback
    contract stays identical across backends.
    """

    name = "sounddevice"

    def __init__(self, callback) -> None:
        self._callback = callback
        self._stream = None

    def __enter__(self) -> "_SounddeviceCapture":
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            blocksize=BLOCKSIZE,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        stream, self._stream = self._stream, None
        if stream is None:
            return False
        return stream.__exit__(exc_type, exc, tb)


# AVFoundation is optional (same lazy pattern as _ensure_mlx_whisper): without
# it the engine still runs on sounddevice, just without Apple echo cancellation.
_AVF = None            # lazily imported AVFoundation module
_AVF_AVAILABLE = None  # tri-state: None=unknown, True/False once checked


def _ensure_avfoundation() -> bool:
    """Import AVFoundation lazily. Returns True if available, else prints help."""
    global _AVF, _AVF_AVAILABLE
    if _AVF_AVAILABLE is not None:
        return _AVF_AVAILABLE
    try:
        import AVFoundation  # type: ignore
        _AVF = AVFoundation
        _AVF_AVAILABLE = True
    except Exception as err:
        _AVF_AVAILABLE = False
        sys.stderr.write(
            "VibeVoice: 'AVFoundation' (PyObjC) is not available — "
            "voice-processing capture disabled, using sounddevice.\n"
            "For Apple echo cancellation (full-duplex) install it with:\n"
            "    pip install pyobjc-framework-AVFoundation\n"
            f"Import error: {err}\n"
        )
    return _AVF_AVAILABLE


class _VoiceProcessingCapture:
    """Microphone capture via AVAudioEngine with Apple voice processing.

    Enabling voice processing on the input node (strictly BEFORE the engine
    starts) turns on the AEC/NS/AGC stack FaceTime uses: the Mac's own speaker
    output is subtracted from the mic signal at the source — the prerequisite
    for full-duplex operation.

    Same contract as _SounddeviceCapture: `callback` receives float32 mono
    blocks of BLOCKSIZE samples at SAMPLE_RATE with the sounddevice signature
    (indata, frames, time_info, status), indata shaped (N, 1).

    Threading: the tap block runs on a CoreAudio thread, so the Python work
    there is strictly copy + enqueue (holding the GIL longer would starve the
    audio HAL). Resampling (AVAudioConverter, native rate → 16 kHz mono) and
    the VAD callback run on a dedicated worker thread.
    """

    name = "voice-processing"

    _TAP_BUFSIZE = 4096   # frames per tap buffer (advisory; CoreAudio may pick its own)
    _QUEUE_MAX = 64       # tap→worker backlog cap; beyond this, blocks are dropped

    def __init__(self, callback) -> None:
        self._callback = callback
        self._queue: queue.Queue = queue.Queue(maxsize=self._QUEUE_MAX)
        self._stopping = threading.Event()
        self._engine = None
        self._node = None
        self._worker: threading.Thread | None = None
        self._converter = None
        self._in_fmt = None    # mono, native VP sample rate (24/48 kHz)
        self._out_fmt = None   # mono, SAMPLE_RATE
        self._in_rate = 0.0

    def __enter__(self) -> "_VoiceProcessingCapture":
        if not _ensure_avfoundation():
            raise RuntimeError("AVFoundation is not importable")
        AVF = _AVF
        self._engine = AVF.AVAudioEngine.alloc().init()
        self._node = self._engine.inputNode()
        # Must happen BEFORE start: enabling VP restructures the input HW path.
        ok, err = self._node.setVoiceProcessingEnabled_error_(True, None)
        if not ok:
            raise RuntimeError(f"setVoiceProcessingEnabled failed: {err}")
        native = self._node.outputFormatForBus_(0)
        rate = float(native.sampleRate())
        if rate <= 0 or int(native.channelCount()) < 1:
            raise RuntimeError(f"unusable voice-processing input format: {native}")
        self._in_rate = rate
        # The converter sees mono: the tap copies only channel 0 (the processed
        # voice channel — VP formats can be multichannel, e.g. 9ch deinterleaved).
        self._in_fmt = AVF.AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
            AVF.AVAudioPCMFormatFloat32, rate, 1, False)
        self._out_fmt = AVF.AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
            AVF.AVAudioPCMFormatFloat32, float(SAMPLE_RATE), 1, False)
        self._converter = AVF.AVAudioConverter.alloc().initFromFormat_toFormat_(
            self._in_fmt, self._out_fmt)
        if self._converter is None:
            raise RuntimeError(f"AVAudioConverter init failed ({rate} Hz -> {SAMPLE_RATE} Hz)")

        q = self._queue

        def _tap(buf, _when) -> None:
            # CoreAudio thread: copy channel 0 + enqueue, nothing else.
            try:
                frames = int(buf.frameLength())
                if frames:
                    q.put_nowait(bytes(buf.floatChannelData()[0].as_buffer(frames)))
            except Exception:
                pass  # full queue or teardown race — drop the block, never raise

        self._node.installTapOnBus_bufferSize_format_block_(0, self._TAP_BUFSIZE, native, _tap)
        try:
            self._engine.prepare()
            ok, err = self._engine.startAndReturnError_(None)
            if not ok:
                raise RuntimeError(f"AVAudioEngine start failed: {err}")
        except Exception:
            self._node.removeTapOnBus_(0)
            raise
        self._worker = threading.Thread(
            target=self._drain, name="vp-capture-drain", daemon=True
        )
        self._worker.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._node is not None:
                self._node.removeTapOnBus_(0)
            if self._engine is not None:
                self._engine.stop()
        except Exception:
            pass
        self._stopping.set()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join(timeout=2.0)
        return False

    # -- worker thread ---------------------------------------------------------

    def _drain(self) -> None:
        """Dequeue native-rate blocks, resample to 16 kHz, regroup to BLOCKSIZE."""
        import objc  # pyobjc-core; guaranteed present when AVFoundation imported
        AVF = _AVF
        pending = np.zeros(0, dtype=np.float32)
        in_buf = None
        in_capacity = 0
        while not self._stopping.is_set():
            try:
                data = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                with objc.autorelease_pool():
                    chunk = np.frombuffer(data, dtype=np.float32)
                    n = int(chunk.size)
                    if n == 0:
                        continue
                    if in_buf is None or n > in_capacity:
                        in_capacity = max(n, self._TAP_BUFSIZE)
                        in_buf = AVF.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
                            self._in_fmt, in_capacity)
                    np.frombuffer(
                        in_buf.floatChannelData()[0].as_buffer(n), dtype=np.float32
                    )[:] = chunk
                    in_buf.setFrameLength_(n)
                    pending = np.concatenate([pending, self._convert(in_buf, n)])
                while pending.size >= BLOCKSIZE:
                    block, pending = pending[:BLOCKSIZE], pending[BLOCKSIZE:]
                    self._callback(block.reshape(-1, 1), BLOCKSIZE, None, None)
            except Exception:
                continue  # one bad block must not kill the capture path

    def _convert(self, in_buf, frames: int) -> np.ndarray:
        """Push one mono native-rate buffer through the converter, pull all output.

        The converter keeps SRC filter state across calls (it is created once per
        session), so a little latency at the start is expected and the sample
        stream stays continuous.
        """
        AVF = _AVF
        out_capacity = int(frames * SAMPLE_RATE / self._in_rate) + 64
        fed = [False]

        def _feed(_num_packets, _out_status):
            # PyObjC bridges the AVAudioConverterInputStatus* out-param as a
            # tuple return: (buffer, status).
            if fed[0]:
                return (None, AVF.AVAudioConverterInputStatus_NoDataNow)
            fed[0] = True
            return (in_buf, AVF.AVAudioConverterInputStatus_HaveData)

        out: list[np.ndarray] = []
        while True:
            out_buf = AVF.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
                self._out_fmt, out_capacity)
            status, _err = self._converter.convertToBuffer_error_withInputFromBlock_(
                out_buf, None, _feed)
            got = int(out_buf.frameLength())
            if got:
                out.append(np.frombuffer(
                    out_buf.floatChannelData()[0].as_buffer(got), dtype=np.float32
                ).copy())  # copy: the array must outlive out_buf
            if status != AVF.AVAudioConverterOutputStatus_HaveData or got == 0:
                break  # InputRanDry (normal) / EndOfStream / Error — done for now
        if not out:
            return np.zeros(0, dtype=np.float32)
        return out[0] if len(out) == 1 else np.concatenate(out)


def _select_capture_backend() -> type:
    """Pick the capture backend class.

    Prefers the macOS voice-processing backend (Apple AEC/NS/AGC — the
    full-duplex prerequisite) unless VIBEVOICE_VP=0 or AVFoundation is not
    importable. Failures later, at open time (mic permission, API errors),
    fall back to sounddevice inside Engine.run().
    """
    if VP_ENABLED and _ensure_avfoundation():
        return _VoiceProcessingCapture
    return _SounddeviceCapture


# ── Speech decider (F2: Silero VAD on a worker thread, RMS fallback) ─────────

# onnxruntime is optional (same lazy tri-state pattern as _ensure_mlx_whisper):
# without it the speech decision degrades to the RMS threshold, exactly as today.
_ORT = None            # lazily imported onnxruntime module
_ORT_AVAILABLE = None  # tri-state: None=unknown, True/False once checked


def _ensure_onnxruntime() -> bool:
    """Import onnxruntime lazily. Returns True if available, else prints help."""
    global _ORT, _ORT_AVAILABLE
    if _ORT_AVAILABLE is not None:
        return _ORT_AVAILABLE
    try:
        import onnxruntime  # type: ignore
        _ORT = onnxruntime
        _ORT_AVAILABLE = True
    except Exception as err:
        _ORT_AVAILABLE = False
        sys.stderr.write(
            "VibeVoice: 'onnxruntime' is not available — Silero VAD disabled, "
            "speech detection stays on the RMS threshold.\n"
            "For neural speech/non-speech decisions install it with:\n"
            "    pip install onnxruntime silero-vad\n"
            f"Import error: {err}\n"
        )
    return _ORT_AVAILABLE


def _resolve_silero_model() -> str | None:
    """Locate the Silero VAD ONNX model, in cascade:

    1. VIBEVOICE_SILERO_MODEL (explicit path; if it doesn't exist, warn and
       keep cascading rather than silently running a different model),
    2. the `silero_vad` package data — found via find_spec WITHOUT importing
       the package (importing it would drag in torch just to read a path),
    3. None → the decider stays on the RMS-threshold fallback.
    """
    env = os.environ.get("VIBEVOICE_SILERO_MODEL", "").strip()
    if env:
        if Path(env).exists():
            return env
        sys.stderr.write(
            f"VibeVoice: VIBEVOICE_SILERO_MODEL='{env}' does not exist — "
            "trying the silero_vad package model.\n"
        )
    try:
        import importlib.util
        spec = importlib.util.find_spec("silero_vad")
        if spec is not None and spec.origin:
            candidate = Path(spec.origin).parent / "data" / "silero_vad.onnx"
            if candidate.exists():
                return str(candidate)
    except Exception:
        pass
    return None


def _create_silero_session(model_path: str):
    """Build the onnxruntime session (the seam tests replace with a fake).

    Single-threaded CPU on purpose: one 512-sample frame every 32 ms is tiny
    work, and the decider already runs on its own worker thread.
    """
    opts = _ORT.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = 1
    opts.log_severity_level = 3  # errors only — no ORT chatter on the engine's stderr
    return _ORT.InferenceSession(
        model_path, sess_options=opts, providers=["CPUExecutionProvider"]
    )


class SileroVad:
    """Speech/non-speech decider backed by the Silero VAD ONNX model.

    The audio callback talks to it through two non-blocking calls only:
    `submit(block, rms)` (bounded put_nowait + a synchronous RMS decision) and
    `is_speech()` (a single attribute read — atomic under the GIL). Inference
    never runs on the audio thread: a dedicated worker re-chunks the ~100 ms
    capture blocks into the model's 512-sample frames (with carry across
    blocks), threads the recurrent state through consecutive calls, and
    publishes the decision with onset/offset hysteresis (SILERO_ONSET /
    SILERO_OFFSET) so borderline frames don't make the flag flap.

    Degradation contract: when onnxruntime or the model is unavailable — or a
    session/inference error occurs mid-stream — is_speech() returns
    (rms >= VAD_THRESHOLD), bit-identical to the legacy energy VAD.
    """

    def __init__(self) -> None:
        self._session = None
        self._active = False       # True only while the ONNX path is live
        self._speech = False       # model decision, published by the worker
        self._rms_speech = False   # threshold decision, updated in submit()
        self._queue: queue.Queue = queue.Queue(maxsize=SILERO_QUEUE_MAX)
        self._stopping = threading.Event()
        self._worker: threading.Thread | None = None
        # Worker-thread-only state: model recurrence + re-chunk carry.
        self._state: np.ndarray | None = None
        self._context: np.ndarray | None = None
        self._carry = np.zeros(0, dtype=np.float32)
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)

    def start(self) -> "SileroVad":
        """Resolve the model and spawn the worker. On any failure the decider
        stays fully usable on the RMS fallback — never raises."""
        model = _resolve_silero_model()
        if model is None or not _ensure_onnxruntime():
            return self
        try:
            self._session = _create_silero_session(model)
        except Exception as err:
            sys.stderr.write(
                f"VibeVoice: Silero VAD session failed ({err}); "
                "speech detection stays on the RMS threshold.\n"
            )
            return self
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(SILERO_CONTEXT, dtype=np.float32)
        self._active = True
        self._worker = threading.Thread(target=self._run, name="silero-vad", daemon=True)
        self._worker.start()
        sys.stderr.write(f"VibeVoice: VAD: silero ({model})\n")
        return self

    def stop(self) -> None:
        """Stop the worker thread (idempotent). Fallback reads keep working."""
        self._active = False
        self._stopping.set()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join(timeout=2.0)

    # -- audio-callback side (non-blocking, never raises) ----------------------

    def submit(self, block: np.ndarray, rms: float) -> None:
        """Feed one capture block. Never blocks, never raises (callback path)."""
        try:
            self._rms_speech = rms >= VAD_THRESHOLD
            if not self._active:
                return
            # Copy: the queue must own its samples — the callback's buffer is reused.
            self._queue.put_nowait(np.array(block, dtype=np.float32, copy=True).reshape(-1))
        except Exception:
            pass  # full queue (drop the block) or teardown race — never raise

    def is_speech(self) -> bool:
        """Current decision. A single attribute read — safe on the audio thread."""
        return self._speech if self._active else self._rms_speech

    # -- worker thread ----------------------------------------------------------

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                block = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._ingest(block)
            except Exception as err:
                # A broken session must degrade, not freeze a stale decision:
                # flip to the RMS fallback and stop consuming.
                self._active = False
                sys.stderr.write(
                    f"VibeVoice: Silero VAD inference failed ({err}); "
                    "falling back to the RMS threshold.\n"
                )
                return

    def _ingest(self, block: np.ndarray) -> None:
        """Re-chunk arbitrary block sizes into SILERO_FRAME frames, with carry."""
        buf = np.concatenate([self._carry, block]) if self._carry.size else block
        end = (buf.size // SILERO_FRAME) * SILERO_FRAME
        for off in range(0, end, SILERO_FRAME):
            self._infer(buf[off:off + SILERO_FRAME])
        self._carry = buf[end:]

    def _infer(self, frame: np.ndarray) -> None:
        """One model step (v5 ONNX contract: 64-sample context + 512-sample
        frame, recurrent state (2,1,128), sr int64) + hysteresis on the prob."""
        x = np.concatenate([self._context, frame]).reshape(1, -1).astype(np.float32, copy=False)
        outs = self._session.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._state = np.asarray(outs[1], dtype=np.float32)
        self._context = x[0, -SILERO_CONTEXT:]
        prob = float(np.asarray(outs[0]).reshape(-1)[0])
        if prob >= SILERO_ONSET:
            self._speech = True
        elif prob <= SILERO_OFFSET:
            self._speech = False
        # between OFFSET and ONSET: hold the previous decision (hysteresis)


# ── Audio engine ─────────────────────────────────────────────────────────────

class Engine:
    """Speech-gated microphone capture + transcription state machine.

    Lifecycle: idle -> recording -> transcribing -> idle. The engine writes the
    state-file contract on every transition and streams RMS levels at ~LEVELS_HZ
    while recording. The speech/non-speech decision comes from the SileroVad
    decider (RMS-threshold fallback when the model is unavailable); the RMS
    keeps feeding levels.bin regardless of who decides.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._busy = threading.Semaphore(2)  # up to two transcriptions in flight — keeps the tail of a long utterance from being dropped while the previous blob is still transcribing

        # Speech decider (F2). Constructed unstarted: until run() calls
        # start(), is_speech() is exactly the legacy RMS-threshold decision.
        self._vad = SileroVad()

        # Mute edge-detector: write "idle" once when entering mute, not per block.
        self._was_muted = False

        # VAD / capture state (guarded by _lock).
        self._speaking = False
        self._buf: list[np.ndarray] = []
        self._pre = deque(maxlen=PRE_ROLL_BLOCKS)
        self._t_start = 0.0
        self._t_silence: float | None = None

        # RMS history for levels.bin (thread-safe deque).
        self._rms_history: deque = deque([0.0] * LEVELS_LEN, maxlen=LEVELS_LEN)

        # Throttle levels.bin writes to ~LEVELS_HZ.
        blocks_per_sec = SAMPLE_RATE / BLOCKSIZE
        self._levels_every = max(1, int(round(blocks_per_sec / LEVELS_HZ)))
        self._levels_tick = 0

    # -- public API ----------------------------------------------------------

    def run(self) -> None:
        """Open the microphone and run the capture loop until stopped."""
        write_state("idle")
        write_levels(self._rms_history)
        # Bring the speech decider up for the whole capture session. start()
        # never raises: without the model/onnxruntime it stays on the RMS
        # fallback, and stop() in the finally is idempotent either way.
        self._vad.start()
        try:
            backend_cls = _select_capture_backend()
            if backend_cls is not _SounddeviceCapture:
                # Voice-processing (or an injected backend) first. Whatever fails
                # at open time — mic permission, VP API errors — degrades to
                # sounddevice below instead of killing the engine.
                try:
                    self._capture_loop(backend_cls)
                    return
                except Exception as err:
                    sys.stderr.write(
                        f"VibeVoice: capture '{backend_cls.name}' failed ({err}); "
                        "falling back to sounddevice.\n"
                    )
            try:
                self._capture_loop(_SounddeviceCapture)
            except Exception as err:
                sys.stderr.write(
                    "VibeVoice: could not open the microphone.\n"
                    "Grant microphone access in System Settings > Privacy & Security "
                    "> Microphone, then retry.\n"
                    f"Audio error: {err}\n"
                )
        finally:
            # Single exit point: every path out of run() (clean stop, capture
            # failure, propagated error) leaves the state file at "idle".
            write_state("idle")
            self._vad.stop()

    def _capture_loop(self, backend_cls: type) -> None:
        """Open `backend_cls` and block until stop() — the callback drives all work."""
        sys.stderr.write(f"VibeVoice: capture: {backend_cls.name}\n")
        with backend_cls(self._audio_callback):
            while not self._stop.is_set():
                self._stop.wait(0.25)

    def stop(self) -> None:
        """Signal the capture loop to exit."""
        self._stop.set()

    # -- audio callback ------------------------------------------------------

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        """Called by sounddevice for each audio block. Runs the VAD."""
        try:
            # Mute gate (a deliberate pause from the pill). Checked outside the
            # lock, before any work: ignore the mic but keep the engine alive.
            # Cheap stat per block; the whole callback already swallows errors.
            if is_muted():
                if not self._was_muted:
                    self._was_muted = True
                    with self._lock:
                        # Drop any in-flight utterance cleanly.
                        self._speaking = False
                        self._t_silence = None
                        self._buf = []
                    write_state("idle")
                return
            self._was_muted = False

            block = np.ascontiguousarray(indata[:, 0], dtype=np.float32)
            rms = float(np.sqrt(np.mean(block ** 2))) if block.size else 0.0
            now = time.monotonic()
            # The speech DECISION comes from the decider: feed + flag read are
            # non-blocking, never raise, and stay OUTSIDE the lock (the callback
            # must never block). The RMS keeps feeding _rms_history/levels.bin
            # below exactly as before — only the decision changed.
            self._vad.submit(block, rms)
            speech = self._vad.is_speech()
            do_finalize = False

            with self._lock:
                # Update level history; emit levels.bin while recording.
                self._rms_history.append(min(rms, 1.0))
                self._levels_tick += 1
                if self._speaking and self._levels_tick >= self._levels_every:
                    self._levels_tick = 0
                    write_levels(self._rms_history)

                if speech:
                    # Speech present (decider's verdict).
                    if not self._speaking:
                        # Onset: start a new utterance, include the pre-roll.
                        self._speaking = True
                        self._t_start = now
                        self._buf = list(self._pre)
                        # Defer the state write until outside the lock.
                        do_finalize = "start"  # type: ignore[assignment]
                    self._t_silence = None
                    self._buf.append(block)
                    if now - self._t_start >= MAX_DUR:
                        do_finalize = "finalize"  # type: ignore[assignment]
                else:
                    # Silence.
                    self._pre.append(block)
                    if self._speaking:
                        self._buf.append(block)
                        if self._t_silence is None:
                            self._t_silence = now
                        elif now - self._t_silence >= SILENCE_SEC:
                            do_finalize = "finalize"  # type: ignore[assignment]

            # File I/O and thread spawning happen outside the lock.
            if do_finalize == "start":  # type: ignore[comparison-overlap]
                write_state("recording")
                write_levels(self._rms_history)
            elif do_finalize == "finalize":  # type: ignore[comparison-overlap]
                self._finalize(now)
        except Exception:
            # The audio callback must never raise.
            pass

    # -- finalize & transcribe ----------------------------------------------

    def _finalize(self, now: float) -> None:
        """Close the current utterance and hand it to a transcription thread."""
        with self._lock:
            dur = now - self._t_start
            audio = (
                np.concatenate(self._buf)
                if self._buf
                else np.zeros(1, dtype=np.float32)
            )
            self._speaking = False
            self._t_silence = None
            self._buf = []

        # Too short to be real speech — drop it silently.
        if dur < MIN_DUR:
            write_state("idle")
            write_levels(self._rms_history)
            return

        # Up to two transcriptions in flight; if both slots are busy, drop this utterance.
        if self._busy.acquire(blocking=False):
            threading.Thread(
                target=self._transcribe_worker, args=(audio, now), daemon=True
            ).start()
        else:
            write_state("idle")

    def _transcribe_worker(self, audio: np.ndarray, t_end: float = 0.0) -> None:
        """Transcribe, publish to raw.txt, and optionally autosend."""
        try:
            write_state("transcribing")
            text = process_utterance(audio, t_end=t_end or None)
            if text and AUTOSEND:
                # Paste off the worker thread so we return to idle promptly.
                threading.Thread(
                    target=autosend, args=(text,), daemon=True
                ).start()
        finally:
            write_state("idle")
            write_levels(self._rms_history)
            self._busy.release()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    # Warn early (but do not exit) if transcription is unavailable, so the user
    # gets actionable instructions even before speaking.
    _ensure_mlx_whisper()

    engine = Engine()

    def _shutdown(*_args) -> None:
        engine.stop()

    # Clean shutdown on Ctrl-C / SIGTERM.
    try:
        import signal
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except Exception:
        pass

    try:
        engine.run()
    except KeyboardInterrupt:
        engine.stop()
    finally:
        write_state("idle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
