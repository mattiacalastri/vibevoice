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
