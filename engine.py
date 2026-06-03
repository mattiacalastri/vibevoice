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
# Captures the microphone, detects speech with an energy-based VAD, transcribes
# with mlx_whisper (Apple Silicon), then optionally pastes the result into the
# frontmost application.
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
#
# Environment variables:
#   VIBEVOICE_LANG            transcription language code (default: "it")
#   VIBEVOICE_MODEL           mlx_whisper model (default: mlx-community/whisper-turbo)
#   VIBEVOICE_AUTOSEND        "1" to paste into frontmost app (default: "1")
#   VIBEVOICE_AUTOSEND_RETURN "1" to press Return after pasting (default: "0")
# ---------------------------------------------------------------------------

import os
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


# ── Configuration ─────────────────────────────────────────────────────────────
LANG = os.environ.get("VIBEVOICE_LANG", "it")
MODEL = os.environ.get("VIBEVOICE_MODEL", "mlx-community/whisper-turbo")
AUTOSEND = os.environ.get("VIBEVOICE_AUTOSEND", "1") == "1"
AUTOSEND_RETURN = os.environ.get("VIBEVOICE_AUTOSEND_RETURN", "0") == "1"

SAMPLE_RATE = 16000     # mlx_whisper expects 16 kHz mono
CHANNELS = 1
BLOCKSIZE = 1600        # ~100 ms per audio block at 16 kHz

LEVELS_LEN = 60         # number of float32 RMS samples in levels.bin
LEVELS_HZ = 10          # target write frequency for levels.bin (Hz)

VAD_THRESHOLD = 0.015   # RMS above this starts/sustains "recording"
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
        result = _MLX_WHISPER.transcribe(
            wav_path,
            path_or_hf_repo=MODEL,
            language=LANG,
        )
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


# ── Audio engine ─────────────────────────────────────────────────────────────

class Engine:
    """Energy-VAD microphone capture + transcription state machine.

    Lifecycle: idle -> recording -> transcribing -> idle. The engine writes the
    state-file contract on every transition and streams RMS levels at ~LEVELS_HZ
    while recording.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._busy = threading.Semaphore(2)  # up to two transcriptions in flight — keeps the tail of a long utterance from being dropped while the previous blob is still transcribing

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
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                blocksize=BLOCKSIZE,
                dtype="float32",
                callback=self._audio_callback,
            ):
                # Block here; the callback drives all the work.
                while not self._stop.is_set():
                    self._stop.wait(0.25)
        except Exception as err:
            sys.stderr.write(
                "VibeVoice: could not open the microphone.\n"
                "Grant microphone access in System Settings > Privacy & Security "
                "> Microphone, then retry.\n"
                f"Audio error: {err}\n"
            )
            write_state("idle")
            return
        write_state("idle")

    def stop(self) -> None:
        """Signal the capture loop to exit."""
        self._stop.set()

    # -- audio callback ------------------------------------------------------

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        """Called by sounddevice for each audio block. Runs the VAD."""
        try:
            block = np.ascontiguousarray(indata[:, 0], dtype=np.float32)
            rms = float(np.sqrt(np.mean(block ** 2))) if block.size else 0.0
            now = time.monotonic()
            do_finalize = False

            with self._lock:
                # Update level history; emit levels.bin while recording.
                self._rms_history.append(min(rms, 1.0))
                self._levels_tick += 1
                if self._speaking and self._levels_tick >= self._levels_every:
                    self._levels_tick = 0
                    write_levels(self._rms_history)

                if rms >= VAD_THRESHOLD:
                    # Speech present.
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
                target=self._transcribe_worker, args=(audio,), daemon=True
            ).start()
        else:
            write_state("idle")

    def _transcribe_worker(self, audio: np.ndarray) -> None:
        """Transcribe, publish to raw.txt, and optionally autosend."""
        try:
            write_state("transcribing")
            text = transcribe(audio)
            if text:
                write_raw(text)
                if AUTOSEND:
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
