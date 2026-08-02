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
#   ~/.vibevoice/metrics.jsonl  per-utterance latency telemetry, JSONL, capped
#                            (report: tools/vibevoice_metrics.py)
#
# CONTROL FILES (written by the pill / external tools, read by the engine — the
# same external-control pattern as autosend's pause flag, NOT engine-owned state):
#   ~/.vibevoice/muted       presence = mic paused: the engine stays alive but
#                            ignores the microphone (no recording/transcription)
#   ~/.vibevoice/dictionary.txt   personal terms, one per line — biases Whisper
#                            via initial_prompt and the cleanup glossary
#   ~/.vibevoice/corrections.jsonl  user corrections {ts,raw,corrected} — few-shot
#                            examples for the cleanup prompt (tools/vibevoice_correct.py)
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
#   VIBEVOICE_CLEANUP         "1" enables the LLM cleanup pass after
#                             transcription (default: "0"; any failure falls
#                             back to the raw text)
#   VIBEVOICE_CLEANUP_URL     OpenAI-compatible endpoint (default: Groq)
#   VIBEVOICE_CLEANUP_MODEL   model id for the cleanup pass
#   VIBEVOICE_CLEANUP_TIMEOUT seconds before the cleanup call is abandoned
#   VIBEVOICE_CLEANUP_API_KEY bearer key (falls back to GROQ_API_KEY)
# ---------------------------------------------------------------------------

import os
import queue
import re
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
PARTIAL_FILE = STATE_DIR / "partial.txt"  # live draft while speaking; absent = nothing in flight
PARTIAL_TMP = STATE_DIR / "partial.tmp"   # staging for atomic replace
# autosend.py's pause hook (that daemon owns the semantics; we only raise/lower it).
AUTOSEND_PAUSE_FLAG = Path("/tmp/vibevoice_autosend_pause")


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
# Key file fallback: LaunchAgent plists cannot read the shell env, and a secret
# does not belong inside a plist — drop the key in this file (chmod 600) instead.
CLEANUP_KEY_FILE = STATE_DIR / "cleanup_key"

SAMPLE_RATE = 16000     # mlx_whisper expects 16 kHz mono
CHANNELS = 1
BLOCKSIZE = 800         # 50 ms per audio block at 16 kHz. Also the waveform's
                        # data rate: the pill redraws at 24 fps but can only
                        # MOVE when a new RMS sample lands, so at the old 100 ms
                        # block each bar was held for 2.4 frames and the scroll
                        # visibly stepped. Silero re-chunks to its own 512-sample
                        # frames with carry, so it is indifferent to this.

LEVELS_LEN = 60         # number of float32 RMS samples in levels.bin
LEVELS_HZ = 20          # target write frequency for levels.bin (Hz) — must not
                        # throttle below the block rate or the extra samples the
                        # smaller BLOCKSIZE buys are thrown away before the pill
                        # ever sees them

HISTORY_MAX = 20        # max lines in history.jsonl

DICT_MAX_TERMS = 64     # terms fed to Whisper's initial_prompt (its context window is ~224 tokens)
METRICS_MAX = 500       # max lines in metrics.jsonl

VAD_THRESHOLD = 0.015   # RMS above this starts/sustains "recording"
SILERO_ONSET = 0.5      # speech probability that turns the decision ON (hysteresis high)
SILERO_OFFSET = 0.35    # probability that turns it OFF (hysteresis low; in between = hold)
SILERO_FRAME = 512      # samples per Silero inference frame at 16 kHz (model contract)
SILERO_CONTEXT = 64     # context samples prepended to each frame (v5 ONNX contract)
SILERO_QUEUE_MAX = 32   # submit()→worker backlog cap; beyond this, blocks are dropped
SILENCE_SEC = float(os.environ.get("VIBEVOICE_SILENCE", "1.5"))  # trailing silence that ends an utterance (lower = faster paste, but a mid-sentence pause splits the utterance)
# Streaming (F3): re-decode the open utterance while it is still being spoken so
# text exists BEFORE the trailing silence expires. Without it the first word can
# only appear SILENCE_SEC after you stop talking — that wait is the architecture,
# not a tunable. The partial pass is a bonus: any failure leaves the final,
# authoritative transcription untouched.
STREAMING = os.environ.get("VIBEVOICE_STREAMING", "1") == "1"
PARTIAL_INTERVAL = float(os.environ.get("VIBEVOICE_PARTIAL_INTERVAL", "0.6"))  # min seconds between partial passes
# Streaming paste: type each confirmed chunk into the frontmost app as it is
# confirmed, instead of pasting the whole sentence after the trailing silence.
# Safe only because the confirmed prefix never retracts. `0` restores the single
# atomic paste at finalize.
STREAM_PASTE = os.environ.get("VIBEVOICE_STREAM_PASTE", "1") == "1"
MIN_DUR = 0.4           # discard utterances shorter than this (seconds)
MAX_DUR = 15.0          # force finalize after this many seconds (short enough to keep each blob within the recognizer's comfort window + sustain rhythm on long dictation)
PRE_ROLL_BLOCKS = 10    # blocks kept before speech onset — a DURATION (0.5s at
                        # BLOCKSIZE=800), and it is what saves the first syllable

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


