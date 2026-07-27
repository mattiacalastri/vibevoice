#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pure logic of the hardware-look floating widget (no GUI, no live files).

The widget's LOOK is verified behaviorally (tools/smoke_hardware_widget.py
renders it to PNGs); these lock the two pure mappings it draws from: the
60-sample RMS history → N VU bars, and the engine state → LED tint.
"""
from __future__ import annotations

import pytest

from vibevoice import widget_bar_color, widget_bar_levels, widget_led, WIDGET_BARS


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
