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
import time
import wave
from collections import deque
from pathlib import Path

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


# ── Capture backend seam (F1: voice-processing capture + sounddevice fallback) ─

def test_capture_backend_falls_back_without_avfoundation(monkeypatch):
    """AVFoundation unavailable → the seam must select sounddevice."""
    monkeypatch.setattr(engine, "VP_ENABLED", True)
    monkeypatch.setattr(engine, "_ensure_avfoundation", lambda: False)
    backend = engine._select_capture_backend()
    assert backend is engine._SounddeviceCapture
    assert backend.name == "sounddevice"


def test_capture_backend_prefers_voice_processing(monkeypatch):
    """AVFoundation available + VIBEVOICE_VP on → voice-processing is selected."""
    monkeypatch.setattr(engine, "VP_ENABLED", True)
    monkeypatch.setattr(engine, "_ensure_avfoundation", lambda: True)
    backend = engine._select_capture_backend()
    assert backend is engine._VoiceProcessingCapture
    assert backend.name == "voice-processing"


def test_vp_env_kill_switch_forces_sounddevice(monkeypatch):
    """VIBEVOICE_VP=0 must force sounddevice even when AVFoundation imports."""
    monkeypatch.setattr(engine, "VP_ENABLED", False)
    monkeypatch.setattr(engine, "_ensure_avfoundation", lambda: True)
    assert engine._select_capture_backend() is engine._SounddeviceCapture


def test_selection_survives_avfoundation_import_failure(monkeypatch, capsys):
    """Simulate a machine WITHOUT pyobjc-framework-AVFoundation: the real
    _ensure_avfoundation() must swallow the ImportError, print the install
    hint, and the seam must degrade to sounddevice (criterio 'è fatto' F1)."""
    import sys as _sys
    monkeypatch.setattr(engine, "_AVF", None)
    monkeypatch.setattr(engine, "_AVF_AVAILABLE", None)      # reset tri-state cache
    monkeypatch.setitem(_sys.modules, "AVFoundation", None)  # import → ImportError
    monkeypatch.setattr(engine, "VP_ENABLED", True)

    assert engine._ensure_avfoundation() is False
    assert engine._select_capture_backend() is engine._SounddeviceCapture
    assert "pyobjc-framework-AVFoundation" in capsys.readouterr().err


def test_run_falls_back_to_sounddevice_when_vp_open_fails(engine_state, monkeypatch, capsys):
    """A VP backend that raises at open time (mic permission, API error) must
    not kill the engine: run() logs the failure and retries via sounddevice."""
    class _BoomCapture:
        name = "voice-processing"

        def __init__(self, callback):
            pass

        def __enter__(self):
            raise RuntimeError("AVAudioEngine start failed")

        def __exit__(self, *exc):
            return False

    opened = {}

    class _FakeSounddevice:
        name = "sounddevice"

        def __init__(self, callback):
            opened["callback"] = callback

        def __enter__(self):
            opened["entered"] = True
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(engine, "_select_capture_backend", lambda: _BoomCapture)
    monkeypatch.setattr(engine, "_SounddeviceCapture", _FakeSounddevice)
    eng = engine.Engine()
    eng.stop()  # loop exits right after the stream opens — no blocking in CI
    eng.run()

    err = capsys.readouterr().err
    assert opened["entered"] is True
    assert opened["callback"] == eng._audio_callback
    assert "falling back to sounddevice" in err
    assert "capture: sounddevice" in err


def test_run_opens_capture_via_seam_and_logs_backend(engine_state, monkeypatch, capsys):
    """Engine.run() must open the mic through the selected backend and announce
    it on stderr ('capture: <name>') — headless-safe via a fake backend."""
    opened = {}

    class _FakeCapture:
        name = "fake"

        def __init__(self, callback):
            opened["callback"] = callback

        def __enter__(self):
            opened["entered"] = True
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(engine, "_select_capture_backend", lambda: _FakeCapture)
    eng = engine.Engine()
    eng.stop()  # loop exits right after the stream opens — no blocking in CI
    eng.run()

    assert opened["entered"] is True
    assert opened["callback"] == eng._audio_callback  # same signature/target as before
    assert "capture: fake" in capsys.readouterr().err


# ── Speech decider (F2: Silero VAD on a worker thread, RMS-threshold fallback) ─

