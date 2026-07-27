#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Contract tests for the dictation-quality layer: the personal dictionary that
biases Whisper decoding via `initial_prompt`.

Like test_contract.py these exercise the real engine module headless — no mic,
no model download — and redirect every runtime file into tmp_path so the live
~/.vibevoice/ is never touched.
"""
from __future__ import annotations

import numpy as np
import pytest

import engine


@pytest.fixture
def quality_state(tmp_path, monkeypatch):
    """Redirect the dictation-quality runtime files into a tmp dir."""
    monkeypatch.setattr(engine, "DICT_FILE", tmp_path / "dictionary.txt")
    monkeypatch.setattr(engine, "RAW_FILE", tmp_path / "raw.txt")
    monkeypatch.setattr(engine, "HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr(engine, "METRICS_FILE", tmp_path / "metrics.jsonl")
    return tmp_path


class _FakeWhisper:
    """Stand-in for the mlx_whisper module: records transcribe() kwargs."""

    def __init__(self, text: str = " ciao mondo "):
        self.text = text
        self.calls: list[dict] = []

    def transcribe(self, wav_path, **kwargs):
        self.calls.append(kwargs)
        return {"text": self.text}


@pytest.fixture
def fake_whisper(monkeypatch):
    fake = _FakeWhisper()
    monkeypatch.setattr(engine, "_MLX_WHISPER", fake)
    monkeypatch.setattr(engine, "_MLX_AVAILABLE", True)
    return fake


# ── load_dictionary ───────────────────────────────────────────────────────────

def test_load_dictionary_reads_terms_skipping_comments_and_blanks(quality_state):
    engine.DICT_FILE.write_text(
        "# clienti\nKongline\n\nFathom\n  GoHighLevel  \n# fine\n"
    )
    assert engine.load_dictionary() == ["Kongline", "Fathom", "GoHighLevel"]


def test_load_dictionary_missing_file_is_empty(quality_state):
    assert engine.load_dictionary() == []


def test_load_dictionary_caps_terms(quality_state):
    engine.DICT_FILE.write_text("\n".join(f"termine{i}" for i in range(500)))
    assert len(engine.load_dictionary()) == engine.DICT_MAX_TERMS


# ── transcribe() biasing ──────────────────────────────────────────────────────

def test_transcribe_passes_dictionary_as_initial_prompt(quality_state, fake_whisper):
    engine.DICT_FILE.write_text("Kongline\nFathom\n")
    audio = np.zeros(1600, dtype=np.float32)

    text = engine.transcribe(audio)

    assert text == "ciao mondo"
    assert len(fake_whisper.calls) == 1
    prompt = fake_whisper.calls[0].get("initial_prompt")
    assert prompt is not None
    assert "Kongline" in prompt and "Fathom" in prompt


def test_transcribe_without_dictionary_omits_initial_prompt(quality_state, fake_whisper):
    audio = np.zeros(1600, dtype=np.float32)

    engine.transcribe(audio)

    assert len(fake_whisper.calls) == 1
    assert fake_whisper.calls[0].get("initial_prompt") is None


def test_transcribe_survives_unreadable_dictionary(quality_state, fake_whisper, monkeypatch):
    """A broken dictionary must never break transcription (degradation is contract)."""
    monkeypatch.setattr(
        engine, "load_dictionary", lambda: (_ for _ in ()).throw(OSError("boom"))
    )
    audio = np.zeros(1600, dtype=np.float32)

    assert engine.transcribe(audio) == "ciao mondo"


# ── metrics.jsonl: the latency telemetry ─────────────────────────────────────

def test_append_metrics_caps_lines(quality_state):
    import json

    for i in range(engine.METRICS_MAX + 30):
        engine._append_metrics({"i": i})

    lines = engine.METRICS_FILE.read_text().splitlines()
    assert len(lines) == engine.METRICS_MAX
    assert json.loads(lines[-1])["i"] == engine.METRICS_MAX + 29  # newest last


def test_process_utterance_writes_metrics_with_latency_fields(quality_state, monkeypatch):
    import json
    import time as _time

    monkeypatch.setattr(engine, "transcribe", lambda audio: "ciao mondo")
    audio = np.zeros(engine.SAMPLE_RATE, dtype=np.float32)  # 1s of audio

    # t_end is on the audio loop's clock (time.monotonic), not time.time().
    text = engine.process_utterance(audio, t_end=_time.monotonic() - 0.5)

    assert text == "ciao mondo"
    assert engine.RAW_FILE.read_text() == "ciao mondo"
    assert "ciao mondo" in engine.HISTORY_FILE.read_text()
    entry = json.loads(engine.METRICS_FILE.read_text().splitlines()[-1])
    assert entry["audio_s"] == pytest.approx(1.0)
    assert entry["chars"] == len("ciao mondo")
    assert entry["stt_ms"] >= 0
    assert entry["total_ms"] >= 500  # end-of-speech was 0.5s ago


def test_process_utterance_empty_transcription_writes_nothing(quality_state, monkeypatch):
    monkeypatch.setattr(engine, "transcribe", lambda audio: "")
    audio = np.zeros(1600, dtype=np.float32)

    assert engine.process_utterance(audio, t_end=None) == ""
    assert not engine.RAW_FILE.exists()
    assert not engine.METRICS_FILE.exists()
