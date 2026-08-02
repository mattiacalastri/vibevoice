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
import threading
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
    """Redirect the engine's state files into a tmp dir, and disarm every path
    that reaches OUT of the process.

    EVERY module-level file the engine can write must be listed here: a full-flow
    test reaches process_utterance, and a path left unredirected leaks into the
    live ~/.vibevoice/ (scar sess.9685: seven fake entries in the real
    metrics.jsonl, written by this very suite before METRICS_FILE was patched).

    The same argument applies to the OUTBOUND switches, and it bites harder: a
    file written in the wrong place is recoverable, synthetic keystrokes posted
    into whatever app the user has in front of them are not. When the streaming
    paste landed (2026-08-02) `AUTOSEND` and `STREAM_PASTE` both defaulted to on,
    so the partial-pass tests typed "il polpo ha" into the user's screen and
    overwrote their clipboard. Off here by default: a test that wants to observe
    the paste turns it back on **and** replaces `type_text`/`autosend` with a
    recorder (see the `typed` fixture).
    """
    monkeypatch.setattr(engine, "AUTOSEND", False)
    monkeypatch.setattr(engine, "STREAM_PASTE", False)
    monkeypatch.setattr(engine, "LEVELS_FILE", tmp_path / "levels.bin")
    monkeypatch.setattr(engine, "LEVELS_TMP", tmp_path / "levels.tmp")
    monkeypatch.setattr(engine, "STATE_FILE", tmp_path / "state")
    monkeypatch.setattr(engine, "RAW_FILE", tmp_path / "raw.txt")
    monkeypatch.setattr(engine, "MUTED_FILE", tmp_path / "muted")
    monkeypatch.setattr(engine, "HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr(engine, "METRICS_FILE", tmp_path / "metrics.jsonl")
    monkeypatch.setattr(engine, "DICT_FILE", tmp_path / "dictionary.txt")
    monkeypatch.setattr(engine, "CORRECTIONS_FILE", tmp_path / "corrections.jsonl")
    monkeypatch.setattr(engine, "PARTIAL_FILE", tmp_path / "partial.txt")
    monkeypatch.setattr(engine, "PARTIAL_TMP", tmp_path / "partial.tmp")
    monkeypatch.setattr(engine, "AUTOSEND_PAUSE_FLAG", tmp_path / "autosend_pause")
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


# ── Waveform cadence: the bars can only move as often as data arrives ────────

def test_levels_data_rate_is_at_least_twenty_per_second():
    """The pill redraws at 24 fps but can only *move* when a new RMS sample
    lands. At one block per 100 ms each bar was held for 2.4 frames and the
    scroll visibly stepped (reported from the live runtime, 2026-08-02).
    """
    blocks_per_sec = engine.SAMPLE_RATE / engine.BLOCKSIZE
    assert blocks_per_sec >= 20, f"only {blocks_per_sec:.0f} waveform samples/s"
    assert engine.LEVELS_HZ >= 20, "the writer would throttle away the extra samples"


def test_levels_write_rate_is_not_throttled_below_the_block_rate():
    """`_levels_every` must not silently drop back to one write per N blocks."""
    eng = engine.Engine()
    effective_hz = (engine.SAMPLE_RATE / engine.BLOCKSIZE) / eng._levels_every
    assert effective_hz >= 20, f"levels.bin written only {effective_hz:.0f} times/s"


def test_pre_roll_keeps_half_a_second_of_audio():
    """Pre-roll is a DURATION, not a block count: it is what saves the first
    syllable, and halving the block size must not halve it."""
    pre_roll_s = engine.PRE_ROLL_BLOCKS * engine.BLOCKSIZE / engine.SAMPLE_RATE
    assert pre_roll_s == pytest.approx(0.5, abs=0.06), f"{pre_roll_s:.2f}s of pre-roll"


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


def _frames_after(n_blocks: int) -> int:
    """Frames the re-chunker must have consumed after `n_blocks` full blocks.

    Derived from the constants, never hard-coded: the law is "consume
    floor(samples / SILERO_FRAME), carry the rest", and it must keep holding
    when BLOCKSIZE changes (it went 1600 → 800 on 2026-08-02 to double the
    waveform's data rate, and four tests that had baked in 1600/512 = 3 went
    red — they were asserting an arithmetic coincidence, not the contract).
    """
    return (n_blocks * engine.BLOCKSIZE) // engine.SILERO_FRAME


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
    # Re-chunk contract: whatever the block size, floor(samples/512) frames.
    assert _wait_until(lambda: len(fake.feeds) == _frames_after(2)), "worker did not re-chunk 2 blocks"
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
    assert _wait_until(lambda: len(fake.feeds) >= _frames_after(1)), "worker never consumed the block"
    assert vad.is_speech() is False


def test_silero_hysteresis_band_holds_previous_decision(make_silero):
    """Probabilities strictly inside the hysteresis band (SILERO_OFFSET < prob
    < SILERO_ONSET) must HOLD the previous decision in both directions — the
    anti-flap contract of SileroVad._infer (ARCHITECTURE.md §2.3). A regression
    collapsing either edge into the band (e.g. `elif prob <= SILERO_OFFSET` →
    `else`) flips these assertions."""
    vad, fake = make_silero(0.9)  # start above onset
    band = (engine.SILERO_OFFSET + engine.SILERO_ONSET) / 2
    assert engine.SILERO_OFFSET < band < engine.SILERO_ONSET  # sanity: strictly inside
    quiet = np.zeros(engine.BLOCKSIZE, dtype=np.float32)

    # Onset edge: prob ≥ SILERO_ONSET turns the decision ON.
    vad.submit(quiet, rms=0.0)
    assert _wait_until(vad.is_speech), "onset never turned speech on"
    # Wait for the block to be fully consumed before mutating the probability
    # (the worker is async); the frame count follows from the constants.
    assert _wait_until(lambda: len(fake.feeds) == _frames_after(1)), "first block not fully consumed"

    # Band while ON: the previous True must hold, not decay to False.
    fake.prob = band
    vad.submit(quiet, rms=0.0)
    assert _wait_until(lambda: len(fake.feeds) == _frames_after(2)), "second block not fully consumed"
    assert vad.is_speech() is True, "hysteresis band dropped the ON decision"

    # Offset edge: prob ≤ SILERO_OFFSET turns the decision OFF.
    fake.prob = 0.1
    vad.submit(quiet, rms=0.0)
    assert _wait_until(lambda: not vad.is_speech()), "offset never turned speech off"
    assert _wait_until(lambda: len(fake.feeds) == _frames_after(3)), "third block not fully consumed"

    # Band while OFF: the previous False must hold, not re-trigger True.
    fake.prob = band
    vad.submit(quiet, rms=0.0)
    assert _wait_until(lambda: len(fake.feeds) == _frames_after(4)), "fourth block not fully consumed"
    assert vad.is_speech() is False, "hysteresis band re-raised the OFF decision"


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
    fake = _ExplodingSileroSession(prob=0.9, explode_after=_frames_after(1))
    monkeypatch.setattr(engine, "_resolve_silero_model", lambda: "fake.onnx")
    monkeypatch.setattr(engine, "_ensure_onnxruntime", lambda: True)
    monkeypatch.setattr(engine, "_create_silero_session", lambda path: fake)
    vad = engine.SileroVad().start()
    try:
        worker = vad._worker
        quiet = np.zeros(engine.BLOCKSIZE, dtype=np.float32)

        # Healthy stretch: the model (prob ≥ onset) decides speech on silence;
        # one block is exactly explode_after successful frames.
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


# ── Streaming: LocalAgreement-2 stabilizer (F3) ───────────────────────────────
# A word is published only once two successive hypotheses agree on it. That is
# what makes live text stable instead of flickering: Whisper re-decodes the whole
# buffer every pass and freely rewrites its own tail, so the tail is never
# trustworthy — the agreed prefix is.

def test_local_agreement_publishes_nothing_on_the_first_hypothesis():
    """One hypothesis has nothing to agree with — it is all still tentative."""
    agree = engine.LocalAgreement()
    assert agree.update("il polpo ha otto") == ""
    assert agree.confirmed == ""


def test_local_agreement_confirms_only_the_agreed_prefix():
    """Two hypotheses agree on 'il polpo ha' — the divergent tail stays tentative."""
    agree = engine.LocalAgreement()
    agree.update("il polpo ha otto")
    assert agree.update("il polpo ha molti tentacoli") == "il polpo ha"
    assert agree.confirmed == "il polpo ha"


def test_local_agreement_emits_each_word_exactly_once():
    """Successive updates return only the delta — never re-emit the prefix."""
    agree = engine.LocalAgreement()
    agree.update("il polpo")
    agree.update("il polpo ha otto")          # confirms "il polpo"
    delta = agree.update("il polpo ha otto tentacoli")  # confirms "ha otto"
    assert delta == "ha otto"
    assert agree.confirmed == "il polpo ha otto"


def test_local_agreement_never_retracts_a_confirmed_word():
    """A later hypothesis that contradicts confirmed text must not un-say it."""
    agree = engine.LocalAgreement()
    agree.update("il polpo ha")
    agree.update("il polpo ha")               # confirms all three
    assert agree.update("il") == ""           # shorter/divergent: no retraction
    assert agree.confirmed == "il polpo ha"


def test_local_agreement_matches_across_punctuation_and_case_drift():
    """Whisper re-punctuates as context grows; agreement is on words, not glyphs."""
    agree = engine.LocalAgreement()
    agree.update("Ciao mondo")
    assert agree.update("ciao, mondo come stai") == "ciao, mondo"


def test_local_agreement_repunctuates_confirmed_words_as_context_grows():
    """A word confirmed at the truncation edge carries a full stop that is wrong
    mid-sentence ("autonomo." → "autonomo,"). Measured live on 2026-08-02: the
    draft read "modo autonomo. mentre il cervello". The word order is what must
    never change; the glyphs must converge on what the final text will say.
    """
    agree = engine.LocalAgreement()
    agree.update("pensa in modo autonomo.")
    agree.update("pensa in modo autonomo. mentre")      # confirms four words
    agree.update("pensa in modo autonomo, mentre il")   # full context: it was a comma
    assert agree.confirmed == "pensa in modo autonomo, mentre"


def test_local_agreement_keeps_confirmed_words_when_the_tail_shrinks():
    """Re-rendering from the newest hypothesis must not lose confirmed words if
    that hypothesis comes back shorter than what is already published."""
    agree = engine.LocalAgreement()
    agree.update("il polpo ha otto")
    agree.update("il polpo ha otto")   # confirms all four
    agree.update("il polpo")           # a bad pass: shorter
    assert agree.confirmed == "il polpo ha otto"


def test_local_agreement_reset_starts_a_new_utterance_clean():
    """Utterance N+1 must not inherit the confirmed prefix of utterance N."""
    agree = engine.LocalAgreement()
    agree.update("prima frase")
    agree.update("prima frase")
    agree.reset()
    assert agree.confirmed == ""
    assert agree.update("seconda") == ""


# ── Streaming: the partial.txt contract file ──────────────────────────────────

def test_partial_roundtrip(engine_state):
    """partial.txt is plain text, like raw.txt — the live sentence so far."""
    engine.write_partial("il polpo ha otto")
    assert engine.PARTIAL_FILE.read_text() == "il polpo ha otto"


def test_partial_write_is_atomic(engine_state):
    """Written via tmp + os.replace: the pill never sees a torn sentence."""
    engine.write_partial("una frase intera")
    assert not engine.PARTIAL_TMP.exists()


def test_clear_partial_removes_the_file(engine_state):
    """No file = nothing live to show. Absence is the 'no partial' signal."""
    engine.write_partial("qualcosa")
    engine.clear_partial()
    assert not engine.PARTIAL_FILE.exists()


def test_clear_partial_is_idempotent(engine_state):
    """Clearing twice (or before any write) must never raise."""
    engine.clear_partial()
    engine.clear_partial()
    assert not engine.PARTIAL_FILE.exists()


# ── Streaming: the engine publishes partials WHILE speech is still going ──────

def _speech_block(level: float = 0.5) -> np.ndarray:
    return np.full((engine.BLOCKSIZE, 1), level, dtype=np.float32)


def _drain_partials(eng, timeout: float = 5.0) -> None:
    """Wait for the single partial slot to be free again (worker finished)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if eng._partial_busy.acquire(blocking=False):
            eng._partial_busy.release()
            return
        time.sleep(0.01)
    raise AssertionError("partial worker never finished")


def _drain_finals(eng, timeout: float = 5.0) -> None:
    """Wait for the final transcription AND its paste to have run their course."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with eng._paste_cv:
            settled = eng._paste_next >= eng._seq_next
        if settled and eng._busy._value == 2:
            time.sleep(0.05)   # let the paste thread's last statement land
            return
        time.sleep(0.01)
    raise AssertionError("final transcription/paste never settled")


def test_engine_publishes_a_partial_while_still_speaking(engine_state, monkeypatch):
    """The whole point: text exists BEFORE the utterance is finalized."""
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)  # every block
    monkeypatch.setattr(engine, "transcribe", lambda audio: "il polpo ha")

    eng = engine.Engine()
    for _ in range(3):
        eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
        _drain_partials(eng)

    assert eng._speaking is True, "utterance must still be open"
    assert engine.STATE_FILE.read_text() == "recording"
    assert engine.PARTIAL_FILE.read_text() == "il polpo ha"


