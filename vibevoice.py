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
vibevoice.py — VibeVoice "Dynamic Island" STT pill for macOS

A minimal floating UI for a speech-to-text engine. A borderless, floating,
non-activating NSPanel docked under the notch. Decoupled from the engine: it
READS the state files written by the engine and draws. It never touches the
audio pipeline directly.

  idle          → invisible (alpha 0)
  recording     → fade-in, live waveform
  transcribing  → keeps drawing the transcript that streams in
  silence       → fade-out after ~1.5s (or ~2.5s while text is shown)

STATE-FILE CONTRACT (shared pill <-> engine, under ~/.vibevoice/):
  ~/.vibevoice/state       text file, one of: idle | recording | transcribing
  ~/.vibevoice/levels.bin  60 float32 little-endian (RMS 0..1), atomic write
  ~/.vibevoice/raw.txt     last transcription, plain text (just the sentence)

The engine WRITES these files; the pill READS them.

CONTROL FILES (this pill WRITES them; the engine / this pill honor them — the
same external-control pattern as autosend's pause flag, NOT engine-owned state):
  ~/.vibevoice/muted       presence = mic paused (engine ignores audio, stays alive)
  ~/.vibevoice/locked      presence = pill stays visible (no auto-hide)

Menu bar icon (🎙/🔇) is always present and acts as the master switch:
  - click toggles the engine (launches/kills engine.py via subprocess)
  - "🔇 Mute mic" pauses the mic without killing the engine
  - "🔒 Keep pill visible" pins the pill so it never auto-hides
  - "Quit" stops everything
The pill also draws clickable 🔇 (red) and 🔒 (amber) icons next to the ✕.

Run:
  python3 vibevoice.py            # live (reads the engine state files)
  python3 vibevoice.py --demo     # animated demo (to preview the design)
  python3 vibevoice.py --place    # placement mode (stays visible)
"""
from __future__ import annotations

import argparse
import math
import os
import random
import struct
import subprocess
import sys
import time
from pathlib import Path

import objc

from AppKit import (
    NSApplication, NSApp, NSPanel, NSView, NSColor, NSBezierPath, NSAnimationContext,
    NSScreen, NSTimer, NSFont, NSForegroundColorAttributeName,
    NSFontAttributeName, NSMakeRect, NSMakePoint,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
    NSBackingStoreBuffered, NSStatusWindowLevel,
    NSApplicationActivationPolicyAccessory,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSString, NSPasteboard, NSPasteboardTypeString,
    NSStatusBar, NSMenu, NSMenuItem, NSVariableStatusItemLength,
)
from Foundation import NSObject

# ── state-file contract (under $HOME/.vibevoice) ──────────────────────────────
STATE_DIR  = Path(os.path.expanduser("~/.vibevoice"))
STATE_FILE = STATE_DIR / "state"        # idle | recording | transcribing
LEVELS_BIN = STATE_DIR / "levels.bin"   # 60 float32 LE (RMS 0..1)
RAW_TXT    = STATE_DIR / "raw.txt"       # last transcription (plain text)
# control files (the pill writes these; not engine-owned state):
MUTED_FILE  = STATE_DIR / "muted"        # presence = mic paused (engine reads, stays alive)
LOCKED_FILE = STATE_DIR / "locked"       # presence = pill stays visible (pill-only, no auto-hide)


def _flag_on(path) -> bool:
    """True if a control flag file exists (defensive: never raises)."""
    try:
        return path.exists()
    except Exception:
        return False


def _toggle_flag(path) -> None:
    """Create the flag if absent, remove it if present. Errors are swallowed."""
    try:
        if path.exists():
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
    except Exception:
        pass

# engine.py lives next to this file
ENGINE_PATH = Path(os.path.abspath(__file__)).parent / "engine.py"

# ── design ────────────────────────────────────────────────────────────────────
N_BARS       = 32
PILL_W       = 460.0
PILL_H       = 110.0
PILL_RADIUS  = 22.0
GAIN         = 12.0     # waveform sensitivity: higher = more reactive
VOICE_THRESH = 0.018    # raw RMS onset threshold (floor ~0.005 < thresh < speech ~0.05)

FADE_STEP   = 0.18           # alpha per tick (fade in/out)
IDLE_HIDE_S = 1.5            # seconds of silence before fade-out
TICK        = 1.0 / 24.0     # ~24 fps

MATRIX = (0.12, 1.00, 0.32)  # Matrix-terminal flúor green (#1fff52)
MUTE_RED   = (1.00, 0.27, 0.23)   # 🔇 active (mic paused)
LOCK_AMBER = (1.00, 0.74, 0.20)   # 🔒 active (pill pinned visible)

_CTRL  = None   # strong refs (avoid GC of controller/timer)
_TIMER = None


def _ensure_state_dir():
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _pill_path(w, h, r):
    """Path with a SQUARE top (flush with the notch) and a ROUNDED bottom — it
    looks like the black notch rectangle extending downward."""
    r = min(r, h / 2.0, w / 2.0)
    p = NSBezierPath.bezierPath()
    p.moveToPoint_(NSMakePoint(0.0, h))                 # top-left square
    p.lineToPoint_(NSMakePoint(w, h))                   # top-right square
    p.lineToPoint_(NSMakePoint(w, r))                   # down the right edge
    p.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
        NSMakePoint(w - r, r), r, 0.0, -90.0, True)     # bottom-right corner
    p.lineToPoint_(NSMakePoint(r, 0.0))                 # along the bottom
    p.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
        NSMakePoint(r, r), r, -90.0, 180.0, True)       # bottom-left corner
    p.closePath()
    return p


class PillView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(PillView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.levels = [0.04] * N_BARS
        self.text = ""
        self.active = False
        self.phase = 0.0
        self.copied_flash = 0.0
        self.hover_x = False
        self.hover_c = False
        self.hover_m = False        # hover over 🔇 mute icon
        self.hover_l = False        # hover over 🔒 lock icon
        self.muted = False          # mic paused (red)
        self.locked = False         # pill pinned visible (amber)
        self.copy_rect = None
        self.mute_rect = None       # click target for mute
        self.lock_rect = None       # click target for lock
        return self

    # ── click on the ✕ (top-right) → stop engine; ⧉ (inline) → copy sentence ──
    def mouseDown_(self, event):
        b = self.bounds()
        loc = event.locationInWindow()   # view fills the window → coords match
        w, h = b.size.width, b.size.height
        # ✕ STOP (top-right) → kills the engine, pill stays as a service
        if loc.x >= w - 40.0 and loc.y >= h - 34.0:
            _stop_engine()
            return
        # 🔇 MUTE (left of the ✕) → pause the mic without killing the engine
        mr = self.mute_rect
        if mr and mr[0] <= loc.x <= mr[0] + mr[2] and mr[1] <= loc.y <= mr[1] + mr[3]:
            _toggle_flag(MUTED_FILE)
            self.muted = _flag_on(MUTED_FILE)
            self.setNeedsDisplay_(True)
            return
        # 🔒 LOCK (between mute and ✕) → keep the pill visible (no auto-hide)
        lr = self.lock_rect
        if lr and lr[0] <= loc.x <= lr[0] + lr[2] and lr[1] <= loc.y <= lr[1] + lr[3]:
            _toggle_flag(LOCKED_FILE)
            self.locked = _flag_on(LOCKED_FILE)
            self.setNeedsDisplay_(True)
            return
        # ⧉ COPY inline (at the end of the text) → re-copy last sentence
        cr = self.copy_rect
        if cr and self.text and cr[0] <= loc.x <= cr[0] + cr[2] and cr[1] <= loc.y <= cr[1] + cr[3]:
            try:
                pb = NSPasteboard.generalPasteboard()
                pb.clearContents()
                pb.setString_forType_(self.text, NSPasteboardTypeString)
                self.copied_flash = time.time()
            except Exception:
                pass

    def setLevels_text_active_(self, levels, text, active):
        self.levels = levels
        self.text = text
        self.active = active
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        b = self.bounds()
        hf = b.size.height
        self.mute_rect = None     # cleared each frame; re-set below when drawn
        self.lock_rect = None
        # ── pure-BLACK background (extension of the notch) ──
        w, h = b.size.width, b.size.height
        bg = _pill_path(w, h, PILL_RADIUS)   # square top, rounded bottom
        NSColor.blackColor().set()           # PURE BLACK — no border, stroke, or shadow
        bg.fill()

        # ── collapsed state (inside the notch): a single green bar ──
        if hf < 34.0:
            NSColor.colorWithCalibratedRed_green_blue_alpha_(MATRIX[0], MATRIX[1], MATRIX[2], 0.85).set()
            NSBezierPath.bezierPathWithRect_(NSMakeRect(b.size.width / 2.0 - 16, hf / 2.0 - 1.5, 32, 3)).fill()
            return

        # ── PIXEL Matrix-terminal waveform: columns of square LEDs, FULL WIDTH ──
        cell, vgap, col_w, cgap = 4.0, 2.0, 5.0, 3.0
        wf_x0 = 24.0                      # left padding
        wf_x1 = b.size.width - 54.0       # space on the right for ✕ / ⧉ icons
        wf_w = wf_x1 - wf_x0
        text_band = 40.0                  # padding below the text
        base_y = text_band + 6.0          # =46 — gap between waveform and text
        top_y = hf - 16.0                 # padding below the top edge
        rows = max(3, int((top_y - base_y) / (cell + vgap)))
        pitch = col_w + cgap
        ncols = max(1, int(wf_w / pitch))    # fill the FULL width (no cap)
        nlv = len(self.levels)
        x = wf_x0
        for i in range(ncols):
            j = min(nlv - 1, int(i * nlv / ncols))
            lv = max(0.0, min(1.0, self.levels[j]))
            lit = int(round(lv * rows))
            for r in range(rows):
                if r < lit:
                    a = (0.55 + 0.45 * (r / max(1, rows))) if self.active else 0.32
                else:
                    a = 0.04
                NSColor.colorWithCalibratedRed_green_blue_alpha_(MATRIX[0], MATRIX[1], MATRIX[2], a).set()
                NSBezierPath.bezierPathWithRect_(NSMakeRect(x, base_y + r * (cell + vgap), cell, cell)).fill()
            x += pitch

        # ── blinking caret █ at the bottom, BEFORE the transcribed sentence ──
        cb = 0.5 + 0.5 * math.sin(self.phase * 4.0)
        NSColor.colorWithCalibratedRed_green_blue_alpha_(MATRIX[0], MATRIX[1], MATRIX[2], 0.30 + 0.70 * cb).set()
        NSBezierPath.bezierPathWithRect_(NSMakeRect(24.0, 18.0, 5.0, 14.0)).fill()
        self.copy_rect = None
        if self.text:
            font = NSFont.fontWithName_size_("Menlo", 11.0)
            if font is None:
                font = NSFont.systemFontOfSize_(11.0)
            attrs = {
                NSFontAttributeName: font,
                NSForegroundColorAttributeName:    # single green everywhere = MATRIX
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(MATRIX[0], MATRIX[1], MATRIX[2], 0.95),
            }
            s = self.text
            if len(s) > 52:
                s = "…" + s[-51:]
            ns = NSString.stringWithString_(s)
            tw = ns.sizeWithAttributes_(attrs).width
            ns.drawAtPoint_withAttributes_(NSMakePoint(35.0, 18.0), attrs)
            # ── ⧉ inline COPY, at the end of the transcribed text ──
            cix = min(35.0 + tw + 10.0, w - 28.0)
            ciy = 18.0
            recent = (time.time() - self.copied_flash) < 1.0
            ic = MATRIX if (self.hover_c or recent) else (1.0, 1.0, 1.0)
            NSColor.colorWithCalibratedRed_green_blue_alpha_(ic[0], ic[1], ic[2], 0.92).set()
            if recent:
                chk = NSBezierPath.bezierPath()
                chk.moveToPoint_(NSMakePoint(cix, ciy + 4))
                chk.lineToPoint_(NSMakePoint(cix + 4, ciy))
                chk.lineToPoint_(NSMakePoint(cix + 11, ciy + 11))
                chk.setLineWidth_(2.0)
                chk.stroke()
            else:
                bk = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(cix + 4, ciy, 9, 10), 2, 2)
                bk.setLineWidth_(1.4)
                bk.stroke()
                fr = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(cix, ciy + 3, 9, 10), 2, 2)
                fr.setLineWidth_(1.4)
                fr.stroke()
            self.copy_rect = (cix - 3, ciy - 3, 22, 20)

        # ── ✕ STOP top-right (white, green on hover) — copy is inline with text ──
        WHITE = (1.0, 1.0, 1.0)
        cxc = w - 26.0
        s = 11.0
        xc = MATRIX if self.hover_x else WHITE
        NSColor.colorWithCalibratedRed_green_blue_alpha_(xc[0], xc[1], xc[2], 0.92).set()
        qy = h - 26.0
        xq = NSBezierPath.bezierPath()
        xq.moveToPoint_(NSMakePoint(cxc - s / 2, qy))
        xq.lineToPoint_(NSMakePoint(cxc + s / 2, qy + s))
        xq.moveToPoint_(NSMakePoint(cxc + s / 2, qy))
        xq.lineToPoint_(NSMakePoint(cxc - s / 2, qy + s))
        xq.setLineWidth_(1.6)
        xq.stroke()

        # ── 🔒 LOCK (left of the ✕): padlock — amber when pinned visible ──
        cx_l = w - 52.0
        lc = LOCK_AMBER if self.locked else (MATRIX if self.hover_l else WHITE)
        la = 0.95 if (self.locked or self.hover_l) else 0.62
        NSColor.colorWithCalibratedRed_green_blue_alpha_(lc[0], lc[1], lc[2], la).set()
        body = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(cx_l - 5.0, qy, 10.0, 8.0), 1.5, 1.5)
        shackle = NSBezierPath.bezierPath()
        shackle.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            NSMakePoint(cx_l, qy + 8.0), 3.0, 0.0, 180.0)
        shackle.setLineWidth_(1.4)
        if self.locked:
            body.fill()
        else:
            body.setLineWidth_(1.4)
            body.stroke()
        shackle.stroke()
        self.lock_rect = (cx_l - 9.0, qy - 3.0, 20.0, 20.0)

        # ── 🔇 MUTE (left of the lock): circle+slash — red when mic paused ──
        cx_m = w - 78.0
        cym = qy + 4.0
        mc = MUTE_RED if self.muted else (MATRIX if self.hover_m else WHITE)
        ma = 0.95 if (self.muted or self.hover_m) else 0.62
        NSColor.colorWithCalibratedRed_green_blue_alpha_(mc[0], mc[1], mc[2], ma).set()
        circ = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx_m - 6.0, cym - 6.0, 12.0, 12.0))
        circ.setLineWidth_(1.4)
        circ.stroke()
        if self.muted:
            sl = NSBezierPath.bezierPath()
            sl.moveToPoint_(NSMakePoint(cx_m - 4.2, cym - 4.2))
            sl.lineToPoint_(NSMakePoint(cx_m + 4.2, cym + 4.2))
            sl.setLineWidth_(1.6)
            sl.stroke()
        self.mute_rect = (cx_m - 9.0, qy - 3.0, 20.0, 20.0)


def _engine_running():
    try:
        r = subprocess.run(["pgrep", "-f", "engine.py"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def _start_engine():
    try:
        subprocess.Popen([sys.executable or "python3", str(ENGINE_PATH)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass


def _stop_engine():
    try:
        subprocess.Popen(["pkill", "-f", "engine.py"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class Controller(NSObject):
    def initWithDemo_place_(self, demo, place):
        self = objc.super(Controller, self).init()
        if self is None:
            return None
        self.demo = bool(demo)
        self.place = bool(place)
        self.alpha = 0.0
        self.last_active = False
        self.last_voice = 0.0
        self.demo_full = "open the dashboard and show me the real margin"
        self.demo_i = 0
        self.t0 = time.time()
        self._build_window()
        self._build_menubar()
        # Optional: come up already dictating when launchd-managed. Gated by env so
        # the default (manual 🎙 toggle) is unchanged. The engine is spawned here in
        # the pill's GUI/TCC context, where the mic permission resolves correctly.
        if (not self.demo and not self.place
                and os.environ.get("VIBEVOICE_ENGINE_AUTOSTART") == "1"
                and not _engine_running()):
            _start_engine()
        return self

    def _build_window(self):
        # find the screen with the NOTCH (built-in), NOT mainScreen
        scr = None
        for s in NSScreen.screens():
            try:
                if s.safeAreaInsets().top > 0:
                    scr = s
                    break
            except Exception:
                pass
        if scr is None:
            scr = NSScreen.mainScreen()
        screen = scr.frame()
        try:
            notch = scr.safeAreaInsets().top
        except Exception:
            notch = 0.0
        if notch <= 0:
            notch = 38.0
        cx = screen.origin.x + screen.size.width / 2.0
        top = screen.origin.y + screen.size.height           # absolute top edge (flush with notch)
        # real notch width (menu-bar aux areas on the sides) for the collapsed footprint
        notch_w = 210.0
        try:
            la = scr.auxiliaryTopLeftArea()
            ra = scr.auxiliaryTopRightArea()
            nw = screen.size.width - la.size.width - ra.size.width
            if 120.0 < nw < 420.0:
                notch_w = nw
        except Exception:
            pass
        # expanded: full pill, top edge FLUSH with the screen edge → looks like the notch extended
        self.exp = (cx - PILL_W / 2.0, top - PILL_H, PILL_W, PILL_H)
        # collapsed: EXACT notch footprint (real width+height) → it "is" the notch
        self.col = (cx - notch_w / 2.0, top - notch, notch_w, notch)
        rect = NSMakeRect(self.col[0], self.col[1], self.col[2], self.col[3])
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setLevel_(NSStatusWindowLevel)
        panel.setHasShadow_(False)            # no shadow (pure black)
        panel.setIgnoresMouseEvents_(False)   # click on the copy icon (no drag)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces |
            NSWindowCollectionBehaviorStationary)
        panel.setAlphaValue_(0.0)
        view = PillView.alloc().initWithFrame_(NSMakeRect(0, 0, PILL_W, PILL_H))
        view.setAutoresizingMask_(18)   # NSViewWidthSizable|HeightSizable — grows with the panel
        panel.setContentView_(view)
        panel.orderFrontRegardless()
        self.panel = panel
        self.view = view

    def _build_menubar(self):
        # icon ALWAYS present in the menu bar → master switch (even when mic is off)
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.status_item.button().setTitle_("🎙")
        menu = NSMenu.alloc().init()
        self.mb_toggle = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Voice: …", "toggleVoice:", "")
        self.mb_toggle.setTarget_(self)
        menu.addItem_(self.mb_toggle)
        self.mb_mute = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "🔇 Mute mic", "toggleMute:", "")
        self.mb_mute.setTarget_(self)
        menu.addItem_(self.mb_mute)
        self.mb_lock = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "🔒 Keep pill visible", "toggleLock:", "")
        self.mb_lock.setTarget_(self)
        menu.addItem_(self.mb_lock)
        menu.addItem_(NSMenuItem.separatorItem())
        qi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "quitAll:", "")
        qi.setTarget_(self)
        menu.addItem_(qi)
        self.status_item.setMenu_(menu)
        self._mb_last = None
        self._mb_mute_last = None
        self._mb_lock_last = None

    def _mic_is_on(self):
        return _engine_running()

    def toggleVoice_(self, sender):
        if _engine_running():
            _stop_engine()
        else:
            _start_engine()

    def toggleMute_(self, sender):
        # 🔇 pause/resume the mic without killing the engine (the engine reads
        # the `muted` control file and ignores audio while it exists).
        _toggle_flag(MUTED_FILE)

    def toggleLock_(self, sender):
        # 🔒 pin the pill visible / release it back to auto-hide.
        _toggle_flag(LOCKED_FILE)

    def quitAll_(self, sender):
        _stop_engine()
        NSApp.terminate_(self)

    # ── live sources ──
    def _read_levels(self):
        try:
            data = LEVELS_BIN.read_bytes()
            if len(data) < 60 * 4:          # torn/partial read → skip the frame
                return None
            vals = struct.unpack("<60f", data[:60 * 4])
            out = []
            for i in range(N_BARS):
                j = int(i * 60 / N_BARS)
                v = abs(vals[j]) * GAIN                 # higher sensitivity
                out.append(min(1.0, v ** 0.6))          # perceptual curve: quiet voice = more visible
            return out
        except Exception:
            return None

    def _read_raw_energy(self):
        # raw RMS (no GAIN) for immediate voice onset
        try:
            data = LEVELS_BIN.read_bytes()
            if len(data) < 60 * 4:
                return 0.0
            vals = struct.unpack("<60f", data[:60 * 4])
            return max(abs(v) for v in vals)
        except Exception:
            return 0.0

    def _read_state(self):
        try:
            return STATE_FILE.read_text().strip()
        except Exception:
            return ""

    def _read_text(self):
        try:
            lines = RAW_TXT.read_text().strip().splitlines()
            return lines[-1].strip() if lines else ""
        except Exception:
            return ""

    def _animate_(self, show):
        # Dynamic Island: EXPANDS from the notch / RE-COLLAPSES into it.
        # the collapsed snap stays OUTSIDE the grouping, otherwise it jumps a frame.
        if show:
            self.panel.setFrame_display_(NSMakeRect(*self.col), True)
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(0.34)
        if show:
            self.panel.animator().setAlphaValue_(1.0)
            self.panel.animator().setFrame_display_(NSMakeRect(*self.exp), True)
        else:
            self.panel.animator().setAlphaValue_(0.0)
            self.panel.animator().setFrame_display_(NSMakeRect(*self.col), True)
        NSAnimationContext.endGrouping()

    def tick_(self, timer):
        self.view.phase = time.time() - self.t0
        if self.demo or self.place:
            active = True
            ph = self.view.phase
            levels = []
            for i in range(N_BARS):
                base = 0.5 + 0.45 * math.sin(ph * 5.0 + i * 0.5)
                env = 0.5 + 0.5 * math.sin(ph * 1.3)
                levels.append(max(0.04, base * env * (0.6 + 0.4 * random.random())))
            # typewriter
            if int(ph * 12) > self.demo_i and self.demo_i < len(self.demo_full):
                self.demo_i += 1
            if self.demo_i >= len(self.demo_full) and ph % 6 < 0.1:
                self.demo_i = 0
            text = self.demo_full[: self.demo_i]
            if self.place:
                text = "↔ placement mode · drag me where you want"
        else:
            state = self._read_state()
            levels = self._read_levels()
            if levels is None:
                levels = [0.04] * N_BARS
            raw = self._read_raw_energy()
            # IMMEDIATE onset: appears as soon as voice clears the noise floor (raw RMS),
            # or when the engine is recording/transcribing. Floor ~0.005 < VOICE_THRESH 0.018.
            if raw > VOICE_THRESH or state in ("recording", "transcribing"):
                self.last_voice = time.time()
            text_now = self._read_text()
            hold = 2.5 if text_now else IDLE_HIDE_S   # with text it stays visible to click copy
            active = (time.time() - self.last_voice) <= hold
            text = text_now if active else ""

        # control flags (mute / lock) — read once per tick.
        muted = _flag_on(MUTED_FILE)
        locked = _flag_on(LOCKED_FILE)
        # 🔒 lock pins the pill visible regardless of silence (not in demo/place,
        # which force-show anyway).
        if locked and not (self.demo or self.place):
            active = True
        self.view.muted = muted
        self.view.locked = locked

        # show/hide transition → native AppKit animation (fade + slide from the notch),
        # triggered ONCE on state change. The waveform updates every tick.
        if active != self.last_active:
            self._animate_(active)
            self.last_active = active
        # icon hover (poll mouse without event stream) — before the redraw
        try:
            mloc = self.panel.mouseLocationOutsideOfEventStream()
            vb = self.view.bounds()
            ww, hh = vb.size.width, vb.size.height
            self.view.hover_x = bool(mloc.x >= ww - 40.0 and mloc.y >= hh - 34.0)
            cr = self.view.copy_rect
            self.view.hover_c = bool(cr and cr[0] <= mloc.x <= cr[0] + cr[2]
                                     and cr[1] <= mloc.y <= cr[1] + cr[3])
            mr = self.view.mute_rect
            self.view.hover_m = bool(mr and mr[0] <= mloc.x <= mr[0] + mr[2]
                                     and mr[1] <= mloc.y <= mr[1] + mr[3])
            lr = self.view.lock_rect
            self.view.hover_l = bool(lr and lr[0] <= mloc.x <= lr[0] + lr[2]
                                     and lr[1] <= mloc.y <= lr[1] + lr[3])
        except Exception:
            pass
        self.view.setLevels_text_active_(levels, text, active)
        # menu bar: icon/label update only on mic state change
        on = self._mic_is_on()
        if on != self._mb_last:
            self._mb_last = on
            self.status_item.button().setTitle_("🎙" if on else "🔇")
            self.mb_toggle.setTitle_("● Voice on — click to stop" if on
                                     else "○ Voice off — click to start")
        # menu titles for mute/lock reflect the live flag state
        if muted != self._mb_mute_last:
            self._mb_mute_last = muted
            self.mb_mute.setTitle_("🔊 Unmute mic" if muted else "🔇 Mute mic")
        if locked != self._mb_lock_last:
            self._mb_lock_last = locked
            self.mb_lock.setTitle_("🔓 Release pill" if locked else "🔒 Keep pill visible")


def main():
    ap = argparse.ArgumentParser(description="VibeVoice — Dynamic Island STT pill (MIT)")
    ap.add_argument("--demo", action="store_true", help="animated demo to preview the design")
    ap.add_argument("--place", action="store_true", help="placement mode: stays visible")
    args = ap.parse_args()

    _ensure_state_dir()

    global _CTRL, _TIMER
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    _CTRL = Controller.alloc().initWithDemo_place_(args.demo, args.place)
    _TIMER = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        TICK, _CTRL, "tick:", None, True)
    NSApp.run()


if __name__ == "__main__":
    main()