class _FakeSileroSession:
    """Stand-in for onnxruntime.InferenceSession with a fixed speech
    probability. Echoes the recurrent state back incremented by 1 so the test
    can observe the state actually flowing through consecutive calls."""

    def __init__(self, prob: float) -> None:
        self.prob = prob
        self.feeds: list[dict] = []

    def run(self, _output_names, feeds):
        self.feeds.append({k: np.array(v, copy=True) for k, v in feeds.items()})
        return [
            np.array([[self.prob]], dtype=np.float32),
            np.asarray(feeds["state"], dtype=np.float32) + 1.0,
        ]


@pytest.fixture
def make_silero(monkeypatch):
    """Factory for a started SileroVad wired to a fake ONNX session; the
    fixture stops every started decider so no worker thread leaks."""
    started: list = []

    def _make(prob: float):
        fake = _FakeSileroSession(prob)
        monkeypatch.setattr(engine, "_resolve_silero_model", lambda: "fake.onnx")
        monkeypatch.setattr(engine, "_ensure_onnxruntime", lambda: True)
        monkeypatch.setattr(engine, "_create_silero_session", lambda path: fake)
        vad = engine.SileroVad().start()
        started.append(vad)
        return vad, fake

    yield _make
    for vad in started:
        vad.stop()


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """Poll `predicate` until true or timeout — the worker thread is async."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_silero_speech_probability_turns_speech_on(make_silero):
    """Fake session says speech (prob ≥ onset) → is_speech() flips True even on
    a silent block: with the model active, the model decides, not the energy."""
    vad, fake = make_silero(0.9)
    quiet = np.zeros(engine.BLOCKSIZE, dtype=np.float32)

    assert vad.is_speech() is False
    vad.submit(quiet, rms=0.0)
    vad.submit(quiet, rms=0.0)

    assert _wait_until(vad.is_speech), "worker never published a speech decision"
    # Re-chunk contract: 2×1600 samples = exactly 6 full 512-frames (+128 carry)…
    assert _wait_until(lambda: len(fake.feeds) == 6), "worker did not re-chunk 2 blocks into 6 frames"
    for i, feed in enumerate(fake.feeds):
        # …each fed as context (64) + frame (512) at 16 kHz — the v5 model contract.
        assert feed["input"].shape == (1, engine.SILERO_CONTEXT + engine.SILERO_FRAME)
        assert int(feed["sr"]) == engine.SAMPLE_RATE
        # Recurrent state: call i must receive the state produced by call i-1.
        assert feed["state"].shape == (2, 1, 128)
        assert feed["state"] == pytest.approx(np.full((2, 1, 128), float(i)))


def test_silero_music_high_rms_low_prob_is_not_speech(make_silero):
    """Music: a block well above VAD_THRESHOLD but the model says non-speech →
    is_speech() stays False (the legacy energy VAD alone would have fired)."""
    vad, fake = make_silero(0.1)
    loud = np.full(engine.BLOCKSIZE, 0.5, dtype=np.float32)
    rms = 0.5
    assert rms >= engine.VAD_THRESHOLD  # sanity: the legacy threshold would say speech

    vad.submit(loud, rms=rms)
    assert _wait_until(lambda: len(fake.feeds) >= 3), "worker never consumed the block"
    assert vad.is_speech() is False


def test_silero_absent_is_bit_identical_to_rms_threshold(monkeypatch):
    """onnxruntime unavailable → the decider degrades to the energy threshold:
    is_speech() == (rms >= VAD_THRESHOLD) after every submit, exactly as today."""
    monkeypatch.setattr(engine, "_resolve_silero_model", lambda: "fake.onnx")
    monkeypatch.setattr(engine, "_ensure_onnxruntime", lambda: False)
    vad = engine.SileroVad().start()
    assert vad._worker is None  # no ONNX → no worker thread at all

    block = np.zeros(engine.BLOCKSIZE, dtype=np.float32)
    t = engine.VAD_THRESHOLD
    for rms in (0.0, t / 2, float(np.nextafter(t, 0.0)), t,
                float(np.nextafter(t, 1.0)), 2 * t, 0.5, 1.0):
        vad.submit(block, rms=rms)
        assert vad.is_speech() is (rms >= t), f"fallback drifted from threshold at rms={rms}"


class _ExplodingSileroSession(_FakeSileroSession):
    """_FakeSileroSession that raises after `explode_after` successful frames —
    drives the mid-stream inference-failure branch of SileroVad._run."""

    def __init__(self, prob: float, explode_after: int) -> None:
        super().__init__(prob)
        self.explode_after = explode_after

    def run(self, _output_names, feeds):
        if len(self.feeds) >= self.explode_after:
            raise RuntimeError("mid-stream inference failure")
        return super().run(_output_names, feeds)


def test_silero_midstream_failure_degrades_to_rms_fallback(monkeypatch, capsys):
    """Inference raising mid-stream (SileroVad._run's except branch): the
    decider must flip to the RMS fallback (_active=False) instead of freezing
    the last neural decision — the second leg of the ARCHITECTURE.md §2.3
    degradation contract (the onnxruntime-absent leg is locked by
    test_full_flow_without_onnxruntime_behaves_like_legacy)."""
    fake = _ExplodingSileroSession(prob=0.9, explode_after=3)
    monkeypatch.setattr(engine, "_resolve_silero_model", lambda: "fake.onnx")
    monkeypatch.setattr(engine, "_ensure_onnxruntime", lambda: True)
    monkeypatch.setattr(engine, "_create_silero_session", lambda path: fake)
    vad = engine.SileroVad().start()
    try:
        worker = vad._worker
        quiet = np.zeros(engine.BLOCKSIZE, dtype=np.float32)

        # Healthy stretch: the model (prob ≥ onset) decides speech on silence
        # (one 1600-sample block = exactly explode_after=3 successful frames).
        vad.submit(quiet, rms=0.0)
        assert _wait_until(vad.is_speech), "model never published speech pre-failure"

        # The next frame raises inside the worker → degrade, announce, stop.
        vad.submit(quiet, rms=0.0)
        assert _wait_until(lambda: not worker.is_alive()), "worker survived the failure"
        assert vad._active is False
        assert "falling back to the RMS threshold" in capsys.readouterr().err

        # From here on the decision is exactly the energy threshold again: the
        # stale neural True must not leak through (frozen-decision scar).
        t = engine.VAD_THRESHOLD
        for rms in (0.0, t / 2, t, 2 * t):
            vad.submit(quiet, rms=rms)
            assert vad.is_speech() is (rms >= t), f"fallback drifted at rms={rms}"
    finally:
        vad.stop()


# ── Decider wired into the audio callback (F2, task 4) ────────────────────────

@pytest.fixture
def arm_engine_decider(monkeypatch):
    """Arm an Engine's own decider with a fake ONNX session (same seams as
    make_silero, but started on `eng._vad` so the callback wiring is exercised);
    stops every armed decider so no worker thread leaks."""
    armed: list = []

    def _arm(eng, prob: float):
        fake = _FakeSileroSession(prob)
        monkeypatch.setattr(engine, "_resolve_silero_model", lambda: "fake.onnx")
        monkeypatch.setattr(engine, "_ensure_onnxruntime", lambda: True)
        monkeypatch.setattr(engine, "_create_silero_session", lambda path: fake)
        eng._vad.start()
        armed.append(eng._vad)
        return fake

    yield _arm
    for vad in armed:
        vad.stop()


def test_callback_loud_music_not_recorded_when_model_says_nonspeech(
    engine_state, arm_engine_decider
):
    """Music: blocks well above VAD_THRESHOLD but the model says non-speech →
    the callback must NOT start an utterance. Proves the state machine is
    driven by the decider's decision, not by the raw RMS threshold."""
    eng = engine.Engine()
    fake = arm_engine_decider(eng, prob=0.1)
    loud = np.full((engine.BLOCKSIZE, 1), 0.5, dtype=np.float32)
    assert 0.5 >= engine.VAD_THRESHOLD  # sanity: the legacy threshold would fire

    for _ in range(3):
        eng._audio_callback(loud, engine.BLOCKSIZE, None, None)
    assert _wait_until(lambda: len(fake.feeds) >= 3), "worker never consumed the blocks"
    eng._audio_callback(loud, engine.BLOCKSIZE, None, None)  # decision now settled

    assert eng._speaking is False


