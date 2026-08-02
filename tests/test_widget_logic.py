#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pure logic of the hardware-look floating widget (no GUI, no live files).

The widget's LOOK is verified behaviorally (tools/smoke_hardware_widget.py
renders it to PNGs); these lock the two pure mappings it draws from: the
60-sample RMS history → N VU bars, and the engine state → LED tint.
"""
from __future__ import annotations

import pytest

from vibevoice import (
    clamp_to_visible,
    first_run_defaults,
    widget_bar_color,
    widget_bar_levels,
    widget_led,
    WIDGET_BARS,
)


# ── a saved position must not become a trap ──────────────────────────────────
# The widget restores WIDGET_POS with no screen constraint. Drag it onto an
# external monitor, unplug, relaunch: the panel is born off-screen, and
# toggling it off and on only calls orderFrontRegardless — it never
# repositions. With SHOW_PILL False that is the entire floating surface, and
# the only cure was deleting a file by hand. Found by review 2026-08-02.

def test_a_position_on_screen_is_left_alone():
    screens = [(0.0, 0.0, 1440.0, 900.0)]
    assert clamp_to_visible(200.0, 300.0, 300.0, 90.0, screens) == (200.0, 300.0)


def test_a_position_from_a_vanished_monitor_comes_back():
    """The external screen is gone; the saved origin is far outside it."""
    screens = [(0.0, 0.0, 1440.0, 900.0)]
    x, y = clamp_to_visible(3200.0, 1400.0, 300.0, 90.0, screens)
    assert 0.0 <= x <= 1440.0 - 300.0
    assert 0.0 <= y <= 900.0 - 90.0


def test_a_second_monitor_is_still_allowed():
    """Clamping must not drag the widget home from a screen that is present."""
    screens = [(0.0, 0.0, 1440.0, 900.0), (1440.0, 0.0, 1920.0, 1080.0)]
    assert clamp_to_visible(2000.0, 500.0, 300.0, 90.0, screens) == (2000.0, 500.0)


def test_no_screens_at_all_changes_nothing():
    """Never invent a position when we cannot know where the screens are."""
    assert clamp_to_visible(50.0, 60.0, 300.0, 90.0, []) == (50.0, 60.0)


# ── first run: an app you open must show you something ───────────────────────
# SHOW_PILL is False, so the notch pill never appears; the visible surface is
# the floating hardware widget, and its flag lives in the state dir. A packaged
# app installed fresh therefore opened with nothing on screen but a menu-bar
# icon and looked dead — which is exactly what happened to the first build,
# 2026-08-02.

def test_first_run_turns_the_visible_widget_on(tmp_path):
    """A state dir nobody has configured yet gets the widget shown."""
    created = first_run_defaults(tmp_path)

    assert tmp_path / "widget" in created
    assert (tmp_path / "widget").exists()


def test_first_run_leaves_a_marker_so_it_happens_once(tmp_path):
    first_run_defaults(tmp_path)
    (tmp_path / "widget").unlink()          # the user turned it off

    assert first_run_defaults(tmp_path) == []
    assert not (tmp_path / "widget").exists(), "a deliberate choice must survive"


def test_first_run_does_not_touch_an_existing_setup(tmp_path):
    """An upgrade must not re-enable what the user had already decided."""
    (tmp_path / "configured").touch()

    assert first_run_defaults(tmp_path) == []
    assert not (tmp_path / "widget").exists()


# ── widget_bar_levels ─────────────────────────────────────────────────────────

def test_bar_levels_downsamples_to_requested_width():
    levels = [i / 60.0 for i in range(60)]
    bars = widget_bar_levels(levels, n=12)
    assert len(bars) == 12
    assert bars[0] < bars[-1]  # rising input → rising bars


def test_bar_levels_takes_the_peak_per_bucket():
    levels = [0.0] * 60
    levels[7] = 0.9  # a single spike must survive downsampling (VU behavior)
    bars = widget_bar_levels(levels, n=12)
    assert max(bars) == pytest.approx(0.9)


def test_bar_levels_clamps_and_handles_empty():
    assert widget_bar_levels([], n=8) == [0.0] * 8
    bars = widget_bar_levels([5.0] * 60, n=8)
    assert all(b == 1.0 for b in bars)


def test_bar_levels_default_width_is_the_module_constant():
    assert len(widget_bar_levels([0.5] * 60)) == WIDGET_BARS


# ── widget_led ────────────────────────────────────────────────────────────────

def test_led_engine_off_is_grey():
    r, g, b = widget_led(state="", muted=False, engine_on=False)
    assert r == g == b  # achromatic


def test_led_muted_wins_over_state():
    tint = widget_led(state="recording", muted=True, engine_on=True)
    assert tint == widget_led(state="idle", muted=True, engine_on=True)


def test_led_recording_is_red_dominant():
    r, g, b = widget_led(state="recording", muted=False, engine_on=True)
    assert r > g and r > b


def test_led_listening_is_green_dominant():
    r, g, b = widget_led(state="idle", muted=False, engine_on=True)
    assert g > r and g > b


# ── widget_bar_color ──────────────────────────────────────────────────────────

def test_bar_color_red_only_near_clipping():
    """Apple's AGC pushes normal speech well past 0.85 — red must mean actual
    clipping, not a loud voice (feedback sess.9685: 'vedo troppo rosso')."""
    r, g, b, _a = widget_bar_color(0.90, engine_on=True)
    assert g > r, "0.90 must still be green"
    r, g, b, _a = widget_bar_color(0.99, engine_on=True)
    assert r > g, "clipping level must be red"


def test_bar_color_engine_off_is_dim():
    _r, _g, _b, a = widget_bar_color(0.5, engine_on=False)
    assert a < 0.95