def test_partial_pass_never_writes_raw_or_history(engine_state, monkeypatch):
    """Partials are provisional: only a finalized utterance may touch raw.txt."""
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)
    monkeypatch.setattr(engine, "transcribe", lambda audio: "provvisorio")

    eng = engine.Engine()
    for _ in range(2):
        eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
        _drain_partials(eng)

    assert not engine.RAW_FILE.exists()
    assert not engine.HISTORY_FILE.exists()


def test_finalize_clears_the_partial(engine_state, monkeypatch):
    """When the real text lands in raw.txt the live draft must disappear."""
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)
    monkeypatch.setattr(engine, "transcribe", lambda audio: "frase")
    monkeypatch.setattr(engine, "AUTOSEND", False)

    eng = engine.Engine()
    eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
    _drain_partials(eng)
    assert engine.PARTIAL_FILE.exists()

    eng._finalize(eng._t_start + engine.MIN_DUR + 0.1)
    assert not engine.PARTIAL_FILE.exists()


def test_new_utterance_does_not_inherit_the_previous_partial(engine_state, monkeypatch):
    """Utterance N+1 starts from an empty draft, not from N's confirmed prefix."""
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)
    monkeypatch.setattr(engine, "transcribe", lambda audio: "prima")
    monkeypatch.setattr(engine, "AUTOSEND", False)

    eng = engine.Engine()
    eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
    _drain_partials(eng)
    eng._finalize(eng._t_start + engine.MIN_DUR + 0.1)

    eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
    assert eng._agree.confirmed == "", "stabilizer must reset at utterance onset"


