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
vibevoice.py — VibeVoice "Dynamic Island" STT pill + robot command center

A minimal floating UI for a speech-to-text engine. A borderless, floating,
non-activating NSPanel docked under the notch. Decoupled from the engine: it
READS the state files written by the engine and draws. It never touches the
audio pipeline directly.

  idle          → invisible (alpha 0)
  recording     → fade-in, live waveform
  transcribing  → keeps drawing the transcript that streams in
  silence       → fade-out after ~1.5s (or ~2.5s while text is shown)

A floating ROBOT widget doubles as the command center: click it for the menu,
drag it to reposition (persisted). It exists because on recent macOS a status
item created before launch-finish can be parked off-screen behind the notch —
the robot is our own deterministic, always-visible control. The menu-bar status
item is still created (and self-heals if parked); both share the same menu.

STATE-FILE CONTRACT (shared pill <-> engine, under ~/.vibevoice/):
  ~/.vibevoice/state       text file, one of: idle | recording | transcribing
  ~/.vibevoice/levels.bin  60 float32 little-endian (RMS 0..1), atomic write
  ~/.vibevoice/raw.txt     last transcription, plain text (just the sentence)
  ~/.vibevoice/history.jsonl  last 20 transcriptions, JSONL {"ts","text"}, newest last

The engine WRITES these files; the pill READS them.