def write_partial(text: str) -> None:
    """Publish the live draft of the utterance being spoken (plain text).

    Atomic like levels.bin: the pill reads this on a timer and must never catch
    half a sentence. Provisional by definition — raw.txt stays the authority.
    """
    try:
        PARTIAL_TMP.write_text(text)
        os.replace(PARTIAL_TMP, PARTIAL_FILE)
    except Exception:
        pass


def clear_partial() -> None:
    """Remove the live draft. Absence of the file IS the 'nothing in flight' signal."""
    try:
        PARTIAL_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _patch_last_metrics(fields: dict) -> None:
    """Merge `fields` into the newest metrics line.

    Some numbers are only known after `process_utterance` has already written
    its line — the tail is computed against the authoritative text. Rewriting
    the last line keeps one row per utterance instead of two half-rows that
    every reader would then have to join.
    """
    try:
        import json
        lines = METRICS_FILE.read_text().splitlines()
        if not lines:
            return
        entry = json.loads(lines[-1])
        entry.update(fields)
        lines[-1] = json.dumps(entry)
        METRICS_FILE.write_text("\n".join(lines) + "\n")
    except Exception:
        pass


def hold_autosend() -> None:
    """Tell the standalone auto-Return daemon that a sentence is in progress.

    `autosend.py` fires Return after 0.8s of typing silence. The streaming paste
    types in bursts ~PARTIAL_INTERVAL apart — close enough that one slow pass
    would read as "the user stopped" and send the message mid-sentence. Raising
    the flag on every chunk also refreshes its timestamp, so the daemon's 60s
    anti-deadlock TTL can never expire while we are still typing.
    """
    try:
        AUTOSEND_PAUSE_FLAG.write_text(str(time.time()))
    except Exception:
        pass


def release_autosend() -> None:
    """The sentence is whole: let the Return fire."""
    try:
        AUTOSEND_PAUSE_FLAG.unlink()
    except FileNotFoundError:
        pass
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
    """Transcribe a float32 mono buffer with mlx_whisper. Returns plain text.

    The buffer goes to mlx_whisper AS AN ARRAY: file paths are decoded through
    ffmpeg, which is not on the launchd PATH — under a LaunchAgent every
    dictation died with "No such file or directory: 'ffmpeg'" while the same
    engine worked from a terminal. The array path needs no decoder at all
    (and skips the temp-WAV roundtrip).
    """
    if not _ensure_mlx_whisper():
        return ""
    try:
        kwargs: dict = {"path_or_hf_repo": MODEL, "language": LANG}
        try:
            terms = load_dictionary()
        except Exception:
            terms = []
        if terms:
            # initial_prompt biases decoding toward these spellings — it is the
            # cheap half of Wispr-style context conditioning (names, jargon).
            kwargs["initial_prompt"] = "Glossario: " + ", ".join(terms) + "."
        result = _MLX_WHISPER.transcribe(audio, **kwargs)
        text = (result.get("text") or "").strip()
        return text
    except Exception as err:
        sys.stderr.write(f"VibeVoice: transcription failed: {err}\n")
        return ""


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
    key = CLEANUP_API_KEY
    if not key:
        try:
            key = CLEANUP_KEY_FILE.read_text().strip()
        except Exception:
            key = ""
    if not key:
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
                "Authorization": f"Bearer {key}",
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


