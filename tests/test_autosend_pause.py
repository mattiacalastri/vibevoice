#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The auto-Return daemon across the engine's pause flag.

The engine raises `/tmp/vibevoice_autosend_pause` while the streaming paste is
typing and lowers it once the sentence is whole. The daemon fires Return after
0.8s of typing silence, so the two meet on every dictated sentence — and the
meeting used to go badly in both directions at once.
"""
from __future__ import annotations

import threading
import time

import pytest

import autosend


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """A daemon whose flags live in tmp and whose Return is only recorded."""
    monkeypatch.setattr(autosend, "PAUSE_FLAG", tmp_path / "pause")
    monkeypatch.setattr(autosend, "STATE_FILE", tmp_path / "autosend", raising=False)
    fired: list[float] = []

    class _Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(autosend, "simulate_return",
                        lambda *a, **k: fired.append(time.monotonic()) or _Result())
    monkeypatch.setattr(autosend, "afplay_sound", lambda *a, **k: None)
    monkeypatch.setattr(autosend, "is_enabled", lambda: True)
    monkeypatch.setattr(autosend, "get_frontmost_signature", lambda: "")

    disarmed: list[bool] = []
    monkeypatch.setattr(autosend, "set_enabled", lambda v: disarmed.append(v))

    d = autosend.AutoSendDaemon.__new__(autosend.AutoSendDaemon)
    d._lock = threading.Lock()
    d._timer = None
    d.delay = 0.05
    return d, fired, disarmed


def _hold(tmp_flag, owner: str = "vibevoice-engine") -> None:
    """Raise the flag the way the engine does: timestamp, then the owner."""
    tmp_flag.write_text(f"{time.time()}\n{owner}\n")


def test_an_owned_hold_is_still_read_as_a_hold(tmp_path, monkeypatch):
    """The owner line must not make a live hold look expired.

    Parsing the whole file raises on the owner name, which lands in the
    fallback with ts=0 — the hold then looks an epoch old, gets deleted as
    stale, and the Return it existed to prevent fires anyway.
    """
    monkeypatch.setattr(autosend, "PAUSE_FLAG", tmp_path / "pause")
    _hold(tmp_path / "pause")

    assert autosend.is_paused_by_flag() is True
    assert (tmp_path / "pause").exists(), "a live hold must not be cleared"


def test_the_engine_does_not_revoke_someone_else_s_hold(tmp_path, monkeypatch):
    """The flag is co-owned by contract: an external tool raises it to protect a
    modal dialog. An unconditional unlink would let a Return into that dialog."""
    import engine
    monkeypatch.setattr(engine, "AUTOSEND_PAUSE_FLAG", tmp_path / "pause")
    _hold(tmp_path / "pause", owner="qualche-altro-strumento")

    engine.release_autosend()
    assert (tmp_path / "pause").exists(), "not ours to revoke"

    _hold(tmp_path / "pause")            # our own hold
    engine.release_autosend()
    assert not (tmp_path / "pause").exists()


def test_a_paused_daemon_waits_instead_of_dropping_the_return(daemon, tmp_path):
    """The Return belongs to the sentence just dictated. Dropping it loses that
    sentence's send AND leaves the one-shot armed, so it later fires into
    whatever the user types by hand — silently wrong twice over.
    """
    d, fired, disarmed = daemon
    _hold(tmp_path / "pause")

    d._fire()
    assert fired == [], "must not press Return while the paste is still typing"

    (tmp_path / "pause").unlink()          # the sentence is whole
    deadline = time.monotonic() + 3
    while not fired and time.monotonic() < deadline:
        time.sleep(0.02)

    assert fired, "the Return must arrive once the pause lifts"
    assert disarmed == [False], "and the one-shot must be consumed, not left armed"


def test_a_pause_that_never_lifts_disarms_instead_of_lurking(daemon, tmp_path, monkeypatch):
    """If the flag is stuck, the daemon must not stay armed forever waiting: a
    zombie one-shot fires into the user's own typing much later."""
    d, fired, disarmed = daemon
    monkeypatch.setattr(autosend.AutoSendDaemon, "PAUSE_RETRY", 0.02)
    monkeypatch.setattr(autosend.AutoSendDaemon, "PAUSE_MAX_WAIT", 0.05)
    _hold(tmp_path / "pause")

    d._fire()
    deadline = time.monotonic() + 3
    while not disarmed and time.monotonic() < deadline:
        time.sleep(0.02)

    assert fired == [], "never press Return into a pause that never lifted"
    assert disarmed == [False], "but do disarm, so nothing fires later by surprise"