def test_streaming_off_behaves_exactly_like_the_legacy_engine(engine_state, monkeypatch):
    """Degradation is contract: with STREAMING off, no partial work happens at all."""
    monkeypatch.setattr(engine, "STREAMING", False)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)

    def _boom(audio):
        raise AssertionError("no transcription may run during capture when streaming is off")

    monkeypatch.setattr(engine, "transcribe", _boom)

    eng = engine.Engine()
    for _ in range(3):
        eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)

    assert eng._speaking is True
    assert not engine.PARTIAL_FILE.exists()


# ── Streaming paste: typing the confirmed words while the sentence is open ────
# The confirmed prefix never retracts — that is exactly what makes it safe to
# type. What must never happen is typing the same words twice: the final paste
# may only add the tail the stream never reached.

def test_unstreamed_tail_is_the_whole_text_when_nothing_was_streamed():
    assert engine.unstreamed_tail("", "il polpo ha otto") == "il polpo ha otto"


def test_unstreamed_tail_is_empty_when_the_stream_already_said_everything():
    assert engine.unstreamed_tail("il polpo ha otto", "il polpo ha otto") == ""


def test_unstreamed_tail_returns_only_the_words_the_stream_never_reached():
    assert engine.unstreamed_tail("il polpo ha", "il polpo ha otto tentacoli") == "otto tentacoli"


