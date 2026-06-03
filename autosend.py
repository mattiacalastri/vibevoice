#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# VibeVoice — MIT
#
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
# -----------------------------------------------------------------------------
"""
autosend.py — auto-press Return after dictation settles, for macOS

Listens to global keystrokes via pynput. When a target app (a terminal, an
editor, a chat box) is frontmost and typing goes quiet for AUTO_SEND_DELAY
seconds, it simulates a Return — so a sentence you dictated gets *sent*
without you reaching for the keyboard.

ONE-SHOT by design. You arm it, you dictate one message, the first Return
fires automatically, then it disarms itself. This prevents a "zombie ON"
state from pressing Return while you later type by hand.

  Cmd+Shift+Space  → toggle arm/disarm
                     "tink" = armed, "submarine" = disarmed
                     a desktop notification confirms when armed

It is decoupled from the rest of VibeVoice: it shares nothing with the engine
except the optional pause flag (see below). It can run standalone with any STT.

STATE-FILE CONTRACT (under ~/.vibevoice/):
  ~/.vibevoice/autosend    text file, "on" | "off" (armed state)

OPTIONAL PAUSE HOOK:
  Write a unix timestamp into /tmp/vibevoice_autosend_pause to suspend
  auto-sending for up to PAUSE_TTL_SECONDS (anti-deadlock auto-clears the
  flag once it ages out). Useful when an external tool opens a modal/dialog
  and you don't want a stray Return. Delete the file to resume immediately.

Usage:
  python3 autosend.py
  python3 autosend.py --delay 3.0

Requires: pip3 install pynput
          Accessibility permission for your terminal / launching app
          (System Settings -> Privacy & Security -> Accessibility -> ... )
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

AUTO_SEND_DELAY = 0.8  # seconds of typing silence -> auto Return

# Apps where auto-send is allowed. NSWorkspace returns the display name;
# osascript sees the process name (e.g. Electron-based editors). Both are
# matched as substrings, so add whatever you dictate into.
TARGET_APPS = {
    "Terminal", "iTerm2", "iTerm", "Hyper", "Warp", "Ghostty",
    "Electron", "Code",
}

STATE_DIR = Path(os.path.expanduser("~/.vibevoice"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "autosend"        # "on" | "off"

# External tools can suspend auto-send by writing a unix timestamp here.
# TTL safety auto-clears the flag if it ages out (anti-deadlock).
PAUSE_FLAG = Path("/tmp/vibevoice_autosend_pause")
PAUSE_TTL_SECONDS = 60.0


def is_paused_by_flag() -> bool:
    """True if the pause flag is present and not yet expired.
    Auto-cleans the flag once TTL is exceeded (anti-deadlock)."""
    try:
        if not PAUSE_FLAG.exists():
            return False
        try:
            ts = float(PAUSE_FLAG.read_text().strip() or "0")
        except (ValueError, OSError):
            ts = 0.0
        age = time.time() - ts
        if age > PAUSE_TTL_SECONDS:
            try:
                PAUSE_FLAG.unlink(missing_ok=True)
                print(f"[autosend] pause TTL expired ({age:.1f}s) — flag cleared", flush=True)
            except Exception:
                pass
            return False
        return True
    except Exception:
        return False


try:
    from pynput import keyboard
except ImportError:
    print("[autosend] pynput not found. Run: pip3 install pynput", file=sys.stderr)
    sys.exit(2)

try:
    from AppKit import NSWorkspace
    HAS_APPKIT = True
except ImportError:
    HAS_APPKIT = False
    print("[autosend] AppKit unavailable — frontmost-app check disabled", file=sys.stderr)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    try:
        return STATE_FILE.read_text().strip() == "on"
    except FileNotFoundError:
        STATE_FILE.write_text("on")
        return True


def set_enabled(v: bool) -> None:
    STATE_FILE.write_text("on" if v else "off")


def is_target_app() -> bool:
    if not HAS_APPKIT:
        return True
    try:
        info = NSWorkspace.sharedWorkspace().activeApplication()
        app_name = info.get("NSApplicationName", "")
        return any(t in app_name for t in TARGET_APPS)
    except Exception:
        return False


def afplay_sound(sound: str) -> None:
    path = f"/System/Library/Sounds/{sound}.aiff"
    subprocess.Popen(
        ["afplay", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def simulate_return():
    """Simulate the Return key via osascript. Returns CompletedProcess so the
    caller can check for errors."""
    return subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to key code 36'],
        capture_output=True,
        text=True,
    )


def get_frontmost_signature() -> str:
    """A stable signature of the frontmost window.

    Uses the window id (an immutable numeric handle) for Terminal/iTerm2
    instead of the title — titles mutate as the shell updates them, which
    would cause false "window changed" skips.
    """
    # Step 1: detect app name
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to name of (first process whose frontmost is true)'],
            capture_output=True, text=True, timeout=1.0,
        )
        app_name = (r.stdout or "").strip()
    except Exception:
        return ""
    if not app_name:
        return ""

    # Step 2: window id (immutable) for terminal-class apps
    wid_script = None
    if app_name == "Terminal":
        wid_script = 'tell application "Terminal" to id of front window'
    elif app_name in ("iTerm2", "iTerm"):
        wid_script = 'tell application "iTerm2" to id of current window'
    elif app_name == "Ghostty":
        wid_script = ('tell application "System Events" to tell process "Ghostty" '
                      'to (position of front window) as string')
    elif app_name == "Warp":
        wid_script = ('tell application "System Events" to tell process "Warp" '
                      'to (position of front window) as string')
    elif app_name == "Electron":
        # Electron-based editors update their window title constantly; the
        # window position is stable within the sub-second auto-send window.
        wid_script = ('tell application "System Events" to tell process "Electron" '
                      'to (position of front window) as string')
    if wid_script:
        try:
            r = subprocess.run(
                ["osascript", "-e", wid_script],
                capture_output=True, text=True, timeout=1.0,
            )
            wid = (r.stdout or "").strip()
            if wid:
                return f"{app_name}::wid::{wid}"
        except Exception:
            pass

    # Step 3: fall back to the title for other apps (Hyper, etc.)
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events"\n'
             '  set fp to first process whose frontmost is true\n'
             '  try\n'
             '    set wn to name of front window of fp\n'
             '  on error\n'
             '    set wn to ""\n'
             '  end try\n'
             '  return wn\n'
             'end tell'],
            capture_output=True, text=True, timeout=1.0,
        )
        win_title = (r.stdout or "").strip()
        # Sanitize: keep only a stable prefix to avoid drift on shell updates
        return f"{app_name}::title::{win_title[:30]}"
    except Exception:
        return f"{app_name}::"


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class AutoSendDaemon:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._held: set = set()

        # Modifier keys — they do not reset the silence timer
        self._modifiers = {
            keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r,
            keyboard.Key.ctrl,  keyboard.Key.ctrl_l,  keyboard.Key.ctrl_r,
            keyboard.Key.alt,   keyboard.Key.alt_l,   keyboard.Key.alt_r,
            keyboard.Key.cmd,   keyboard.Key.cmd_l,   keyboard.Key.cmd_r,
        }

    def _has_cmd(self) -> bool:
        return any(k in self._held for k in (keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r))

    def _has_shift(self) -> bool:
        return any(k in self._held for k in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r))

    def _cancel_timer(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def _schedule_send(self) -> None:
        # Snapshot the frontmost window signature (lock at window level, not
        # just app level, so we don't fire into a different window).
        snap_sig = get_frontmost_signature()
        try:
            snap_app = NSWorkspace.sharedWorkspace().activeApplication().get("NSApplicationName", "") if HAS_APPKIT else ""
        except Exception:
            snap_app = ""
        with self._lock:
            if self._timer:
                self._timer.cancel()
            t = threading.Timer(self.delay, self._fire, args=(snap_app, snap_sig))
            t.daemon = True
            t.start()
            self._timer = t

    def _fire(self, scheduled_app: str = "", scheduled_sig: str = "") -> None:
        if not is_enabled():
            return
        # Suspend if an external tool raised the pause flag
        if is_paused_by_flag():
            print("[autosend] skip — paused by flag", flush=True)
            return
        # Verify window signature (lock at window level)
        current_sig = get_frontmost_signature()
        if scheduled_sig and current_sig and current_sig != scheduled_sig:
            print(f"[autosend] skip — window changed ({scheduled_sig} → {current_sig})", flush=True)
            return
        # Fallback: verify frontmost app
        try:
            current = NSWorkspace.sharedWorkspace().activeApplication().get("NSApplicationName", "") if HAS_APPKIT else scheduled_app
        except Exception:
            current = scheduled_app
        if scheduled_app and current != scheduled_app:
            print(f"[autosend] skip — app changed ({scheduled_app} → {current})", flush=True)
            return
        result = simulate_return()
        if result.returncode != 0:
            print(f"[autosend] osascript error: {result.stderr.strip()}", flush=True)
        else:
            # ONE-SHOT: after the Return fires, disarm automatically. You arm
            # it only for a dictated message; auto-off prevents a zombie ON
            # state from sending while you type manually afterwards.
            set_enabled(False)
            afplay_sound("submarine")
            print(f"[autosend] → Return sent ({scheduled_app}) | one-shot consumed → OFF", flush=True)

    def on_press(self, key) -> None:
        # Track modifiers
        if key in self._modifiers:
            self._held.add(key)
            return

        # Toggle: Cmd+Shift+Space
        if key == keyboard.Key.space and self._has_cmd() and self._has_shift():
            enabled = not is_enabled()
            set_enabled(enabled)
            afplay_sound("tink" if enabled else "submarine")
            # Desktop notification when armed (one-shot visibility)
            if enabled:
                subprocess.Popen(
                    ["osascript", "-e",
                     'display notification "Dictate now — first Return fires automatically, then OFF" '
                     'with title "🎙️  Autosend ARMED" sound name "Tink"'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            print(f"[autosend] {'ON ✓ (one-shot armed)' if enabled else 'OFF ✗'}", flush=True)
            return

        # Manual Return → cancel the pending timer (you already sent)
        if key in (keyboard.Key.enter,):
            self._cancel_timer()
            return

        # Escape → cancel the pending timer (you want to abort)
        if key == keyboard.Key.esc:
            self._cancel_timer()
            return

        # Not in a target app, or disarmed → ignore
        if not is_enabled() or not is_target_app():
            return

        # Any other key → reset the silence timer
        self._schedule_send()

    def on_release(self, key) -> None:
        self._held.discard(key)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="VibeVoice auto-send daemon")
    parser.add_argument(
        "--delay", type=float, default=AUTO_SEND_DELAY,
        help=f"Seconds of silence → automatic Return (default {AUTO_SEND_DELAY})"
    )
    args = parser.parse_args()

    daemon = AutoSendDaemon(delay=args.delay)
    state = "ON" if is_enabled() else "OFF"
    print(
        f"[autosend] started | delay={args.delay}s | state={state} | "
        f"toggle=Cmd+Shift+Space | ctrl+c to quit",
        flush=True,
    )

    with keyboard.Listener(
        on_press=daemon.on_press,
        on_release=daemon.on_release,
    ) as listener:
        listener.join()


if __name__ == "__main__":
    main()