def test_callback_quiet_speech_starts_recording_when_model_says_speech(
    engine_state, arm_engine_decider
):
    """The model hears speech in a block below the RMS threshold → the callback
    starts recording (with the legacy energy VAD this block was inaudible)."""
    eng = engine.Engine()
    arm_engine_decider(eng, prob=0.9)
    quiet = np.full((engine.BLOCKSIZE, 1), 0.001, dtype=np.float32)
    assert 0.001 < engine.VAD_THRESHOLD  # sanity: the legacy threshold stays silent

    # Pre-warm: inference is async — let the worker publish "speech" first.
    eng._vad.submit(quiet[:, 0], rms=0.001)
    assert _wait_until(eng._vad.is_speech), "worker never published a speech decision"

    eng._audio_callback(quiet, engine.BLOCKSIZE, None, None)

    assert eng._speaking is True
    assert engine.STATE_FILE.read_text() == "recording"


def test_levels_bin_identical_with_neural_vad_on_and_off(engine_state, arm_engine_decider):
    """Same audio → byte-identical levels.bin with the neural decider on or off:
    the decider replaces the speech DECISION only; the RMS→levels.bin pipeline
    must stay untouched (invariant #2)."""
    rng = np.random.default_rng(9465)
    blocks = [
        rng.normal(0.3, 0.05, (engine.BLOCKSIZE, 1)).astype(np.float32)
        for _ in range(12)
    ]  # loud enough that BOTH deciders say speech on every block

    def feed_all(eng) -> bytes:
        for b in blocks:
            eng._audio_callback(b, engine.BLOCKSIZE, None, None)
        assert eng._speaking is True  # guard: both paths really recorded
        return engine.LEVELS_FILE.read_bytes()

    # Neural ON (fake model: always speech; pre-warmed so the async worker
    # cannot skew the first decisions).
    eng_on = engine.Engine()
    arm_engine_decider(eng_on, prob=0.9)
    eng_on._vad.submit(blocks[0][:, 0], rms=0.5)
    assert _wait_until(eng_on._vad.is_speech), "worker never published a speech decision"
    with_neural = feed_all(eng_on)

    engine.LEVELS_FILE.unlink()

    # Neural OFF (unstarted decider → RMS fallback, exactly the legacy path).
    without_neural = feed_all(engine.Engine())

    assert with_neural == without_neural


