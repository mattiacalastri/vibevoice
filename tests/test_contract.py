#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Contract tests for VibeVoice.

These exercise the real modules (no reimplementation) and lock down the
state-file contract that decouples the engine from the pill — the invariants
documented in AGENTS.md. They are designed to run headless in CI: no
microphone, no GUI, no Whisper model download. State is redirected to a tmp
dir so the live ~/.vibevoice/ runtime is never touched.

Run:  pytest -q
"""
from __future__ import annotations

import struct
import wave
from collections import deque

import numpy as np
import pytest

import engine

# The pill (vibevoice.py) reads levels.bin with a hard-coded `struct.unpack("<60f", ...)`.
# That magic number lives on the reader side; this is the single source that must agree.
PILL_LEVELS_FORMAT = "<60f"
PILL_LEVELS_BYTES = 60 * 4


# ── levels.bin: the binary heartbeat (invariant #2) ───────────────────────────

@pytest.fixture
def engine_state(tmp_path, monkeypatch):
    """Redirect the engine's state files into a tmp dir."""
    monkeypatch.setattr(engine, "LEVELS_FILE", tmp_path / "levels.bin")
    monkeypatch.setattr(engine, "LEVELS_TMP", tmp_path / "levels.tmp")
    monkeypatch.setattr(engine, "STATE_FILE", tmp_path / "state")
    monkeypatch.setattr(engine, "RAW_FILE", tmp_path / "raw.txt")
    monkeypatch.setattr(engine, "MUTED_FILE", tmp_path / "muted")
    return tmp_path


def _read_levels_as_pill(path) -> tuple[float, ...]:
    """Decode levels.bin exactly the way vibevoice.py's Controller does."""
    data = path.read_bytes()
    assert len(data) >= PILL_LEVELS_BYTES, "torn/short read — pill would skip the frame"
    return struct.unpack(PILL_LEVELS_FORMAT, data[:PILL_LEVELS_BYTES])


def test_levels_roundtrip_full(engine_state):
    """A full history writes 60 floats the pill can read back verbatim."""
    values = [i / 60.0 for i in range(engine.LEVELS_LEN)]
    engine.write_levels(deque(values, maxlen=engine.LEVELS_LEN))

    decoded = _read_levels_as_pill(engine.LEVELS_FILE)
    assert len(decoded) == 60
    assert decoded == pytest.approx(values, abs=1e-6)


def test_levels_left_padded_when_short(engine_state):
    """Fewer than 60 samples are left-padded with zeros — file is always 60 wide."""
    engine.write_levels(deque([0.5, 0.6, 0.7]))

    decoded = _read_levels_as_pill(engine.LEVELS_FILE)
    assert decoded[:-3] == pytest.approx([0.0] * 57)
    assert decoded[-3:] == pytest.approx([0.5, 0.6, 0.7])


def test_levels_keeps_last_60_when_long(engine_state):
    """More than 60 samples keep the most recent 60."""
    values = [float(i) for i in range(100)]
    engine.write_levels(deque(values))

    decoded = _read_levels_as_pill(engine.LEVELS_FILE)
    assert decoded == pytest.approx([float(i) for i in range(40, 100)])


def test_levels_write_is_atomic(engine_state):
    """The staging tmp file must not linger after an atomic os.replace."""
    engine.write_levels(deque([0.1] * engine.LEVELS_LEN))
    assert engine.LEVELS_FILE.exists()
    assert not engine.LEVELS_TMP.exists()


def test_levels_len_matches_pill_magic():
    """Cross-side guard: the pill hard-codes 60; the engine must agree."""
    assert engine.LEVELS_LEN == 60


# ── state / raw text files ────────────────────────────────────────────────────

def test_state_roundtrip(engine_state):
    for state in ("idle", "recording", "transcribing"):
        engine.write_state(state)
        assert engine.STATE_FILE.read_text() == state


def test_raw_roundtrip(engine_state):
    engine.write_raw("apri la dashboard")
    assert engine.RAW_FILE.read_text() == "apri la dashboard"


# ── mute control file: pill writes, engine reads (pause-not-kill contract) ─────

