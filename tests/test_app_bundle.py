#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Bundle tests for VibeVoice.

These build a real VibeVoice.app via build_app.sh into a tmp dir and lock down
its shape: the bundle layout, the Info.plist identity + usage strings (without
which macOS silently denies the mic), and a launcher that is executable, valid
bash, and runs the pill from Resources. They run headless — the app is assembled
and inspected, never launched (launching would open the mic/GUI).

Run:  pytest -q
"""
from __future__ import annotations

import plistlib
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build_app.sh"


@pytest.fixture(scope="module")
def app(tmp_path_factory) -> Path:
    """Build VibeVoice.app once into a tmp dir; never touch the real ./dist."""
    out = tmp_path_factory.mktemp("vv_dist")
    subprocess.run(["bash", str(BUILD), str(out)], check=True,
                   capture_output=True, text=True)
    bundle = out / "VibeVoice.app"
    assert bundle.is_dir(), "build_app.sh did not produce VibeVoice.app"
    return bundle


# ── bundle layout ─────────────────────────────────────────────────────────────

def test_bundle_layout(app):
    """The .app has the canonical macOS skeleton + self-contained sources."""
    assert (app / "Contents" / "Info.plist").is_file()
    assert (app / "Contents" / "MacOS" / "VibeVoice").is_file()
    assert (app / "Contents" / "PkgInfo").is_file()
    res = app / "Contents" / "Resources"
    for name in ("vibevoice.py", "engine.py", "autosend.py", "requirements.txt"):
        assert (res / name).is_file(), f"missing bundled resource: {name}"


# ── Info.plist identity + usage strings ───────────────────────────────────────

def test_infoplist_is_valid_and_identified(app):
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "com.vibevoice.app"
    assert info["CFBundleExecutable"] == "VibeVoice"
    assert info["CFBundlePackageType"] == "APPL"


def test_infoplist_is_accessory(app):
    """LSUIElement must match the app's NSApplicationActivationPolicyAccessory."""
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["LSUIElement"] is True


def test_infoplist_has_permission_strings(app):
    """No usage string => macOS denies the mic/AppleEvents prompt silently."""
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info.get("NSMicrophoneUsageDescription", "").strip()
    assert info.get("NSAppleEventsUsageDescription", "").strip()


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS-only")
def test_infoplist_passes_plutil(app):
    r = subprocess.run(["plutil", "-lint", str(app / "Contents" / "Info.plist")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ── launcher ──────────────────────────────────────────────────────────────────

def test_launcher_is_executable_bash(app):
    launcher = app / "Contents" / "MacOS" / "VibeVoice"
    assert launcher.read_text().startswith("#!/bin/bash")
    assert launcher.stat().st_mode & stat.S_IXUSR, "launcher is not executable"
    # Valid bash — catches a generated-heredoc syntax error before a user double-clicks.
    subprocess.run(["bash", "-n", str(launcher)], check=True,
                   capture_output=True, text=True)


def test_launcher_runs_the_pill_all_in_one(app):
    """The launcher execs the pill from Resources with autostart on."""
    body = (app / "Contents" / "MacOS" / "VibeVoice").read_text()
    assert 'exec "$PY" "$RES/vibevoice.py"' in body
    assert "VIBEVOICE_ENGINE_AUTOSTART" in body