def test_run_starts_and_stops_the_decider(engine_state, monkeypatch):
    """Engine.run() must bring the decider up before capture opens and stop its
    worker on the way out — the callback itself never manages lifecycles."""
    class _FakeCapture:
        name = "fake"

        def __init__(self, callback):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(engine, "_select_capture_backend", lambda: _FakeCapture)
    eng = engine.Engine()
    calls: list[str] = []
    monkeypatch.setattr(eng._vad, "start", lambda: calls.append("start"))
    monkeypatch.setattr(eng._vad, "stop", lambda: calls.append("stop"))
    eng.stop()  # loop exits right after the stream opens — no blocking in CI
    eng.run()
    assert calls == ["start", "stop"]


# ── Degradation lock-down (task 5: no onnxruntime / no AVFoundation) ──────────

def test_full_flow_without_onnxruntime_behaves_like_legacy(
    engine_state, monkeypatch, capsys
):
    """Machine WITHOUT onnxruntime (sys.modules poisoned, REAL
    _ensure_onnxruntime): the whole callback→finalize→transcribe flow must
    behave exactly as today — RMS-threshold decisions, same state-file
    transitions, transcription published to raw.txt + history."""
    import sys as _sys
    monkeypatch.setattr(engine, "_ORT", None)
    monkeypatch.setattr(engine, "_ORT_AVAILABLE", None)     # reset tri-state cache
    monkeypatch.setitem(_sys.modules, "onnxruntime", None)  # import → ImportError
    monkeypatch.setattr(engine, "_resolve_silero_model", lambda: "fake.onnx")
    # Deterministic finalize in CI: no real-time waits, no clipboard, no model.
    monkeypatch.setattr(engine, "SILENCE_SEC", 0.0)
    monkeypatch.setattr(engine, "MIN_DUR", 0.0)
    monkeypatch.setattr(engine, "AUTOSEND", False)
    monkeypatch.setattr(engine, "HISTORY_FILE", engine_state / "history.jsonl")
    monkeypatch.setattr(engine, "transcribe", lambda audio: "senza onnx")

    eng = engine.Engine()
    eng._vad.start()  # degrades in place: no worker thread, RMS fallback
    assert eng._vad._worker is None
    assert eng._vad._active is False
    assert "onnxruntime" in capsys.readouterr().err  # the real install hint fired

    loud = np.full((engine.BLOCKSIZE, 1), 0.5, dtype=np.float32)
    quiet = np.zeros((engine.BLOCKSIZE, 1), dtype=np.float32)

    eng._audio_callback(loud, engine.BLOCKSIZE, None, None)
    assert eng._speaking is True                            # RMS onset, exactly as today
    assert engine.STATE_FILE.read_text() == "recording"

    eng._audio_callback(quiet, engine.BLOCKSIZE, None, None)  # arms the silence timer
    eng._audio_callback(quiet, engine.BLOCKSIZE, None, None)  # SILENCE_SEC=0 → finalize

    assert eng._speaking is False
    assert _wait_until(
        lambda: engine.RAW_FILE.exists() and engine.RAW_FILE.read_text() == "senza onnx"
    ), "transcription never reached raw.txt on the degraded path"
    assert _wait_until(lambda: engine.STATE_FILE.read_text() == "idle")
    assert engine.HISTORY_FILE.exists()


