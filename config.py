#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pill-owned persistent settings (~/.vibevoice/config.json).

Leaf module: imported ONLY by vibevoice.py. The engine gets these values via
environment variables at spawn time (the pill exports them); autosend.py keeps
its own `autosend` state file. Writer and only reader: the pill.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

STATE_DIR = Path(os.path.expanduser("~")) / ".vibevoice"
CONFIG_FILE = STATE_DIR / "config.json"
CONFIG_TMP = STATE_DIR / "config.json.tmp"

# Adding a key here is backward compatible by construction: `load()` fills any key
# missing from an existing config.json with the default below, so no migration is
# needed. `vp` = macOS voice-processing capture (echo cancellation + noise
# suppression); it reaches the engine as VIBEVOICE_VP at spawn time. Default on,
# and a voice-processing failure already falls back to sounddevice (AGENTS.md rule 8).
DEFAULTS = {
    "lang": "it",
    "autosend": True,
    "autosend_return": True,
    "dock": True,
    "vp": True,
}


def load() -> dict:
    try:
        raw = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return dict(DEFAULTS)
    return {k: raw.get(k, v) for k, v in DEFAULTS.items()}


def save(cfg: dict) -> None:
    CONFIG_TMP.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_TMP.write_text(json.dumps({k: cfg[k] for k in DEFAULTS}, indent=2))
    os.replace(CONFIG_TMP, CONFIG_FILE)
