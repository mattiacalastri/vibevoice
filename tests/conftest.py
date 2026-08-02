#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Session-wide safety net for the VibeVoice suite.

Two problems, one cause: the engine finishes its work on **daemon threads**, so
a worker can outlive the test that started it.

1. It can reach the real keyboard. `autosend()` pastes via the clipboard and
   `type_text()` posts synthetic keystrokes into whatever app is frontmost —
   which is exactly what happened on 2026-08-02, when the streaming-paste tests
   typed "il polpo ha" into the user's screen and overwrote their clipboard.
2. It can land in the NEXT test's recorder. A leaked worker running the real
   Whisper on a buffer of zeros (which hallucinates "Grazie a tutti.") called
   `engine.autosend` while a later test in another file had monkeypatched it,
   and that test failed with a paste it never made.

Both disappear if the real outbound functions are simply never installed during
a test session. A test that wants to observe a paste still monkeypatches them
per-test: function-scoped `monkeypatch` layers on top of these no-ops and
reverts to the no-ops, never to the real thing.
"""
from __future__ import annotations

import threading
import time

import pytest

import engine


@pytest.fixture(autouse=True, scope="session")
def _no_outbound_effects_during_tests():
    """Replace the two functions that reach outside the process, for the whole run."""
    patch = pytest.MonkeyPatch()
    patch.setattr(engine, "autosend", lambda text: None)
    patch.setattr(engine, "type_text", lambda text: True)
    yield
    patch.undo()


# How long to wait for a test's own threads before giving up and moving on.
# Generous: a real transcription on this machine is ~200 ms.
_THREAD_SETTLE_TIMEOUT = 8.0


@pytest.fixture(autouse=True)
def _let_each_test_take_its_threads_with_it():
    """No test may finish while the threads it started are still running.

    The session no-ops above are not enough on their own: while a test has
    monkeypatched `engine.autosend` with its own recorder, a worker leaked by an
    EARLIER test calls that recorder, not the no-op. That is how a transcription
    of silence ("Grazie a tutti.", Whisper's hallucination on a zero buffer)
    from `test_contract.py` turned up inside a paste-ordering assertion in
    `test_dictation_quality.py` — a failure with no relationship to the code
    under test, and reproducible only in a full-suite run.

    So the leak is closed at the source. Not asserted, only waited for: a hung
    worker should slow the suite down, not fail an unrelated test.
    """
    before = set(threading.enumerate())
    yield
    deadline = time.monotonic() + _THREAD_SETTLE_TIMEOUT
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if t not in before and t.is_alive()]:
            return
        time.sleep(0.02)