def test_full_run_without_avfoundation_captures_via_sounddevice(
    engine_state, monkeypatch, capsys
):
    """Machine WITHOUT pyobjc-framework-AVFoundation (sys.modules poisoned,
    REAL _ensure_avfoundation): Engine.run() must degrade to sounddevice and
    the capture flow must keep working exactly as today under the fallback."""
    import sys as _sys
    monkeypatch.setattr(engine, "_AVF", None)
    monkeypatch.setattr(engine, "_AVF_AVAILABLE", None)      # reset tri-state cache
    monkeypatch.setitem(_sys.modules, "AVFoundation", None)  # import → ImportError
    monkeypatch.setattr(engine, "VP_ENABLED", True)
    monkeypatch.setattr(engine, "_resolve_silero_model", lambda: None)  # keep F2 out of the frame

    seen = {}

    class _FakeSounddevice:
        name = "sounddevice"

        def __init__(self, callback):
            self._callback = callback

        def __enter__(self):
            # Prove the degraded backend still drives the normal capture flow.
            loud = np.full((engine.BLOCKSIZE, 1), 0.5, dtype=np.float32)
            self._callback(loud, engine.BLOCKSIZE, None, None)
            seen["state_after_loud"] = engine.STATE_FILE.read_text()
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(engine, "_SounddeviceCapture", _FakeSounddevice)
    eng = engine.Engine()
    eng.stop()  # loop exits right after the stream opens — no blocking in CI
    eng.run()

    err = capsys.readouterr().err
    assert "pyobjc-framework-AVFoundation" in err  # the real install hint fired
    assert "capture: sounddevice" in err
    assert seen["state_after_loud"] == "recording"  # callback flow intact under fallback
    assert engine.STATE_FILE.read_text() == "idle"  # run() closed the session cleanly


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


# ── history.jsonl: last transcriptions (settings window reads this) ───────────

@pytest.fixture
def history_state(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "HISTORY_FILE", tmp_path / "history.jsonl")
    return tmp_path


def test_history_appends_and_caps(history_state):
    import json
    for i in range(25):
        engine._append_history(f"frase {i}")
    lines = (history_state / "history.jsonl").read_text().splitlines()
    assert len(lines) == engine.HISTORY_MAX == 20
    assert json.loads(lines[-1])["text"] == "frase 24"   # newest last
    assert set(json.loads(lines[0])) == {"ts", "text"}


def test_history_write_failure_never_raises(history_state, monkeypatch):
    monkeypatch.setattr(engine, "HISTORY_FILE", history_state / "no" / "dir.jsonl")
    engine._append_history("must not raise")  # transcription path must survive


# ── AGENTS.md §6: the barge-in acceptance procedure is documented ─────────────
#
# The end-to-end criterion for the full-duplex jump (TTS-only / barge-in /
# music-only, judged on history.jsonl) runs on the LIVE runtime with a human
# voice at the mic — never in pytest (CLAUDE.md rule 2). What CAN be locked
# headless is that the ritual exists in AGENTS.md §6 with its measurable
# commands, so the doc and the runtime procedure cannot drift apart.

def _agents_section_6() -> str:
    text = (Path(engine.__file__).parent / "AGENTS.md").read_text()
    return text[text.index("## 6."):text.index("## 7.")]


def test_agents_documents_barge_in_acceptance():
    sec = _agents_section_6()
    low = sec.lower()
    # The three phases, by name.
    assert "tts-only" in low
    assert "barge-in" in low
    assert "music" in low
    # The measurable commands the criterion is judged by.
    assert "pgrep -f stt_bar.py" in sec              # legacy engine off → exit 1
    assert "L0=$(wc -l < ~/.vibevoice/history.jsonl" in sec
    assert "L0+1" in sec                             # barge-in grows it by one…
    assert "tail -1" in sec                          # …and the phrase is the last line
    assert "afplay" in sec                           # speakers open during the test
    # Measurable startup evidence that VP + Silero are the active paths.
    assert "VibeVoice: capture: voice-processing" in sec
    assert "VibeVoice: VAD: silero" in sec