def test_unstreamed_tail_aligns_across_punctuation_drift():
    """The stream typed "autonomo"; the final says "autonomo," — same word, no repeat."""
    assert engine.unstreamed_tail(
        "pensa in modo autonomo", "Pensa in modo autonomo, mentre il"
    ) == "mentre il"


def test_unstreamed_tail_keeps_the_tail_when_the_final_diverges_mid_sentence():
    """Losing the end of a sentence is worse than a couple of repeated words."""
    assert engine.unstreamed_tail(
        "il polpo ha molti", "il polpo ha otto tentacoli"
    ) == "otto tentacoli"


@pytest.fixture
def typed(monkeypatch):
    """Record what the engine types instead of hitting the real keyboard."""
    calls: list[str] = []
    monkeypatch.setattr(engine, "type_text", lambda text: calls.append(text))
    monkeypatch.setattr(engine, "autosend", lambda text: calls.append(text))
    return calls


def test_stream_paste_types_the_confirmed_delta_while_still_speaking(engine_state, monkeypatch, typed):
    """The whole point: words reach the app BEFORE the trailing silence expires."""
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "STREAM_PASTE", True)
    monkeypatch.setattr(engine, "AUTOSEND", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)
    monkeypatch.setattr(engine, "transcribe", lambda audio: "il polpo ha")

    eng = engine.Engine()
    for _ in range(2):
        eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
        _drain_partials(eng)

    assert eng._speaking is True, "must have typed while the utterance was open"
    assert "".join(typed) == "il polpo ha"