CONTROL FILES (this pill WRITES them; the engine / this pill honor them — the
same external-control pattern as autosend's pause flag, NOT engine-owned state):
  ~/.vibevoice/muted       presence = mic paused (engine ignores audio, stays alive)
  ~/.vibevoice/locked      presence = pill stays visible (no auto-hide)
  ~/.vibevoice/autosend    "on" | "off" — armed state of the auto-Return daemon
                           (autosend.py owns this file; the pill toggles it and
                           spawns the daemon when it isn't already running)

OPTIONAL TTS-REACTIVITY (self-contained hook; ANY external text-to-speech may
write these, the pill only READS them — no engine/autosend change needed):
  ~/.vibevoice/tts         presence = the feature is enabled
  ~/.vibevoice/tts.txt     line 1: "<start_epoch> <duration_s>"; line 2+: spoken text
  ~/.vibevoice/tts_levels.bin  60 float32 LE (RMS 0..1) of the TTS audio
When tts.txt is present and fresh, the pill turns RED and types out the spoken
sentence in sync with the audio (a mirror of the green dictation waveform).

  ~/.vibevoice/robot_pos   "x,y" — saved position of the floating robot (drag)

Menu / robot acts as the master switch:
  - toggle the engine (launches/kills engine.py via subprocess)
  - Mute mic (pause without killing the engine)
  - Lock pill (pin it so it never auto-hides)
  - Auto-send loop (arm autosend.py: every dictation → automatic Return)
  - Restart / Quit

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

import config
import objc

from AppKit import (
    NSApplication, NSApp, NSPanel, NSView, NSColor, NSBezierPath, NSAnimationContext,
    NSScreen, NSTimer, NSFont, NSForegroundColorAttributeName,
    NSFontAttributeName, NSMakeRect, NSMakePoint, NSImage, NSCursor, NSEvent,
    NSTrackingArea, NSMenu,
    NSTrackingMouseEnteredAndExited, NSTrackingMouseMoved,
    NSTrackingActiveAlways, NSTrackingCursorUpdate,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSBackingStoreBuffered, NSStatusWindowLevel,
    NSApplicationActivationPolicyAccessory, NSApplicationActivationPolicyRegular,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSString, NSPasteboard, NSPasteboardTypeString,
    NSStatusBar, NSMenuItem, NSVariableStatusItemLength,
    NSWindow, NSTextField, NSButton, NSPopUpButton, NSTextView, NSScrollView,
)
from Foundation import NSObject, NSMakeSize

# ── state-file contract (under $HOME/.vibevoice) ──────────────────────────────
STATE_DIR  = Path(os.path.expanduser("~/.vibevoice"))
STATE_FILE = STATE_DIR / "state"        # idle | recording | transcribing
LEVELS_BIN = STATE_DIR / "levels.bin"   # 60 float32 LE (RMS 0..1)
RAW_TXT    = STATE_DIR / "raw.txt"       # last transcription (plain text)
# control files (the pill writes these; not engine-owned state):
MUTED_FILE  = STATE_DIR / "muted"        # presence = mic paused (engine reads, stays alive)
LOCKED_FILE = STATE_DIR / "locked"       # presence = pill stays visible (pill-only, no auto-hide)
AUTOSEND_FILE = STATE_DIR / "autosend"   # "on" | "off" — autosend.py owns this; the pill toggles it
# optional TTS-reactivity hook (any external TTS writes these; the pill reads):
TTS_FLAG   = STATE_DIR / "tts"            # presence = feature enabled
TTS_TEXT   = STATE_DIR / "tts.txt"        # line1 "<start> <dur>", line2+ spoken text
TTS_LEVELS = STATE_DIR / "tts_levels.bin" # 60 float32 LE (RMS of the TTS audio)
ROBOT_POS  = STATE_DIR / "robot_pos"      # "x,y" saved position of the floating robot

# engine.py / autosend.py live next to this file
ENGINE_PATH   = Path(os.path.abspath(__file__)).parent / "engine.py"
AUTOSEND_PATH = Path(os.path.abspath(__file__)).parent / "autosend.py"

# Menu-bar-only mode (sess.9203): the floating pill + robot widget are the live
# waveform/TTS visualiser, but Mattia wants a single surface — the menu-bar item.
# False → the pill panel is never shown and the robot widget is never built; the
# NSStatusBar item stays the sole UI. Flip to True to bring the floating pill back.
SHOW_PILL = False


def _flag_on(path) -> bool:
    """True if a control flag file exists (defensive: never raises)."""
    try:
        return path.exists()
    except Exception:
        return False


def _toggle_flag(path) -> bool:
    """Create the flag if absent, remove it if present. Returns the new state."""
    try:
        if path.exists():
            path.unlink()
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return True
    except Exception:
        return _flag_on(path)


def _autosend_armed() -> bool:
    """True if the auto-send loop is armed ('on' in the shared autosend file)."""
    try:
        return AUTOSEND_FILE.read_text().strip() == "on"
    except Exception:
        return False


# ── design ────────────────────────────────────────────────────────────────────
N_BARS       = 32
PILL_W       = 460.0
PILL_H       = 140.0    # taller: the waveform never grazes the notch
PILL_RADIUS  = 22.0
GAIN         = 12.0     # waveform sensitivity: higher = more reactive
VOICE_THRESH = 0.018    # raw RMS onset threshold (floor ~0.005 < thresh < speech ~0.05)

FADE_STEP   = 0.18           # alpha per tick (fade in/out)
IDLE_HIDE_S = 1.5            # seconds of silence before fade-out
TICK        = 1.0 / 24.0     # ~24 fps

MATRIX = (0.12, 1.00, 0.32)  # Matrix-terminal flúor green (#1fff52) — dictation
AMBER  = (1.00, 0.66, 0.18)  # 🔒 locked / 🔁 auto-send armed
RED    = (0.95, 0.27, 0.27)  # 🔇 muted / TTS speaking

_CTRL  = None   # strong refs (avoid GC of controller/timer)
_TIMER = None


def _ensure_state_dir():
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


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
        self.tint = MATRIX          # current colour: green (dictation) or red (TTS speaking)
        self.phase = 0.0
        self.copied_flash = 0.0
        self.hover_x = False
        self.hover_c = False
        self.copy_rect = None
        # control panel — re-read the flags from disk on launch, otherwise a pill
        # restart (e.g. launchd kickstart) would show mic-ON even if muted/locked
        # already exist, and the UI would lie about the real state.
        self.muted = _flag_on(MUTED_FILE)
        self.locked = _flag_on(LOCKED_FILE)
        self.muta_rect = None
        self.blocca_rect = None
        self.hover_m = False
        self.hover_l = False
        self.autoloop = _autosend_armed()
        self.loop_rect = None
        self.hover_o = False
        self._loop_chk = 0.0
        return self

    # NSTrackingArea: pointing-hand cursor over clickable icons, even on a
    # non-key panel. The per-frame .set() gets reset by the WindowServer;
    # cursorUpdate_ is the correct path.
    def updateTrackingAreas(self):
        for ta in list(self.trackingAreas()):
            self.removeTrackingArea_(ta)
        opts = (NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved
                | NSTrackingActiveAlways | NSTrackingCursorUpdate)
        ta = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), opts, self, None)
        self.addTrackingArea_(ta)
        objc.super(PillView, self).updateTrackingAreas()

    def _over_icon(self, loc):
        """True if loc (window coords) is over a clickable icon (✕/copy/mute/lock/loop)."""
        b = self.bounds()
        w, h = b.size.width, b.size.height
        if loc.x >= w - 40.0 and loc.y >= h - 34.0:        # ✕
            return True
        for r in (self.copy_rect, self.muta_rect, self.blocca_rect, self.loop_rect):
            if r and r[0] <= loc.x <= r[0] + r[2] and r[1] <= loc.y <= r[1] + r[3]:
                return True
        return False

    def cursorUpdate_(self, event):
        if self._over_icon(event.locationInWindow()):
            NSCursor.pointingHandCursor().set()
        else:
            NSCursor.arrowCursor().set()

    # mouseMoved_ is the most reliable path on a non-key NSPanel: the .set()
    # during the real event is honoured by the WindowServer (the per-frame
    # version in the tick was being reset). Delivered via the tracking area.
    def mouseMoved_(self, event):
        if self._over_icon(event.locationInWindow()):
            NSCursor.pointingHandCursor().set()
        else:
            NSCursor.arrowCursor().set()

    def mouseDown_(self, event):
        b = self.bounds()
        loc = event.locationInWindow()   # the view fills the window → coords match
        w, h = b.size.width, b.size.height
        # ✕ STOP (top-right) → kills the engine, pill stays as a service
        if loc.x >= w - 40.0 and loc.y >= h - 34.0:
            _stop_engine()
            return
        # 🔇 MUTE (below the ✕) → pause the mic without killing the engine
        mr = self.muta_rect
        if mr and mr[0] <= loc.x <= mr[0] + mr[2] and mr[1] <= loc.y <= mr[1] + mr[3]:
            self.muted = _toggle_flag(MUTED_FILE)
            self.setNeedsDisplay_(True)
            return
        # 🔒 LOCK (padlock) → keep the pill visible (no auto-hide)
        lr = self.blocca_rect
        if lr and lr[0] <= loc.x <= lr[0] + lr[2] and lr[1] <= loc.y <= lr[1] + lr[3]:
            self.locked = _toggle_flag(LOCKED_FILE)
            self.setNeedsDisplay_(True)
            return
        # 🔁 AUTO-SEND LOOP (below the padlock) → arm/disarm autosend.py
        orr = self.loop_rect
        if orr and orr[0] <= loc.x <= orr[0] + orr[2] and orr[1] <= loc.y <= orr[1] + orr[3]:
            _toggle_autosend()
            self.autoloop = _autosend_armed()
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
        was = self.active
        self.active = active
        # the loop can change from OUTSIDE the pill (hotkey, daemon auto-disarm):
        # re-read the armed state at most once/second and redraw if it changed.
        now = time.time()
        if now - self._loop_chk > 1.0:
            self._loop_chk = now
            cur = _autosend_armed()
            if cur != self.autoloop:
                self.autoloop = cur
                self.setNeedsDisplay_(True)
        # redraw ONLY while visible (or on the transition frame to hidden, for a
        # clean fade). From idle/hidden it does NOT redraw: the fade is the native
        # animator on alpha. Fixes the constant idle CPU and the "freeze" when the
        # engine dies (an orphan pill spinning the waveform on stale levels).
        if active or was:
            self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        b = self.bounds()
        hf = b.size.height
        # ── pure-BLACK background (extension of the notch) ──
        w, h = b.size.width, b.size.height
        bg = _pill_path(w, h, PILL_RADIUS)   # square top, rounded bottom
        NSColor.blackColor().set()           # PURE BLACK — no border, stroke, or shadow
        bg.fill()

        # ── collapsed state (inside the notch): a single bar in the current tint ──
        if hf < 34.0:
            NSColor.colorWithCalibratedRed_green_blue_alpha_(self.tint[0], self.tint[1], self.tint[2], 0.85).set()
            NSBezierPath.bezierPathWithRect_(NSMakeRect(b.size.width / 2.0 - 16, hf / 2.0 - 1.5, 32, 3)).fill()
            return

        # ── PIXEL Matrix-terminal waveform: columns of square LEDs, FULL WIDTH ──
        cell, vgap, col_w, cgap = 4.0, 2.0, 5.0, 3.0
        wf_x0 = 24.0                      # left padding
        wf_x1 = b.size.width - 54.0       # space on the right for ✕ / ⧉ icons
        wf_w = wf_x1 - wf_x0
        text_band = 40.0                  # padding below the text
        base_y = text_band + 6.0          # =46 — gap between waveform and text
        top_y = hf - 44.0                 # clearance from the notch (~38px) + margin
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
                NSColor.colorWithCalibratedRed_green_blue_alpha_(self.tint[0], self.tint[1], self.tint[2], a).set()
                NSBezierPath.bezierPathWithRect_(NSMakeRect(x, base_y + r * (cell + vgap), cell, cell)).fill()
            x += pitch

        # ── blinking caret █ at the bottom, BEFORE the transcribed sentence ──
        cb = 0.5 + 0.5 * math.sin(self.phase * 4.0)
        NSColor.colorWithCalibratedRed_green_blue_alpha_(self.tint[0], self.tint[1], self.tint[2], 0.30 + 0.70 * cb).set()
        NSBezierPath.bezierPathWithRect_(NSMakeRect(24.0, 18.0, 5.0, 14.0)).fill()
        self.copy_rect = None
        if self.text:
            font = NSFont.fontWithName_size_("Menlo", 11.0)
            if font is None:
                font = NSFont.systemFontOfSize_(11.0)
            attrs = {
                NSFontAttributeName: font,
                NSForegroundColorAttributeName:    # dynamic tint: green dictation / red TTS
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(self.tint[0], self.tint[1], self.tint[2], 0.95),
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
            ic = self.tint if (self.hover_c or recent) else (1.0, 1.0, 1.0)
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

        # ── ✕ STOP top-right (white, tinted on hover) — copy is inline with text ──
        WHITE = (1.0, 1.0, 1.0)
        cxc = w - 26.0
        s = 11.0
        xc = self.tint if self.hover_x else WHITE
        NSColor.colorWithCalibratedRed_green_blue_alpha_(xc[0], xc[1], xc[2], 0.92).set()
        qy = h - 26.0
        xq = NSBezierPath.bezierPath()
        xq.moveToPoint_(NSMakePoint(cxc - s / 2, qy))
        xq.lineToPoint_(NSMakePoint(cxc + s / 2, qy + s))
        xq.moveToPoint_(NSMakePoint(cxc + s / 2, qy))
        xq.lineToPoint_(NSMakePoint(cxc - s / 2, qy + s))
        xq.setLineWidth_(1.6)
        xq.stroke()

        # ── 🔇 MUTE (below the ✕) — speaker + waves / slash when muted ──
        mx, my = cxc, h - 52.0
        self.muta_rect = (mx - 11, my - 11, 22, 22)
        mc = RED if self.muted else (self.tint if self.hover_m else WHITE)
        ma = 0.95 if (self.muted or self.hover_m) else 0.62
        NSColor.colorWithCalibratedRed_green_blue_alpha_(mc[0], mc[1], mc[2], ma).set()
        sp = NSBezierPath.bezierPath()
        sp.moveToPoint_(NSMakePoint(mx - 6, my - 2))
        sp.lineToPoint_(NSMakePoint(mx - 3, my - 2))
        sp.lineToPoint_(NSMakePoint(mx + 1, my - 5))
        sp.lineToPoint_(NSMakePoint(mx + 1, my + 5))
        sp.lineToPoint_(NSMakePoint(mx - 3, my + 2))
        sp.lineToPoint_(NSMakePoint(mx - 6, my + 2))
        sp.closePath()
        sp.fill()
        if self.muted:
            sl = NSBezierPath.bezierPath()
            sl.moveToPoint_(NSMakePoint(mx - 7, my + 7))
            sl.lineToPoint_(NSMakePoint(mx + 7, my - 7))
            sl.setLineWidth_(1.7)
            sl.stroke()
        else:
            for rr in (3.2, 5.4):
                wv = NSBezierPath.bezierPath()
                wv.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
                    NSMakePoint(mx + 1, my), rr, -42.0, 42.0)
                wv.setLineWidth_(1.2)
                wv.stroke()

        # ── 🔒 LOCK (padlock) — filled when locked (no auto-hide), outline when unlocked ──
        lx, ly = cxc, h - 78.0
        self.blocca_rect = (lx - 11, ly - 11, 22, 22)
        lc = AMBER if self.locked else (self.tint if self.hover_l else WHITE)
        la = 0.95 if (self.locked or self.hover_l) else 0.62
        NSColor.colorWithCalibratedRed_green_blue_alpha_(lc[0], lc[1], lc[2], la).set()
        body = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(lx - 5, ly - 6, 10, 8), 1.6, 1.6)
        shackle = NSBezierPath.bezierPath()
        shackle.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            NSMakePoint(lx, ly + 2), 3.2, 0.0, 180.0)
        shackle.setLineWidth_(1.4)
        if self.locked:
            body.fill()
            shackle.stroke()
        else:
            body.setLineWidth_(1.4)
            body.stroke()
            shackle.stroke()

        # ── 🔁 AUTO-SEND LOOP (below the padlock) — two looping arrows
        #    armed = AMBER filled · off = white/tint-hover outline
        ox, oy = cxc, h - 104.0
        self.loop_rect = (ox - 11, oy - 11, 22, 22)
        oc = AMBER if self.autoloop else (self.tint if self.hover_o else WHITE)
        oa = 0.95 if (self.autoloop or self.hover_o) else 0.62
        NSColor.colorWithCalibratedRed_green_blue_alpha_(oc[0], oc[1], oc[2], oa).set()
        R_LOOP = 5.6
        # top arc (left→right) + bottom arc (right→left)
        for a0, a1 in ((150.0, 30.0), (330.0, 210.0)):
            arc = NSBezierPath.bezierPath()
            arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                NSMakePoint(ox, oy), R_LOOP, a0, a1, True)
            arc.setLineWidth_(1.5)
            arc.stroke()
        # arrow heads at the arc tips (at 30° and 210°)
        for ang, drot in ((30.0, -90.0), (210.0, -90.0)):
            rad = math.radians(ang)
            tipx = ox + R_LOOP * math.cos(rad)
            tipy = oy + R_LOOP * math.sin(rad)
            t = math.radians(ang + drot)   # clockwise tangent direction
            ah = NSBezierPath.bezierPath()
            ah.moveToPoint_(NSMakePoint(tipx + 3.4 * math.cos(t + 2.6), tipy + 3.4 * math.sin(t + 2.6)))
            ah.lineToPoint_(NSMakePoint(tipx, tipy))
            ah.lineToPoint_(NSMakePoint(tipx + 3.4 * math.cos(t - 2.6), tipy + 3.4 * math.sin(t - 2.6)))
            ah.setLineWidth_(1.5)
            ah.stroke()


