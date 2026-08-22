#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Who lives, who dies, and who comes back — the process lifecycle.

Two defects of the same family, found 2026-08-06 while the user was trying to
close the app and could not:

1. The pill identified its children by NAME (`pgrep -f engine.py`), which is a
   substring match against every process's full argv. Any stranger carrying
   that string answered for them.
2. The LaunchAgent templates said `KeepAlive: true`, which relaunches whatever
   the exit code — so the menu's own "Quit (close everything)" could not close
   anything.
"""
from __future__ import annotations

import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ── 1. a name is not an identity ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def pill():
    """vibevoice.py, or skip: it needs PyObjC, which CI-less machines may lack."""
    return pytest.importorskip("vibevoice")


def test_a_stranger_named_engine_py_is_not_our_engine(pill):
    """The bug, reproduced: an unrelated process with engine.py in its argv.

    `pgrep -f engine.py` matched it, so the pill believed its engine was alive
    and refused to start one — and `_stop_engine`'s `pkill -f engine.py` would
    have killed the stranger instead. Neither is the pill's process to reason
    about.
    """
    stranger = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)",
         "/tmp/not-our-project/engine.py"]
    )
    try:
        # It really is visible under the old, unanchored pattern — otherwise
        # this test would pass for the wrong reason.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            r = subprocess.run(["pgrep", "-f", "engine.py"],
                               capture_output=True, text=True)
            if str(stranger.pid) in r.stdout.split():
                break
            time.sleep(0.05)
        else:
            pytest.skip("could not stage a stranger process")

        assert not pill._engine_running(), (
            "the pill mistook an unrelated engine.py for its own"
        )
    finally:
        stranger.kill()
        stranger.wait()


def test_our_own_engine_is_found(pill):
    """The other half, and the one that would fail silently.

    "Does not match a stranger" is satisfied by a probe that matches NOTHING —
    which is exactly what an unescaped path does the moment it contains a regex
    metacharacter. So assert the positive directly: a process carrying the real
    ENGINE_PATH in its argv must be seen.
    """
    ours = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)", str(pill.ENGINE_PATH)]
    )
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if pill._engine_running():
                return
            time.sleep(0.05)
        pytest.fail("the pill cannot see its own engine")
    finally:
        ours.kill()
        ours.wait()


@pytest.mark.parametrize("directory", [
    "vibe(voice)", "vibe+voice", "vibe[1]", "a.b*c", "n^1$", "due parole",
    "💼-prodotti",
])
def test_a_path_with_regex_characters_still_finds_its_engine(pill, monkeypatch,
                                                             tmp_path, directory):
    """pgrep/pkill take an extended regex, and the repo path is user-chosen.

    Raw, `~/projects/vibe(voice)/engine.py` is a regex with a capture group that
    matches nothing: the pill decides its engine is dead and every toggle spawns
    another one. All seven of these fail unescaped (verified 2026-08-06).
    """
    engine_path = tmp_path / directory / "engine.py"
    engine_path.parent.mkdir(parents=True)
    monkeypatch.setattr(pill, "ENGINE_PATH", engine_path)
    ours = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)", str(engine_path)]
    )
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if pill._engine_running():
                return
            time.sleep(0.05)
        pytest.fail(f"engine invisible under a path containing {directory!r}")
    finally:
        ours.kill()
        ours.wait()


@pytest.mark.parametrize("func, path_attr", [
    ("_engine_running", "ENGINE_PATH"),
    ("_autosend_running", "AUTOSEND_PATH"),
])
def test_process_probes_match_on_the_absolute_path(pill, monkeypatch, func, path_attr):
    seen = []
    monkeypatch.setattr(pill.subprocess, "run",
                        lambda argv, **kw: seen.append(argv) or
                        subprocess.CompletedProcess(argv, 1))
    getattr(pill, func)()
    assert seen, f"{func} ran no probe"
    assert seen[0][-1] == pill._argv_pattern(getattr(pill, path_attr)), (
        f"{func} still matches by name, not by escaped path: {seen[0]}"
    )


def test_stop_engine_kills_by_absolute_path(pill, monkeypatch):
    """The dangerous half: pkill acts, it does not merely observe."""
    seen = []
    monkeypatch.setattr(pill.subprocess, "Popen",
                        lambda argv, **kw: seen.append(argv))
    pill._stop_engine()
    assert seen and seen[0][-1] == pill._argv_pattern(pill.ENGINE_PATH), (
        f"pkill would still fire at every process named engine.py: {seen}"
    )


# ── 2. a deliberate quit must stay quit ──────────────────────────────────────

@pytest.mark.parametrize("name", [
    "com.vibevoice.pill.plist",
    "com.vibevoice.autosend.plist",
])
def test_launchagent_restarts_on_crash_but_not_on_quit(name):
    """`KeepAlive: true` made "Quit (close everything)" a lie.

    NSApp.terminate_ exits 0; launchd brought the pill straight back, and with
    VIBEVOICE_ENGINE_AUTOSTART=1 the engine with it. The only way out was
    `launchctl bootout`, which no user should have to discover.
    """
    plist = plistlib.loads((REPO / name).read_bytes())
    keep = plist["KeepAlive"]
    assert isinstance(keep, dict), (
        "KeepAlive: true relaunches a clean exit — the app cannot be quit"
    )
    assert keep["SuccessfulExit"] is False, (
        "crash-restart must stay; clean-exit-restart must not"
    )


@pytest.mark.parametrize("name", [
    "com.vibevoice.pill.plist",
    "com.vibevoice.autosend.plist",
])
def test_launchagent_templates_are_valid_plists(name):
    r = subprocess.run(["plutil", "-lint", str(REPO / name)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