# ── LocalAgreement-2: turning unstable hypotheses into stable text ───────────

_AGREE_STRIP = re.compile(r"[^\w']+", re.UNICODE)
# Whisper emits the typographic apostrophe for Italian elisions as readily as
# the ASCII one, often flipping between passes. Treating them as different
# characters broke agreement exactly at l', dell', quest', un' — the commonest
# words in fluent Italian — so the live draft stalled where it should flow.
_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "＇": "'"})


def _agreement_key(word: str) -> str:
    """Comparison key for one word: one apostrophe, no punctuation, case-folded.

    Whisper re-punctuates and re-capitalizes its own output as the buffer grows
    ("Ciao mondo" → "ciao, mondo come stai"). Comparing glyphs would find almost
    no agreement; comparing words finds the real one.
    """
    return _AGREE_STRIP.sub("", word.translate(_APOSTROPHES)).casefold()


def _locate(needle: list[str], haystack: list[str]) -> int | None:
    """Index where `needle` sits inside `haystack`, comparing by word key.

    Empty needle is at 0 by definition. Returns None when it is not there at
    all — the caller must then refuse to guess a position.
    """
    if not needle:
        return 0
    keys = [_agreement_key(w) for w in haystack]
    target = [_agreement_key(w) for w in needle]
    for start in range(len(keys) - len(target) + 1):
        if keys[start:start + len(target)] == target:
            return start
    return None


class LocalAgreement:
    """Stabilizer for streaming ASR (LocalAgreement-2, Liu et al. / whisper-streaming).

    A word is published only once **two successive hypotheses agree** on it. Each
    partial pass re-decodes the whole open utterance, so Whisper is free to rewrite
    its tail — the tail is never trustworthy, the agreed prefix is. This is what
    buys "text appears as you speak" without the flicker of raw partials.

    Pure and synchronous on purpose: no threads, no I/O, no model — testable
    without a microphone.
    """

    def __init__(self) -> None:
        self._committed: list[str] = []   # surface forms, first-seen spelling wins
        self._prev: list[str] = []        # previous hypothesis, for the agreement

    def update(self, hypothesis: str) -> str:
        """Feed a fresh full-utterance hypothesis; return the newly committed words.

        Returns "" when this pass confirms nothing new. The **word sequence** never
        retracts: a shorter or divergent hypothesis leaves published words alone
        (un-saying a word the user has read is worse than a late one).

        The **spelling** of a published word, however, is refreshed from the newest
        hypothesis while its identity holds. A word confirmed at the truncation edge
        carries whatever punctuation closed that buffer — measured live 2026-08-02,
        the draft read "modo autonomo. mentre il cervello" where the sentence really
        had a comma. More context means better punctuation, so the draft converges on
        what the final text will say instead of freezing an artefact of truncation.
        """
        words = hypothesis.split()
        prev = self._prev
        self._prev = words

        # Where does what we already published sit in each hypothesis? Whisper
        # re-decodes the whole buffer, so it can revise the START too — "ciao
        # amico mio" became "oh ciao amico mio". Tracking the committed words by
        # POSITION silently misaligned every later commit and published a
        # duplicated word (review 2026-08-02). So we re-locate ourselves each
        # pass instead of assuming we have not moved.
        here = _locate(self._committed, words)
        there = _locate(self._committed, prev)
        if here is None or there is None:
            # We can no longer find what we published. Publishing anything now
            # would be a guess at a position — say nothing.
            return ""

        n = len(self._committed)
        # Refresh the glyphs of published words (identity holds by construction
        # of _locate): more context means better punctuation.
        self._committed = words[here:here + n]

        # Agreement resumes AFTER the published part, in both hypotheses.
        fresh: list[str] = []
        for a, b in zip(words[here + n:], prev[there + n:]):
            if _agreement_key(a) != _agreement_key(b):
                break
            fresh.append(a)
        if not fresh:
            return ""
        self._committed = self._committed + fresh
        return " ".join(fresh)

    @property
    def confirmed(self) -> str:
        """Everything published so far for this utterance."""
        return " ".join(self._committed)

    def reset(self) -> None:
        """Start a new utterance. Utterance N's prefix must not leak into N+1."""
        self._committed = []
        self._prev = []