def test_stream_paste_separates_successive_chunks_with_a_space(engine_state, monkeypatch, typed):
    """Two deltas must not weld into "il polpoha"."""
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "STREAM_PASTE", True)
    monkeypatch.setattr(engine, "AUTOSEND", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)

    hypotheses = iter(["il polpo", "il polpo ha", "il polpo ha otto", "il polpo ha otto"])
    monkeypatch.setattr(engine, "transcribe", lambda audio: next(hypotheses, "il polpo ha otto"))

    eng = engine.Engine()
    for _ in range(4):
        eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
        _drain_partials(eng)

    assert "".join(typed) == "il polpo ha otto"


def test_final_paste_adds_only_the_tail_after_streaming(engine_state, monkeypatch, typed):
    """No duplication: the stream said "il polpo", the final adds " ha otto"."""
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "STREAM_PASTE", True)
    monkeypatch.setattr(engine, "AUTOSEND", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)
    monkeypatch.setattr(engine, "transcribe", lambda audio: "il polpo")

    eng = engine.Engine()
    for _ in range(2):
        eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
        _drain_partials(eng)
    assert "".join(typed) == "il polpo"

    monkeypatch.setattr(engine, "transcribe", lambda audio: "il polpo ha otto")
    eng._finalize(eng._t_start + engine.MIN_DUR + 0.1)
    _drain_finals(eng)

    assert "".join(typed) == "il polpo ha otto", "the tail only, never the whole sentence again"