class RobotView(NSView):
    """Floating ROBOT widget — the always-visible command center (the menu bar
    can park new status items off-screen behind the notch). Click = control
    center (the same menu as the status item) · drag = reposition (persisted)."""

    def initWithFrame_(self, frame):
        self = objc.super(RobotView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.on = False
        self.loop = False
        self.owner = None        # Controller (for the menu + the icon)
        self._drag0 = None       # (mouse_screen, win_origin) at drag start
        self._moved = False
        return self

    def drawRect_(self, rect):
        b = self.bounds()
        from AppKit import NSGradient
        # ── disc with a body: radial gradient + an accent ring tinted by state +
        #    a hairline. Glass-orb style.
        inset = NSMakeRect(4.0, 4.0, b.size.width - 8.0, b.size.height - 8.0)
        disc = NSBezierPath.bezierPathWithOvalInRect_(inset)
        # state tint: amber (loop) · green (listening) · grey (off)
        if self.loop and self.on:
            tc = (1.0, 0.72, 0.05)
        elif self.on:
            tc = (0.12, 1.0, 0.32)
        else:
            tc = (0.48, 0.51, 0.58)
        # soft outer halo (state glow)
        for gr, ga in ((2.6, 0.10), (1.4, 0.16)):
            NSColor.colorWithCalibratedRed_green_blue_alpha_(tc[0], tc[1], tc[2], ga).set()
            halo = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(inset.origin.x - gr, inset.origin.y - gr,
                           inset.size.width + 2 * gr, inset.size.height + 2 * gr))
            halo.setLineWidth_(1.6)
            halo.stroke()
        # body: radial gradient panel→near-black (depth, light from top-left)
        g = NSGradient.alloc().initWithStartingColor_endingColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.14, 0.16, 0.22, 0.97),
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.03, 0.035, 0.055, 0.97))
        g.drawInBezierPath_relativeCenterPosition_(disc, NSMakePoint(-0.25, 0.35))
        # accent ring (thin, full character)
        NSColor.colorWithCalibratedRed_green_blue_alpha_(tc[0], tc[1], tc[2], 0.55).set()
        ring = NSBezierPath.bezierPathWithOvalInRect_(inset)
        ring.setLineWidth_(1.3)
        ring.stroke()
        # inner white hairline (glass sheen)
        NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.10).set()
        hl = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(inset.origin.x + 1.2, inset.origin.y + 1.2,
                       inset.size.width - 2.4, inset.size.height - 2.4))
        hl.setLineWidth_(0.8)
        hl.stroke()
        if self.owner is not None:
            img = self.owner._make_robot_icon(self.on, self.loop, S=20.0)
            img.drawInRect_fromRect_operation_fraction_(
                NSMakeRect((b.size.width - 20.0) / 2.0, (b.size.height - 20.0) / 2.0, 20.0, 20.0),
                NSMakeRect(0, 0, 0, 0), 2, 1.0)   # NSCompositeSourceOver

    def mouseDown_(self, event):
        w = self.window()
        self._drag0 = (NSEvent.mouseLocation(), w.frame().origin)
        self._moved = False

    def mouseDragged_(self, event):
        if not self._drag0:
            return
        m0, o0 = self._drag0
        m1 = NSEvent.mouseLocation()
        dx, dy = m1.x - m0.x, m1.y - m0.y
        if abs(dx) + abs(dy) > 3:
            self._moved = True
        self.window().setFrameOrigin_(NSMakePoint(o0.x + dx, o0.y + dy))

    def mouseUp_(self, event):
        if self._moved:
            try:
                o = self.window().frame().origin
                ROBOT_POS.write_text(f"{o.x},{o.y}")
            except Exception:
                pass
        else:
            # a plain click → control center (the same menu as the status item)
            if self.owner is not None and getattr(self.owner, "mb_menu", None):
                NSMenu.popUpContextMenu_withEvent_forView_(self.owner.mb_menu, event, self)
        self._drag0 = None