PHANTOM_MAX_WORDS = 5   # above this a quiet blob is treated as real (quiet) speech
# Well BELOW the RMS gate on purpose. The guard re-judges speech-vs-silence with
# the crude threshold even when the neural VAD — more sensitive by design, which
# is why it exists — is what opened the utterance. At the plain threshold a soft
# "sì" was transcribed correctly and then deleted with no trace (review
# 2026-08-02). Only near-total silence should qualify.
PHANTOM_RMS_FACTOR = 0.4


def _is_phantom(audio: np.ndarray, text: str) -> bool:
    """True when the audio was silent and the text is short — a hallucination.

    Whisper does not answer "" to silence; it answers with training-set filler.
    Found in the live history 2026-08-02 11:47:12: an utterance reading
    "Grazie a tutti." that was never spoken, and which the paste would have
    typed into whatever the user had open.

    Both conditions are required. Audio alone would censor genuinely quiet
    dictation; a phrase list alone would delete the same words when actually
    spoken. Loudness is read at the 90th percentile, not the mean: one quiet
    word inside a real sentence must not condemn it.
    """
    if len(text.split()) > PHANTOM_MAX_WORDS:
        return False
    try:
        if audio.size < 2:
            return False
        # Per-block RMS, then p90 — the loudest tenth is what says "someone spoke".
        n = max(1, audio.size // 160)         # ~10 ms blocks
        blocks = np.array_split(audio, n)
        rms = np.array([float(np.sqrt(np.mean(b.astype(np.float64) ** 2))) for b in blocks])
        return bool(np.percentile(rms, 90) < VAD_THRESHOLD * PHANTOM_RMS_FACTOR)
    except Exception:
        return False


def process_utterance(audio: np.ndarray, t_end: float | None = None,
                      extra: dict | None = None) -> str:
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
    if text and _is_phantom(audio, text):
        # A silent deletion nobody can see is worse than the phantom it
        # prevents: leave a countable trace so a guard that starts eating real
        # speech shows up in the ledger instead of in the user's confusion.
        _append_metrics({
            "ts": time.time(),
            "audio_s": round(len(audio) / SAMPLE_RATE, 3),
            "chars": 0,
            "stt_ms": round(stt_ms, 1),
            "phantom": True,
            "dropped": text[:60],
        })
        return ""
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
    if extra:
        # The engine owns the streaming numbers; this function owns the ledger.
        entry.update(extra)
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


_TYPE_CHUNK = 16   # CGEventKeyboardSetUnicodeString is unreliable past ~20 UTF-16 units


def type_text(text: str) -> bool:
    """Type `text` into the frontmost app as synthetic keystrokes.

    Used by the streaming paste, which fires many times per sentence: going
    through the clipboard would thrash the user's pasteboard a dozen times a
    dictation (sharp edge #2 says we already overwrite it once — doing it 15
    times is a different animal). `CGEventKeyboardSetUnicodeString` posts the
    characters directly, at the same HID tap as the Cmd+V path, so it reaches
    sandboxed Electron editors too. Returns False if Quartz is unavailable.
    """
    if not text:
        return True
    try:
        from Quartz import (  # type: ignore
            CGEventCreateKeyboardEvent,
            CGEventKeyboardSetUnicodeString,
            CGEventPost,
            CGEventSetFlags,
            kCGHIDEventTap,
        )
        for off in range(0, len(text), _TYPE_CHUNK):
            chunk = text[off:off + _TYPE_CHUNK]
            for is_down in (True, False):
                event = CGEventCreateKeyboardEvent(None, 0, is_down)
                # Virtual keycode 0 is the letter A. Electron-based editors read
                # the KEYCODE, not the unicode payload, when a modifier is set —
                # and a synthesized event inherits the current flag state. Left
                # alone, a stray Command turns every chunk into Cmd+A: measured
                # in the wild 2026-08-02, the whole editor went blue mid-sentence
                # and the next chunk would have replaced the selection. Clearing
                # the flags is what keeps this a keystroke instead of a shortcut.
                CGEventSetFlags(event, 0)
                CGEventKeyboardSetUnicodeString(event, len(chunk), chunk)
                CGEventPost(kCGHIDEventTap, event)
                time.sleep(0.002)
        return True
    except Exception as err:
        sys.stderr.write(f"VibeVoice: typing failed: {err}\n")
        return False


_ANCHOR_LEN = 3   # words of context that make a match trustworthy


def unstreamed_tail(streamed: str, final: str) -> str:
    """The part of `final` the streaming paste has NOT already typed.

    Anchors on the **end** of what was typed, not on its beginning. What is
    already on screen ends somewhere inside the authoritative text, and that
    junction is what we need; the start is the part the final decode is most
    likely to have revised.

    Prefix alignment was the first attempt and it failed in the wild
    (2026-08-02): one corrected word near the beginning dropped the alignment to
    zero and the whole sentence was pasted a second time. The user's screen read
    "…che pattern e risolvili tranquillamente Ti dico i ragionamenti che fai…".

    Comparison is punctuation- and case-insensitive: the stream typed "autonomo"
    where the final says "autonomo," — the same word, and the missing comma
    costs less than the repetition.

    When no anchor is found anywhere we return "" — we cannot tell what is
    already on screen, and a sentence printed twice makes the text unusable
    while a missing tail is visible and re-dictatable.
    """
    if not streamed:
        return final
    typed = [_agreement_key(w) for w in streamed.split()]
    typed = [w for w in typed if w]
    final_words = final.split()
    keys = [_agreement_key(w) for w in final_words]
    if not typed:
        return final

    # Try the longest anchor first, shrinking to a single word: the more context
    # a match has, the more certain the junction.
    for size in range(min(_ANCHOR_LEN, len(typed)), 0, -1):
        anchor = typed[-size:]
        matches = [start for start in range(len(keys) - size + 1)
                   if keys[start:start + size] == anchor]
        if not matches:
            continue
        # Among equal matches, take the one nearest to where the stream actually
        # stopped. Always taking the LAST one deleted whatever lay between two
        # occurrences — and Italian repeats short phrases when thinking aloud
        # ("non lo so, non lo so bene"), so the deletion was silent and common
        # (review 2026-08-02).
        expected = len(typed) - size
        best = min(matches, key=lambda start: (abs(start - expected), start))
        return " ".join(final_words[best + size:])
    return ""


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

        # Paste sequencing (long-dictation quality): transcriptions may finish
        # out of order, pastes must not. Each finalized utterance takes a
        # sequence number; the paste step waits (bounded) for its turn.
        self._paste_cv = threading.Condition()
        self._seq_next = 0    # next sequence number to assign at finalize
        self._paste_next = 0  # next sequence allowed to paste

        # Speech decider (F2). Constructed unstarted: until run() calls
        # start(), is_speech() is exactly the legacy RMS-threshold decision.
        self._vad = SileroVad()

        # Streaming (F3). One partial pass at a time — the partial has its own
        # slot so it can never starve the two final-transcription slots, and a
        # slow pass drops its turn instead of queueing up behind itself.
        self._agree = LocalAgreement()
        self._agree_lock = threading.Lock()   # never taken from inside _lock (invariant #3)
        self._partial_busy = threading.Semaphore(1)
        self._t_partial = 0.0                 # monotonic time of the last partial dispatch
        self._utt_gen = 0                     # bumped on every open/close: makes in-flight partials stale
        self._streamed = ""                   # what the streaming paste has already typed for THIS utterance
        self._streamed_gen = -1               # which utterance _streamed belongs to
        self._t_first_typed = 0.0             # when the first character of THIS utterance reached the app
        self._partials = 0                    # streaming passes run for THIS utterance

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
            partial_blocks = None   # set when a streaming pass is due this block
            partial_gen = 0
            emit_levels = False     # deferred: written outside the lock

            with self._lock:
                # Update level history; emit levels.bin while recording.
                self._rms_history.append(min(rms, 1.0))
                self._levels_tick += 1
                if self._speaking and self._levels_tick >= self._levels_every:
                    self._levels_tick = 0
                    # Deferred: file I/O never runs inside the lock (invariant #3).
                    # At 20 Hz this matters twice as much as it did at 10.
                    emit_levels = True

                if speech:
                    # Speech present (decider's verdict).
                    if not self._speaking:
                        # Onset: start a new utterance, include the pre-roll.
                        self._speaking = True
                        self._t_start = now
                        self._buf = list(self._pre)
                        self._utt_gen += 1     # any partial still in flight is now stale
                        self._t_partial = now
                        # Defer the state write until outside the lock.
                        do_finalize = "start"  # type: ignore[assignment]
                    self._t_silence = None
                    self._buf.append(block)
                    if now - self._t_start >= MAX_DUR:
                        do_finalize = "finalize"  # type: ignore[assignment]
                    elif STREAMING and now - self._t_partial >= PARTIAL_INTERVAL:
                        # Due for a streaming pass. Snapshot the block LIST only
                        # (references, no audio copy) — the concatenate happens
                        # on the worker, never on the audio thread.
                        self._t_partial = now
                        partial_blocks = list(self._buf)
                        partial_gen = self._utt_gen
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
            if emit_levels:
                write_levels(self._rms_history)
            if do_finalize == "start":  # type: ignore[comparison-overlap]
                write_state("recording")
                write_levels(self._rms_history)
                with self._agree_lock:
                    self._agree.reset()
                    self._streamed = ""
                    self._t_first_typed = 0.0
                    self._partials = 0
                clear_partial()
            elif do_finalize == "finalize":  # type: ignore[comparison-overlap]
                self._finalize(now)
                return

            # Streaming pass: one slot, non-blocking. If the previous pass is
            # still decoding we simply skip this turn — a partial is a bonus.
            if partial_blocks is not None and self._partial_busy.acquire(blocking=False):
                threading.Thread(
                    target=self._partial_worker,
                    args=(partial_blocks, partial_gen),
                    daemon=True,
                ).start()
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
            closing_gen = self._utt_gen
            self._utt_gen += 1  # partials still decoding are stale from here on

        # The draft has served its purpose: raw.txt is about to become the
        # authority. (The pill only renders the draft while state == recording,
        # so a partial that lands in the microseconds after this is invisible.)
        clear_partial()

        # Too short to be real speech — drop it silently.
        if dur < MIN_DUR:
            write_state("idle")
            write_levels(self._rms_history)
            return

        # Up to two transcriptions in flight; if both slots are busy, drop this utterance.
        if self._busy.acquire(blocking=False):
            with self._paste_cv:
                seq = self._seq_next
                self._seq_next += 1
            # The worker reads what the stream typed for THIS utterance — by
            # generation, and only after any in-flight chunk has landed. Reading
            # it here would race: a partial pass that has claimed its delta but
            # not yet typed it would be missed, and the final paste would type
            # the same words a second time (seen in the wild 2026-08-02).
            threading.Thread(
                target=self._transcribe_worker, args=(audio, now, seq, closing_gen), daemon=True
            ).start()
        else:
            write_state("idle")

    def _paste_queue_is_clear(self) -> bool:
        """True when no earlier utterance is still waiting to paste.

        Ordering beats latency: typing a live chunk into the middle of a pending
        paste would interleave two sentences. When a predecessor is outstanding
        the stream simply stands down and the final paste delivers the lot.
        """
        with self._paste_cv:
            return self._paste_next >= self._seq_next

    def _partial_worker(self, blocks: list, gen: int) -> None:
        """Re-decode the still-open utterance and publish its stable prefix.

        Best-effort by contract: any failure here leaves the final transcription
        path untouched — the user loses live text for one pass, never the
        sentence. `gen` guards against publishing a draft for an utterance that
        has already been closed or replaced while we were decoding.
        """
        try:
            audio = (
                np.concatenate(blocks) if blocks else np.zeros(1, dtype=np.float32)
            )
            text = transcribe(audio)
            if gen != self._utt_gen:
                return
            with self._agree_lock:
                self._partials += 1
                self._agree.update(text)
                draft = self._agree.confirmed
                # Claim the chunk under the same lock that produced it, so two
                # passes can never type overlapping text.
                to_type = ""
                if STREAM_PASTE and AUTOSEND and self._paste_queue_is_clear():
                    # One word of lag. The newest confirmed word still sits on
                    # the truncation edge, where Whisper puts a full stop that
                    # full context will drop ("lo sto testando." → "testando e").
                    # The draft can re-punctuate itself; typed text cannot.
                    typeable = draft.split()[:-1]
                    already = (self._streamed.split()
                               if self._streamed_gen == gen else [])
                    if len(typeable) > len(already):
                        lead = " " if already else ""
                        to_type = lead + " ".join(typeable[len(already):])
                        self._streamed = " ".join(typeable)
                        self._streamed_gen = gen
            if gen != self._utt_gen:
                return
            write_partial(draft)
            if to_type:
                # Hold the Return daemon BEFORE typing: the gap it measures
                # starts at the first keystroke, not after the last.
                hold_autosend()
                type_text(to_type)
                if not self._t_first_typed:
                    self._t_first_typed = time.monotonic()
        except Exception as err:
            sys.stderr.write(f"VibeVoice: partial pass failed (live text skipped): {err}\n")
        finally:
            self._partial_busy.release()

    _PASTE_ORDER_TIMEOUT = 8.0  # a wedged predecessor must not dam the queue forever

    _PARTIAL_DRAIN_TIMEOUT = 5.0  # a wedged partial must not dam the final paste

    def _collect_streamed(self, closing_gen: int) -> str:
        """What the streaming paste typed for the utterance that just closed.

        Waits for the single partial slot first: a pass that has claimed its
        delta but not yet typed it must be counted, or the final paste repeats
        those words. Runs on the transcription thread, never on the audio thread.
        """
        got = self._partial_busy.acquire(timeout=self._PARTIAL_DRAIN_TIMEOUT)
        try:
            with self._agree_lock:
                return self._streamed if self._streamed_gen == closing_gen else ""
        finally:
            if got:
                self._partial_busy.release()

    def _transcribe_worker(self, audio: np.ndarray, t_end: float = 0.0,
                           seq: int | None = None, closing_gen: int = -1) -> None:
        """Transcribe, publish to raw.txt, and optionally autosend (in order).

        Only the part the streaming paste never typed may be pasted, or the
        sentence would land twice.
        """
        try:
            write_state("transcribing")
            streamed = self._collect_streamed(closing_gen)
            t_start = self._t_start
            kpi = {
                "stream_words": len(streamed.split()),
                "partials": self._partials,
                "t_first_ms": (round((self._t_first_typed - t_start) * 1000.0, 1)
                               if self._t_first_typed and t_start else -1),
            }
            text = process_utterance(audio, t_end=t_end or None, extra=kpi)
            if streamed:
                # Only the remainder — and it must not weld onto the last word
                # the stream already typed ("il polpo" + "ha" = "il polpoha").
                tail = unstreamed_tail(streamed, text)
                if text:
                    # A refused anchor is words the user simply lost: count it.
                    # (Only when a metrics line exists — a phantom writes none.)
                    _patch_last_metrics({
                        "anchor": "ok" if tail else "none",
                        "tail_words": len(tail.split()),
                        "final_words": len(text.split()),
                    })
                text = (" " + tail) if tail else ""
            if text and AUTOSEND:
                # Paste off the worker thread (the busy slot frees promptly);
                # the paste thread itself enforces utterance order.
                threading.Thread(
                    target=self._paste_in_order, args=(seq, text), daemon=True
                ).start()
            else:
                # No paste for this utterance — its turn must pass anyway, and
                # the Return daemon must be freed even when the stream said it all.
                self._advance_paste(seq)
                if not self._speaking:
                    release_autosend()
        finally:
            write_state("idle")
            write_levels(self._rms_history)
            self._busy.release()

    def _paste_in_order(self, seq, text: str) -> None:
        """Paste when it is this utterance's turn (bounded wait, then paste
        anyway: a late paste beats a lost one)."""
        if seq is not None:
            deadline = time.monotonic() + self._PASTE_ORDER_TIMEOUT
            with self._paste_cv:
                while self._paste_next < seq and time.monotonic() < deadline:
                    self._paste_cv.wait(timeout=0.25)
        try:
            autosend(text)
        finally:
            self._advance_paste(seq)
            # The sentence is whole — let the Return daemon fire again, unless a
            # new utterance has already started typing.
            if not self._speaking:
                release_autosend()

    def _advance_paste(self, seq) -> None:
        if seq is None:
            return
        with self._paste_cv:
            self._paste_next = max(self._paste_next, seq + 1)
            self._paste_cv.notify_all()


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