def test_engine_state_fixture_disarms_the_outbound_switches(engine_state):
    """The argine itself: no test may reach the user's keyboard or clipboard by
    default. If this fails, every other test in this file became dangerous."""
    assert engine.AUTOSEND is False
    assert engine.STREAM_PASTE is False


def test_stream_paste_off_still_pastes_the_whole_sentence_once(engine_state, monkeypatch, typed):
    """Degradation is contract: with streaming paste off the behaviour is the old one."""
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "STREAM_PASTE", False)
    monkeypatch.setattr(engine, "AUTOSEND", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)
    monkeypatch.setattr(engine, "transcribe", lambda audio: "il polpo ha otto")

    eng = engine.Engine()
    eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
    _drain_partials(eng)
    assert typed == [], "nothing may be typed mid-utterance when the feature is off"

    eng._finalize(eng._t_start + engine.MIN_DUR + 0.1)
    _drain_finals(eng)
    assert typed == ["il polpo ha otto"]


def test_stream_paste_stands_down_while_a_previous_utterance_is_still_pasting(engine_state, monkeypatch, typed):
    """Ordering beats latency: typing into the middle of a pending paste would
    interleave two sentences. The stream skips its turn and the final paste
    delivers the whole thing."""
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "STREAM_PASTE", True)
    monkeypatch.setattr(engine, "AUTOSEND", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)
    monkeypatch.setattr(engine, "transcribe", lambda audio: "seconda frase")

    eng = engine.Engine()
    # Simulate an earlier utterance whose paste has not had its turn yet.
    with eng._paste_cv:
        eng._seq_next = 1
        eng._paste_next = 0

    for _ in range(2):
        eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
        _drain_partials(eng)

    assert typed == [], "must not type over a pending paste"


# ── Streaming paste vs the standalone auto-Return daemon ─────────────────────
# autosend.py fires Return after AUTO_SEND_DELAY (0.8s) of typing silence. The
# streaming paste types in bursts ~0.65s apart — close enough that one slow pass
# would send the message mid-sentence. The engine therefore holds the daemon's
# pause flag while it is streaming and releases it once the sentence is whole.

def test_stream_paste_holds_the_auto_return_daemon(engine_state, monkeypatch, typed):
    """A pause between two typed chunks must not be read as 'the user stopped'."""
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "STREAM_PASTE", True)
    monkeypatch.setattr(engine, "AUTOSEND", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)
    monkeypatch.setattr(engine, "transcribe", lambda audio: "il polpo ha")

    eng = engine.Engine()
    for _ in range(2):
        eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
        _drain_partials(eng)

    assert engine.AUTOSEND_PAUSE_FLAG.exists(), "the daemon must be held while streaming"
    held_at = float(engine.AUTOSEND_PAUSE_FLAG.read_text().strip())
    assert abs(held_at - time.time()) < 5, "a stale timestamp would let the TTL expire mid-sentence"


def test_finished_sentence_releases_the_auto_return_daemon(engine_state, monkeypatch, typed):
    """Once the sentence is whole the Return must be allowed to fire."""
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "STREAM_PASTE", True)
    monkeypatch.setattr(engine, "AUTOSEND", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)
    monkeypatch.setattr(engine, "transcribe", lambda audio: "il polpo ha")

    eng = engine.Engine()
    for _ in range(2):
        eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
        _drain_partials(eng)
    assert engine.AUTOSEND_PAUSE_FLAG.exists()

    eng._finalize(eng._t_start + engine.MIN_DUR + 0.1)
    _drain_finals(eng)

    assert not engine.AUTOSEND_PAUSE_FLAG.exists(), "the daemon must be freed after the paste"