def _child_python():
    """Interpreter for spawning the engine.py/autosend.py siblings.

    Inside the py2app bundle sys.executable is the app LAUNCHER: it ignores
    argv and always boots the pill main again, so spawning children through it
    forks a second pill instead of the engine. py2app ships a real embedded
    interpreter at Contents/MacOS/python — use that when frozen. Keeping the
    script path in argv preserves invariant #8 (pgrep/pkill -f engine.py)."""
    if getattr(sys, "frozen", None) == "macosx_app":
        cand = Path(sys.executable).parent / "python"
        if cand.exists():
            return str(cand)
    return sys.executable or "python3"


def _engine_running():
    try:
        r = subprocess.run(["pgrep", "-f", "engine.py"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def _clean_child_env(base=None):
    """Environment for spawning the embedded interpreter (engine/autosend).

    Inside the py2app bundle os.environ carries PYTHONHOME / PYTHONPATH /
    PYTHONEXECUTABLE / RESOURCEPATH pointing INTO the app. Passing them to
    Contents/MacOS/python makes it miss its own stdlib and die on the first
    import (`ModuleNotFoundError: wave`, engine.py) within ~1s — so the menu-bar
    toggle looked dead. Strip them so the child interpreter resolves its own
    home. (Scar sess.9203: standalone spawn works, app-spawn crashed — the diff
    was the inherited env, not the signature.)"""
    env = dict(os.environ if base is None else base)
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "RESOURCEPATH",
              "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE",
              "PYVENV_LAUNCHER", "__PYVENV_LAUNCHER__"):
        env.pop(k, None)
    return env


def _start_engine():
    try:
        cfg = config.load()
        env = _clean_child_env()
        env.setdefault("VIBEVOICE_LANG", cfg["lang"])
        env.setdefault("VIBEVOICE_AUTOSEND", "1" if cfg["autosend"] else "0")
        env.setdefault("VIBEVOICE_AUTOSEND_RETURN", "1" if cfg["autosend_return"] else "0")
        env.setdefault("VIBEVOICE_VP", "1" if cfg["vp"] else "0")
        subprocess.Popen([_child_python(), str(ENGINE_PATH)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    except Exception:
        pass


def _stop_engine():
    try:
        subprocess.Popen(["pkill", "-f", "engine.py"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _autosend_running():
    try:
        r = subprocess.run(["pgrep", "-f", "autosend.py"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def _toggle_autosend():
    """Arm/disarm the auto-send loop. Writes "on"/"off" to the shared autosend
    file (autosend.py reads it) and spawns the daemon when arming if it isn't
    already running — a switch with no wire is useless."""
    try:
        cur = ""
        try:
            cur = AUTOSEND_FILE.read_text().strip()
        except Exception:
            pass
        if cur == "on":
            AUTOSEND_FILE.parent.mkdir(parents=True, exist_ok=True)
            AUTOSEND_FILE.write_text("off")
            subprocess.Popen(["afplay", "/System/Library/Sounds/Submarine.aiff"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            AUTOSEND_FILE.parent.mkdir(parents=True, exist_ok=True)
            AUTOSEND_FILE.write_text("on")
            subprocess.Popen(["afplay", "/System/Library/Sounds/Tink.aiff"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not _autosend_running():
                subprocess.Popen([_child_python(), str(AUTOSEND_PATH)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, env=_clean_child_env())
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
        self._restart_timer = None
        self._restart_ticks = 0
        self._build_window()
        self._build_menubar()
        if SHOW_PILL:
            self._build_robot()
        # Optional: come up already dictating when launchd-managed. Gated by env so
        # the default (manual toggle) is unchanged. The engine is spawned here in
        # the pill's GUI/TCC context, where the mic permission resolves correctly.
        if (not self.demo and not self.place
                and os.environ.get("VIBEVOICE_ENGINE_AUTOSTART") == "1"
                and not _engine_running()):
            _start_engine()
        return self

    def _build_robot(self):
        """Always-visible floating robot (functional stand-in for a status item
        that recent macOS can park off-screen behind the notch)."""
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
        sf = scr.frame()
        SZ = 34.0   # room for the glow ring + gradient body
        # position: saved, else top-right just below the menu bar
        x = sf.origin.x + sf.size.width - SZ - 14.0
        y = sf.origin.y + sf.size.height - SZ - 30.0
        try:
            sx, sy = ROBOT_POS.read_text().strip().split(",")
            x, y = float(sx), float(sy)
        except Exception:
            pass
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        rp = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, SZ, SZ), style, NSBackingStoreBuffered, False)
        rp.setOpaque_(False)
        rp.setBackgroundColor_(NSColor.clearColor())
        rp.setLevel_(NSStatusWindowLevel)
        rp.setHasShadow_(False)
        rp.setIgnoresMouseEvents_(False)
        rp.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces |
            NSWindowCollectionBehaviorStationary)
        rv = RobotView.alloc().initWithFrame_(NSMakeRect(0, 0, SZ, SZ))
        rv.owner = self
        rv.on = self._mic_is_on()
        rv.loop = _autosend_armed()
        rp.setContentView_(rv)
        rp.setAlphaValue_(1.0)
        rp.orderFrontRegardless()
        self.robot_panel = rp
        self.robot_view = rv

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
        panel.setAcceptsMouseMovedEvents_(True)   # mouseMoved_/hover on a non-key panel
        panel.disableCursorRects()   # stop the window's auto cursor-rect reset (it would
                                     # reset to arrow on every mouseMoved right after my set)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces |
            NSWindowCollectionBehaviorStationary)
        panel.setAlphaValue_(0.0)
        view = PillView.alloc().initWithFrame_(NSMakeRect(0, 0, PILL_W, PILL_H))
        view.setAutoresizingMask_(18)   # NSViewWidthSizable|HeightSizable — grows with the panel
        panel.setContentView_(view)
        if SHOW_PILL:
            panel.orderFrontRegardless()
        else:
            panel.orderOut_(None)    # menu-bar-only: keep the pill panel off-screen
        view.updateTrackingAreas()   # install the tracking area now (AppKit won't on a hand-built panel)
        self.panel = panel
        self.view = view

    def _make_robot_icon(self, on, loop, S=18.0):
        """Robot head with an antenna, two eyes and a mouth. States:
          mic OFF          → soft white outline, hollow eyes (sleeping)
          mic ON           → green Matrix eyes lit (listening)
          mic ON + loop 🔁 → amber eyes (listening AND auto-sending)
        Used both as the menu-bar icon and (scaled) by the floating widget."""
        img = NSImage.alloc().initWithSize_(NSMakeSize(S, S))
        img.lockFocus()
        if S != 18.0:
            # the drawing is calibrated at 18px → scale the context for other sizes
            from AppKit import NSAffineTransform
            t = NSAffineTransform.transform()
            t.scaleBy_(S / 18.0)
            t.concat()
        S = 18.0
        cx = S / 2.0
        WHITE = (1.0, 1.0, 1.0)
        GREEN = (0.12, 1.0, 0.32)
        AMBER_I = (1.0, 0.72, 0.05)
        body_a = 0.92 if on else 0.55

        NSColor.colorWithCalibratedRed_green_blue_alpha_(WHITE[0], WHITE[1], WHITE[2], body_a).set()
        # antenna: stalk + ball
        st = NSBezierPath.bezierPath()
        st.moveToPoint_(NSMakePoint(cx, 13.0))
        st.lineToPoint_(NSMakePoint(cx, 15.0))
        st.setLineWidth_(1.4)
        st.stroke()
        ball = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - 1.4, 15.0, 2.8, 2.8))
        ball.fill()
        # head: rounded rect 14×10
        head = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(cx - 7.0, 3.0, 14.0, 10.0), 3.2, 3.2)
        head.setLineWidth_(1.5)
        head.stroke()
        # side ears
        for ex in (cx - 8.6, cx + 7.6):
            ear = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(ex, 6.2, 1.0, 3.6), 0.5, 0.5)
            ear.fill()
        # eyes: lit (green/amber) when mic on, hollow when off
        if on:
            ec = AMBER_I if loop else GREEN
            NSColor.colorWithCalibratedRed_green_blue_alpha_(ec[0], ec[1], ec[2], 1.0).set()
            for ox_ in (cx - 3.6, cx + 1.2):
                eye = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(ox_, 7.4, 2.4, 2.4))
                eye.fill()
            NSColor.colorWithCalibratedRed_green_blue_alpha_(WHITE[0], WHITE[1], WHITE[2], body_a).set()
        else:
            for ox_ in (cx - 3.6, cx + 1.2):
                eye = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(ox_, 7.4, 2.4, 2.4))
                eye.setLineWidth_(1.0)
                eye.stroke()
        # mouth
        mo = NSBezierPath.bezierPath()
        mo.moveToPoint_(NSMakePoint(cx - 2.6, 5.2))
        mo.lineToPoint_(NSMakePoint(cx + 2.6, 5.2))
        mo.setLineWidth_(1.2)
        mo.stroke()
        img.unlockFocus()
        img.setTemplate_(False)   # NOT a template → keep the colour (template = mono white)
        return img

    def _build_menubar(self):
        # icon ALWAYS present in the menu bar → command center (even when mic is off)
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.status_item.button().setTitle_("")
        self.status_item.button().setImage_(self._make_robot_icon(self._mic_is_on(), _autosend_armed()))
        self.status_item.button().setToolTip_("VibeVoice — click for the menu")
        menu = NSMenu.alloc().init()
        # status header (disabled = label only)
        self.mb_status = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("🎙 VibeVoice", "", "")
        self.mb_status.setEnabled_(False)
        menu.addItem_(self.mb_status)
        menu.addItem_(NSMenuItem.separatorItem())
        self.mb_toggle = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Voice: …", "toggleVoice:", "")
        self.mb_toggle.setTarget_(self)
        menu.addItem_(self.mb_toggle)
        self.mb_mute = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("🔇 Mute (full pause)", "toggleMute:", "")
        self.mb_mute.setTarget_(self)
        menu.addItem_(self.mb_mute)
        self.mb_lock = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("🔒 Lock (stay visible)", "toggleLock:", "")
        self.mb_lock.setTarget_(self)
        menu.addItem_(self.mb_lock)
        self.mb_loop = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("🔁 Auto-send loop", "toggleLoop:", "")
        self.mb_loop.setTarget_(self)
        menu.addItem_(self.mb_loop)
        st = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("⚙️ Settings…", "openSettings:", ",")
        st.setTarget_(self)
        menu.addItem_(st)
        menu.addItem_(NSMenuItem.separatorItem())
        ri = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("↻ Restart pill", "restartPill:", "")
        ri.setTarget_(self)
        menu.addItem_(ri)
        qi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit (close everything)", "quitAll:", "")
        qi.setTarget_(self)
        menu.addItem_(qi)
        self.status_item.setMenu_(menu)
        self.mb_menu = menu   # reused by the floating robot (popup)
        self._mb_last = None

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

    def toggleLoop_(self, sender):
        # 🔁 arm/disarm the auto-send loop (every dictation → automatic Return).
        _toggle_autosend()
        try:
            self.view.autoloop = _autosend_armed()
            self.view.setNeedsDisplay_(True)
        except Exception:
            pass

    def restartPill_(self, sender):
        # best-effort restart when launchd-managed; a no-op otherwise.
        try:
            subprocess.Popen(
                ["launchctl", "kickstart", "-k",
                 "gui/%d/com.vibevoice.pill" % os.getuid()],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    # Poll cadence/timeout for the post-kill respawn below: _stop_engine() fires
    # `pkill -f engine.py` async (no wait), so we can't just fire-once after a
    # fixed delay — if the old process is still exiting when the timer fires,
    # skipping the start would leave the engine down for good (no lang/env
    # update, silently). Instead poll until it's actually dead, bounded so a
    # wedged old process can't spawn a second engine on top of it.
    _RESTART_POLL_INTERVAL = 0.25
    _RESTART_MAX_TICKS = 20  # ~5s

    def restartEngine_(self, _sender=None):
        # Kill + respawn engine.py so it picks up the new env (lang/autosend/
        # autosend_return from config.json). No-op if the engine isn't running
        # right now — Settings shouldn't autostart the mic.
        if not _engine_running():
            return
        # Rapid successive settings changes must not stack timers.
        if getattr(self, "_restart_timer", None) is not None:
            self._restart_timer.invalidate()
            self._restart_timer = None
        _stop_engine()
        self._restart_ticks = 0
        self._restart_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            self._RESTART_POLL_INTERVAL, self, "_pollRestartEngine:", None, True)

    def _pollRestartEngine_(self, timer):
        if not _engine_running():
            timer.invalidate()
            self._restart_timer = None
            _start_engine()
            return
        self._restart_ticks += 1
        if self._restart_ticks >= self._RESTART_MAX_TICKS:
            timer.invalidate()
            self._restart_timer = None
            print(
                "[vibevoice] restartEngine_: old engine.py still exiting after "
                "%.1fs, giving up (not starting a second engine)"
                % (self._RESTART_MAX_TICKS * self._RESTART_POLL_INTERVAL),
                file=sys.stderr,
            )

    def openSettings_(self, _sender):
        cfg = config.load()
        if getattr(self, "_settings_win", None):
            self._settings_win.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
            return
        W, H = 420, 380
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False)
        win.setTitle_("VibeVoice — Settings")
        win.center()
        win.setReleasedWhenClosed_(False)
        v = win.contentView()

        def _label(text, y):
            lbl = NSTextField.labelWithString_(text)
            lbl.setFrame_(NSMakeRect(20, y, 180, 22))
            v.addSubview_(lbl)
            return lbl

        def _check(title, y, on, action):
            b = NSButton.buttonWithTitle_target_action_(title, self, action)
            b.setButtonType_(3)  # NSButtonTypeSwitch (checkbox)
            b.setFrame_(NSMakeRect(200, y, 200, 22))
            b.setState_(1 if on else 0)
            v.addSubview_(b)
            return b

        _label("Language", H - 50)
        self._set_lang = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(200, H - 54, 120, 26), False)
        self._set_lang.addItemsWithTitles_(["it", "en"])
        self._set_lang.selectItemWithTitle_(cfg["lang"])
        self._set_lang.setTarget_(self)
        self._set_lang.setAction_("settingsChanged:")
        v.addSubview_(self._set_lang)

        _label("Autosend (paste)", H - 85)
        self._set_as = _check("enabled", H - 85, cfg["autosend"], "settingsChanged:")
        _label("Auto-Return", H - 115)
        self._set_ar = _check("press Return", H - 115, cfg["autosend_return"], "settingsChanged:")
        _label("Dock icon", H - 145)
        self._set_dk = _check("show in Dock", H - 145, cfg["dock"], "settingsChanged:")

        _label("History", H - 180)
        tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 40, 150))
        tv.setEditable_(False)
        sc = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 20, W - 40, 150))
        sc.setDocumentView_(tv)
        sc.setHasVerticalScroller_(True)
        v.addSubview_(sc)
        self._set_hist = tv
        self._reload_history()
        self._settings_win = win
        win.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def _reload_history(self):
        import json
        rows = []
        try:
            lines = (STATE_DIR / "history.jsonl").read_text().splitlines()
            for ln in reversed(lines):
                if not ln.strip():
                    continue
                d = json.loads(ln)
                rows.append("• %s" % d.get("text", ""))
        except (OSError, ValueError):
            rows = []
        self._set_hist.setString_("\n".join(rows) if rows else "(no transcriptions yet)")

    def settingsChanged_(self, _sender):
        cfg = {
            "lang": str(self._set_lang.titleOfSelectedItem()),
            "autosend": bool(self._set_as.state()),
            "autosend_return": bool(self._set_ar.state()),
            "dock": bool(self._set_dk.state()),
        }
        config.save(cfg)
        NSApp.setActivationPolicy_(
            NSApplicationActivationPolicyRegular if cfg["dock"]
            else NSApplicationActivationPolicyAccessory)
        # engine picks lang/autosend/autosend_return up on next spawn:
        self.restartEngine_(None)

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

    def _tts_speaking(self):
        """Return the slice of the sentence already spoken by an external TTS (a
        typewriter synced to the audio), or '' when not speaking / feature off.
        TTS_TEXT format (any TTS writes it during playback, clears it at the end):
          line 1: '<start_epoch> <duration_s>'
          line 2+: the full clean sentence
        Reveals text[:progress*len] where progress = (now-start)/duration. The
        presence of TTS_FLAG enables the feature. Back-compat: a legacy single
        line returns the whole text."""
        if not TTS_FLAG.exists():
            return ""
        try:
            raw = TTS_TEXT.read_text()
        except Exception:
            return ""
        if not raw.strip():
            return ""
        lines = raw.split("\n", 1)
        try:
            parts = lines[0].split()
            start = float(parts[0])
            dur = float(parts[1])
            full = lines[1].rstrip("\n") if len(lines) > 1 else ""
            if not full:
                return ""
            if dur <= 0.01:
                return full
            prog = max(0.0, min(1.0, (time.time() - start) / dur))
            n = max(1, int(round(prog * len(full))))
            return full[:n]
        except (ValueError, IndexError):
            return raw.strip()   # legacy single line → all of it

    def _read_tts_levels(self):
        """Waveform of the real TTS audio (rolling RMS, 60 float32 LE written by
        the external TTS). Mapped to N_BARS like the live waveform. Returns None
        when absent/torn (→ the caller uses a synthetic fallback)."""
        try:
            data = TTS_LEVELS.read_bytes()
            if len(data) < 60 * 4:
                return None
            vals = struct.unpack("<60f", data[:60 * 4])
            return [max(0.04, min(1.0, abs(vals[int(i * 60 / N_BARS)]))) for i in range(N_BARS)]
        except Exception:
            return None

    def _animate_(self, show):
        # Dynamic Island: EXPANDS from the notch / RE-COLLAPSES into it.
        # the collapsed snap stays OUTSIDE the grouping, otherwise it jumps a frame.
        if not SHOW_PILL:
            return                    # menu-bar-only: the pill panel never surfaces
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
        # control panel state (cheap stat every tick)
        self.view.muted = _flag_on(MUTED_FILE)
        self.view.locked = _flag_on(LOCKED_FILE)
        engine_on = _engine_running()
        # adaptive polling: while the pill is HIDDEN, work 1 tick in 3 (~8 fps)
        # instead of 24 fps. Cuts idle CPU. Visible → full 24 fps. Onset latency
        # ~125ms max. Compute the TTS-speaking slice once; if it speaks, don't
        # idle-skip (smooth typewriter).
        tspk = "" if (self.demo or self.place) else self._tts_speaking()
        if (not self.demo and not self.place and not self.last_active
                and not tspk and not self.view.locked):
            self._idle_skip = (getattr(self, "_idle_skip", 0) + 1) % 3
            if self._idle_skip:
                return
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
                text = "↔ drag me anywhere · position is saved"
        elif tspk:
            # TTS is speaking → pill turns RED. Waveform from the REAL TTS audio
            # (the TTS writes the rolling RMS); the text types out in sync. The
            # mic is HW-muted (anti-echo), so the waves come from the TTS audio,
            # not the mic: same rhythm as the speech, zero loop risk.
            self.view.tint = RED
            lv = self._read_tts_levels()
            if lv is not None:
                self._tts_last = lv
                levels = lv
            elif getattr(self, "_tts_last", None) is not None:
                levels = self._tts_last       # HOLD the last real frame → no mid-speech flicker
            else:
                # only the first ~100ms (audio not decoded yet) → soft synthetic wave
                ph = self.view.phase
                levels = [max(0.04, (0.5 + 0.45 * math.sin(ph * 5.0 + i * 0.5))
                                    * (0.5 + 0.5 * math.sin(ph * 1.3))
                                    * (0.6 + 0.4 * random.random())) for i in range(N_BARS)]
            text = tspk
            active = True
            self.last_voice = time.time()   # keep the pill alive while it speaks
        else:
            self.view.tint = MATRIX   # dictation input → green
            self._tts_last = None      # reset the TTS waveform hold (next turn starts clean)
            # if the mic is OFF (engine dead) don't trust levels.bin: it stays
            # frozen on the last frame, whose residual energy can read > threshold
            # → the pill would believe itself eternally active, redraw at 24 fps,
            # and stay on screen. Mic off = no voice → goes idle → hides → CPU ~0.
            # 🔇 MUTE = full pause: treat as mic-off (flat waves, no false-active).
            mic_off = (not engine_on) or self.view.muted
            state = "" if mic_off else self._read_state()
            levels = self._read_levels()
            if levels is None or mic_off:
                levels = [0.04] * N_BARS
            raw = 0.0 if mic_off else self._read_raw_energy()
            # IMMEDIATE onset: appears as soon as voice clears the noise floor (raw
            # RMS), or when the engine is recording/transcribing.
            if raw > VOICE_THRESH or state in ("recording", "transcribing"):
                self.last_voice = time.time()
            text_now = self._read_text()
            hold = 2.5 if text_now else IDLE_HIDE_S   # with text it stays visible to click copy
            active = (time.time() - self.last_voice) <= hold
            text = text_now if active else ""

        # 🔒 lock pins the pill visible regardless of silence.
        if self.view.locked:
            active = True
        # show/hide transition → native AppKit animation (fade + slide from the
        # notch), triggered ONCE on state change. The waveform updates every tick.
        if active != self.last_active:
            self._animate_(active)
            self.last_active = active
        # icon hover (poll the mouse without an event stream) — before the redraw
        try:
            mloc = self.panel.mouseLocationOutsideOfEventStream()
            vb = self.view.bounds()
            ww, hh = vb.size.width, vb.size.height
            self.view.hover_x = bool(mloc.x >= ww - 40.0 and mloc.y >= hh - 34.0)
            cr = self.view.copy_rect
            self.view.hover_c = bool(cr and cr[0] <= mloc.x <= cr[0] + cr[2]
                                     and cr[1] <= mloc.y <= cr[1] + cr[3])
            mr = self.view.muta_rect
            self.view.hover_m = bool(mr and mr[0] <= mloc.x <= mr[0] + mr[2]
                                     and mr[1] <= mloc.y <= mr[1] + mr[3])
            lr = self.view.blocca_rect
            self.view.hover_l = bool(lr and lr[0] <= mloc.x <= lr[0] + lr[2]
                                     and lr[1] <= mloc.y <= lr[1] + lr[3])
            orr = self.view.loop_rect
            self.view.hover_o = bool(orr and orr[0] <= mloc.x <= orr[0] + orr[2]
                                     and orr[1] <= mloc.y <= orr[1] + orr[3])
            # the pointing-hand cursor is handled by PillView.cursorUpdate_
            # (NSTrackingArea), not here.
        except Exception:
            pass
        self.view.setLevels_text_active_(levels, text, active)
        # SELF-HEAL the status item: on recent macOS an item created before
        # launch-finish can be parked off-screen (y<0, screen=None). Detect it →
        # destroy and recreate the item at runtime (max 3 attempts, every ~3s).
        # The floating robot covers the gap meanwhile.
        if time.time() - self.t0 > 3.0 and getattr(self, "_mb_heal", 0) < 3:
            try:
                win = self.status_item.button().window()
                fr = win.frame()
                parked = (win.screen() is None) or (fr.origin.y < 0)
                if parked:
                    self._mb_heal = getattr(self, "_mb_heal", 0) + 1
                    NSStatusBar.systemStatusBar().removeStatusItem_(self.status_item)
                    self._build_menubar()
                    self.t0 = time.time()   # give the new item 3s of grace
                else:
                    self._mb_heal = 3   # healthy: stop checking
            except Exception:
                self._mb_heal = getattr(self, "_mb_heal", 0) + 1
        # menu bar: icon + control center update only on a state change
        on = engine_on
        state_now = (on, self.view.muted, self.view.locked, self.view.autoloop)
        if state_now != self._mb_last:
            self._mb_last = state_now
            self.status_item.button().setImage_(self._make_robot_icon(on, self.view.autoloop))
            # floating robot: same state, same icon
            if getattr(self, "robot_view", None) is not None:
                self.robot_view.on = on
                self.robot_view.loop = self.view.autoloop
                self.robot_view.setNeedsDisplay_(True)
            if self.view.muted:
                st = "🔇 Paused (muted)"
            elif on:
                st = "● Listening"
            else:
                st = "○ Off"
            self.mb_status.setTitle_("🎙 VibeVoice — %s" % st)
            self.mb_toggle.setTitle_("Voice on — click to stop" if on
                                     else "Voice off — click to start")
            self.mb_mute.setTitle_("🔊 Unmute" if self.view.muted else "🔇 Mute (full pause)")
            self.mb_mute.setState_(1 if self.view.muted else 0)
            self.mb_lock.setTitle_("🔓 Unlock (auto-hide)" if self.view.locked else "🔒 Lock (stay visible)")
            self.mb_lock.setState_(1 if self.view.locked else 0)
            self.mb_loop.setTitle_("🔁 Auto-send ON — click to stop" if self.view.autoloop
                                   else "🔁 Auto-send loop — click to start")
            self.mb_loop.setState_(1 if self.view.autoloop else 0)


def main():
    ap = argparse.ArgumentParser(description="VibeVoice — Dynamic Island STT pill (MIT)")
    ap.add_argument("--demo", action="store_true", help="animated demo to preview the design")
    ap.add_argument("--place", action="store_true", help="placement mode: stays visible")
    args = ap.parse_args()

    _ensure_state_dir()

    global _CTRL, _TIMER
    app = NSApplication.sharedApplication()
    cfg = config.load()
    app.setActivationPolicy_(
        NSApplicationActivationPolicyRegular if cfg.get("dock", True)
        else NSApplicationActivationPolicyAccessory)
    _CTRL = Controller.alloc().initWithDemo_place_(args.demo, args.place)
    _TIMER = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        TICK, _CTRL, "tick:", None, True)
    NSApp.run()


if __name__ == "__main__":
    main()