def test_is_muted_reflects_flag_file(engine_state):
    """`is_muted()` mirrors the presence of the muted control file."""
    assert engine.is_muted() is False
    engine.MUTED_FILE.touch()
    assert engine.is_muted() is True
    engine.MUTED_FILE.unlink()
    assert engine.is_muted() is False


def test_muted_engine_ignores_microphone(engine_state):
    """While muted, a loud block must NOT start recording — the mic is paused."""
    eng = engine.Engine()
    engine.MUTED_FILE.touch()
    loud = np.full((engine.BLOCKSIZE, 1), 0.5, dtype=np.float32)  # well above VAD
    eng._audio_callback(loud, engine.BLOCKSIZE, None, None)
    assert eng._speaking is False
    assert engine.STATE_FILE.read_text() == "idle"


def test_unmuted_engine_starts_recording_on_speech(engine_state):
    """Without the mute flag, the same loud block starts an utterance — guards
    that the mute gate does not break the normal capture path."""
    eng = engine.Engine()
    loud = np.full((engine.BLOCKSIZE, 1), 0.5, dtype=np.float32)
    eng._audio_callback(loud, engine.BLOCKSIZE, None, None)
    assert eng._speaking is True
    assert engine.STATE_FILE.read_text() == "recording"


# ── WAV encoding for Whisper (16 kHz / 16-bit / mono) ─────────────────────────

def test_save_wav_format_and_length(tmp_path, monkeypatch):
    audio = np.linspace(-1.0, 1.0, 16000, dtype=np.float32)  # 1 s ramp
    path = engine.save_wav(audio)
    try:
        with wave.open(path, "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2          # 16-bit
            assert wf.getframerate() == engine.SAMPLE_RATE  # 16 kHz
            assert wf.getnframes() == len(audio)
    finally:
        import os
        os.unlink(path)


def test_save_wav_clips_out_of_range(tmp_path):
    """Values beyond [-1, 1] are clipped, not wrapped, to avoid int16 overflow."""
    audio = np.array([2.0, -2.0, 0.0], dtype=np.float32)
    path = engine.save_wav(audio)
    try:
        with wave.open(path, "rb") as wf:
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        assert pcm[0] == 32767
        assert pcm[1] == -32767
    finally:
        import os
        os.unlink(path)


# ── transcription guard (no model in CI) ──────────────────────────────────────

def test_transcribe_returns_empty_when_mlx_unavailable(monkeypatch):
    """If mlx_whisper can't load, transcribe degrades to '' instead of crashing."""
    monkeypatch.setattr(engine, "_ensure_mlx_whisper", lambda: False)
    assert engine.transcribe(np.zeros(1600, dtype=np.float32)) == ""


# ── autosend daemon: arm flag + pause hook (one-shot semantics) ───────────────

@pytest.fixture
def autosend_mod(tmp_path, monkeypatch):
    import autosend
    monkeypatch.setattr(autosend, "STATE_FILE", tmp_path / "autosend")
    monkeypatch.setattr(autosend, "PAUSE_FLAG", tmp_path / "pause")
    return autosend


def test_autosend_enabled_defaults_to_on(autosend_mod):
    """Missing flag file self-heals to 'on' (first run is armed-readable)."""
    assert autosend_mod.is_enabled() is True
    assert autosend_mod.STATE_FILE.read_text() == "on"


def test_autosend_set_enabled_roundtrip(autosend_mod):
    autosend_mod.set_enabled(False)
    assert autosend_mod.is_enabled() is False
    autosend_mod.set_enabled(True)
    assert autosend_mod.is_enabled() is True


def test_pause_flag_fresh_suspends(autosend_mod):
    import time
    autosend_mod.PAUSE_FLAG.write_text(str(time.time()))
    assert autosend_mod.is_paused_by_flag() is True


def test_pause_flag_expired_self_clears(autosend_mod):
    import time
    stale = time.time() - (autosend_mod.PAUSE_TTL_SECONDS + 10)
    autosend_mod.PAUSE_FLAG.write_text(str(stale))
    assert autosend_mod.is_paused_by_flag() is False
    assert not autosend_mod.PAUSE_FLAG.exists()  # anti-deadlock cleanup


def test_pause_flag_absent_is_not_paused(autosend_mod):
    assert autosend_mod.is_paused_by_flag() is False