def test_no_hold_when_the_streaming_paste_is_off(engine_state, monkeypatch, typed):
    """Nothing is typed mid-sentence, so nothing needs holding — leave the flag alone."""
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "STREAM_PASTE", False)
    monkeypatch.setattr(engine, "AUTOSEND", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)
    monkeypatch.setattr(engine, "transcribe", lambda audio: "il polpo ha")

    eng = engine.Engine()
    for _ in range(2):
        eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
        _drain_partials(eng)

    assert not engine.AUTOSEND_PAUSE_FLAG.exists()


def test_a_chunk_typed_while_the_sentence_closes_is_not_repeated(engine_state, monkeypatch):
    """The race seen in the wild (2026-08-02): a partial pass had claimed its
    chunk and was still typing when the utterance finalized, so the final paste
    computed its tail from a stale `streamed` and typed the chunk a second time.
    The user's screen read "...funzionando bene. parola.Anche la trascrizione".

    Here `type_text` blocks mid-flight while the utterance is finalized, exactly
    reproducing that window.
    """
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "STREAM_PASTE", True)
    monkeypatch.setattr(engine, "AUTOSEND", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)
    monkeypatch.setattr(engine, "transcribe", lambda audio: "il polpo")

    typing_started = threading.Event()
    may_finish_typing = threading.Event()
    landed: list[str] = []

    def slow_type(text: str) -> bool:
        landed.append(text)
        typing_started.set()
        may_finish_typing.wait(timeout=5)
        return True

    monkeypatch.setattr(engine, "type_text", slow_type)
    monkeypatch.setattr(engine, "autosend", lambda text: landed.append(text))

    eng = engine.Engine()
    # First pass has nothing to agree with yet — let it finish, or the second
    # callback finds the single partial slot busy and silently skips its turn.
    eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
    _drain_partials(eng)
    # Second pass confirms "il polpo" and starts typing it — then blocks.
    eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
    assert typing_started.wait(timeout=5), "the partial pass never reached type_text"

    # The sentence closes while that chunk is still on the wire.
    monkeypatch.setattr(engine, "transcribe", lambda audio: "il polpo ha otto")
    closer = threading.Thread(
        target=eng._finalize, args=(eng._t_start + engine.MIN_DUR + 0.1,), daemon=True
    )
    closer.start()
    time.sleep(0.2)
    may_finish_typing.set()
    closer.join(timeout=5)
    _drain_finals(eng)

    assert "".join(landed) == "il polpo ha otto", f"duplicated: {landed!r}"


def test_engine_and_daemon_agree_on_the_pause_flag_path():
    """Two processes, one path, no imports between them — assert they still match."""
    daemon = (Path(engine.__file__).parent / "autosend.py").read_text()
    assert str(engine.AUTOSEND_PAUSE_FLAG) in daemon or \
        "/tmp/vibevoice_autosend_pause" in daemon


def test_pill_reads_the_partial_file(engine_state):
    """Writer + reader in the same commit: a draft nobody renders is dead code.

    The pill needs AppKit, so it cannot be imported headless — this locks the
    contract at source level, the same way the levels.bin magic number is.
    """
    pill = (Path(engine.__file__).parent / "vibevoice.py").read_text()
    assert "partial.txt" in pill, "the pill must read the live draft"
    # And it must prefer the draft only while the utterance is open, otherwise
    # it would keep showing a stale draft after the real text landed.
    assert "_read_partial" in pill


def test_partial_worker_failure_does_not_break_capture(engine_state, monkeypatch):
    """A partial is a bonus, never a dependency — it may fail silently."""
    monkeypatch.setattr(engine, "STREAMING", True)
    monkeypatch.setattr(engine, "PARTIAL_INTERVAL", 0.0)

    def _boom(audio):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(engine, "transcribe", _boom)

    eng = engine.Engine()
    eng._audio_callback(_speech_block(), engine.BLOCKSIZE, None, None)
    _drain_partials(eng)

    assert eng._speaking is True
    assert engine.STATE_FILE.read_text() == "recording"
    assert not engine.PARTIAL_FILE.exists()
