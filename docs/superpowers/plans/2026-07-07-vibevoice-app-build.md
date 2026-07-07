# VibeVoice App Build Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship VibeVoice as a real, distributable macOS app — own icon, Dock presence, settings window, embedded Python, signed, delivered as a DMG.

**Architecture:** Build ON the existing three-process design (`engine.py` · `vibevoice.py` · `autosend.py`, file contract under `~/.vibevoice/`). New pieces: a pill-owned `config.json`, a `history.jsonl` state file, an AppKit settings window inside the pill process, an icon pipeline, and a `packaging/` layer (py2app → codesign → DMG) on top of the existing lightweight `build_app.sh`.

**Tech Stack:** Python 3.10 (homebrew), PyObjC/AppKit, mlx_whisper, py2app (fallback PyInstaller), Pillow (icon gen, dev-only), iconutil, codesign/notarytool, create-dmg.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-07-vibevoice-app-build-design.md` — read it first.
- `ruff check .` and `pytest` green before EVERY commit (CI enforces on macOS).
- Tests NEVER touch the live `~/.vibevoice/` — always `tmp_path` + `monkeypatch` (see `tests/test_contract.py`).
- State-file contract changes: writer + every reader + tests in the SAME commit.
- The three processes (`engine.py`, `vibevoice.py`, `autosend.py`) never import each other. Leaf helper modules imported by ONE process are allowed.
- Bundle id: `com.vibevoice.app`. macOS 12+, Apple Silicon only (mlx).
- NEVER run this app and Mattia's legacy daily-driver at once (see CLAUDE.md "Trap"). Collaudo live: pause daily-driver first (`touch ~/.local/run/jarvis/stt_disabled` + kill PID in `~/.local/run/jarvis/stt_bar.pid`), resume at the end (remove the file, `launchctl kickstart gui/501/com.vibevoice.dictation`).
- Conventional commits. Human gates are marked ⚠️ GATE — stop and wait for Mattia.

---

### Task 1: `config.json` — pill-owned persistent settings

**Files:**
- Create: `config.py` (leaf module, imported ONLY by `vibevoice.py`)
- Create: `tests/test_config.py`
- Modify: `vibevoice.py` (read config at startup: Dock policy + engine env)

**Interfaces:**
- Produces: `config.load() -> dict`, `config.save(cfg: dict) -> None`, `config.DEFAULTS` — exactly:
  ```python
  DEFAULTS = {"lang": "it", "autosend": True, "autosend_return": True, "dock": True}
  def load() -> dict   # DEFAULTS overlaid with ~/.vibevoice/config.json; corrupt/missing file → DEFAULTS
  def save(cfg: dict) -> None  # atomic write (tmp + os.replace)
  ```
- Consumed by: Task 3 (settings window), Task 1 itself (startup wiring).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Contract tests for config.json — pill-owned settings (never touch live ~/.vibevoice)."""
from __future__ import annotations

import json

import config


def _redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_TMP", tmp_path / "config.json.tmp")


def test_load_missing_file_returns_defaults(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    assert config.load() == config.DEFAULTS


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    cfg = dict(config.DEFAULTS, lang="en", dock=False)
    config.save(cfg)
    assert config.load() == cfg
    assert not (tmp_path / "config.json.tmp").exists()  # atomic: no staging left


def test_corrupt_file_returns_defaults(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    (tmp_path / "config.json").write_text("{not json")
    assert config.load() == config.DEFAULTS


def test_unknown_keys_dropped_known_kept(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    (tmp_path / "config.json").write_text(json.dumps({"lang": "en", "evil": 1}))
    cfg = config.load()
    assert cfg["lang"] == "en"
    assert "evil" not in cfg
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_config.py -q` → FAIL (`ModuleNotFoundError: config`)

- [ ] **Step 3: Minimal implementation**

```python
# config.py
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

DEFAULTS = {"lang": "it", "autosend": True, "autosend_return": True, "dock": True}


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
```

- [ ] **Step 4: Run to verify PASS** — `pytest tests/test_config.py -q` → 4 passed

- [ ] **Step 5: Wire into `vibevoice.py` startup.** In `main()` (bottom of file, currently `app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)`):
  - add `import config` near the other stdlib imports;
  - add `NSApplicationActivationPolicyRegular` to the existing AppKit import list (it sits next to `NSApplicationActivationPolicyAccessory`, ~line 104);
  - replace the policy line with:

```python
    cfg = config.load()
    app.setActivationPolicy_(
        NSApplicationActivationPolicyRegular if cfg.get("dock", True)
        else NSApplicationActivationPolicyAccessory)
```

  - in the engine-autostart branch (~line 681, `VIBEVOICE_ENGINE_AUTOSTART == "1"`), find where the engine subprocess env is assembled and export the config before spawn:

```python
        env = dict(os.environ)
        env.setdefault("VIBEVOICE_LANG", cfg["lang"])
        env.setdefault("VIBEVOICE_AUTOSEND", "1" if cfg["autosend"] else "0")
        env.setdefault("VIBEVOICE_AUTOSEND_RETURN", "1" if cfg["autosend_return"] else "0")
```

  (pass `env=env` to the existing `subprocess.Popen` — `setdefault` so explicit env vars still win, launcher compatibility preserved.)

- [ ] **Step 6: Full gate + commit**

```bash
ruff check . && pytest -q
git add config.py tests/test_config.py vibevoice.py
git commit -m "feat(config): pill-owned config.json — Dock policy + engine env from settings"
```

---

### Task 2: `history.jsonl` — last transcriptions in the state contract

**Files:**
- Modify: `engine.py` (writer — right after it writes `raw.txt`)
- Modify: `vibevoice.py` line ~44 header comment (document the new file) — reader arrives in Task 3
- Test: `tests/test_contract.py` (append)

**Interfaces:**
- Produces: `~/.vibevoice/history.jsonl` — one JSON object per line, `{"ts": float, "text": str}`, newest LAST, capped at `HISTORY_MAX = 20` lines. Engine-side names: `HISTORY_FILE`, `HISTORY_MAX`, `_append_history(text: str) -> None`.

- [ ] **Step 1: Failing test (append to `tests/test_contract.py`)**

```python
# ── history.jsonl: last transcriptions (settings window reads this) ───────────

@pytest.fixture
def history_state(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "HISTORY_FILE", tmp_path / "history.jsonl")
    return tmp_path


def test_history_appends_and_caps(history_state):
    import json
    for i in range(25):
        engine._append_history(f"frase {i}")
    lines = (history_state / "history.jsonl").read_text().splitlines()
    assert len(lines) == engine.HISTORY_MAX == 20
    assert json.loads(lines[-1])["text"] == "frase 24"   # newest last
    assert set(json.loads(lines[0])) == {"ts", "text"}


def test_history_write_failure_never_raises(history_state, monkeypatch):
    monkeypatch.setattr(engine, "HISTORY_FILE", history_state / "no" / "dir.jsonl")
    engine._append_history("must not raise")  # transcription path must survive
```

- [ ] **Step 2: Run** — `pytest tests/test_contract.py -q` → FAIL (`no attribute 'HISTORY_FILE'`)

- [ ] **Step 3: Implement in `engine.py`.** Next to `RAW_FILE` (~line 84): `HISTORY_FILE = STATE_DIR / "history.jsonl"` and `HISTORY_MAX = 20`. Then:

```python
def _append_history(text: str) -> None:
    """Append to history.jsonl, newest last, capped. Never raises (transcription path)."""
    try:
        import json, time
        lines = []
        try:
            lines = HISTORY_FILE.read_text().splitlines()
        except OSError:
            pass
        lines.append(json.dumps({"ts": time.time(), "text": text}))
        HISTORY_FILE.write_text("\n".join(lines[-HISTORY_MAX:]) + "\n")
    except Exception:
        pass
```

Call `_append_history(text)` at the exact site where `RAW_FILE` is written after a successful transcription. Update the STATE-FILE CONTRACT header comments in BOTH `engine.py` (~line 36) and `vibevoice.py` (~line 44): `~/.vibevoice/history.jsonl  last 20 transcriptions, JSONL {"ts","text"}, newest last`.

- [ ] **Step 4: Run** — `pytest -q` → all green

- [ ] **Step 5: Commit** — `git add engine.py vibevoice.py tests/test_contract.py && git commit -m "feat(engine): history.jsonl in the state contract — last 20 transcriptions"`

---

### Task 3: Settings window (AppKit, pill process) + menu entry

**Files:**
- Modify: `vibevoice.py` only (window lives in the pill process — invariant #3)

**Interfaces:**
- Consumes: `config.load/save` (Task 1), `~/.vibevoice/history.jsonl` (Task 2), existing `AUTOSEND_FILE` toggle plumbing (`toggleLoop:` already exists).
- Produces: `Controller.openSettings:` action; menu item `⚙️ Settings…` in the shared menu (`_build_menu`, ~line 858).

- [ ] **Step 1: Add menu item** after the `mb_loop` item (~line 875):

```python
        st = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("⚙️ Settings…", "openSettings:", ",")
        st.setTarget_(self)
        menu.addItem_(st)
```

- [ ] **Step 2: Implement the window** — add to `Controller`:

```python
    def openSettings_(self, _sender):
        import config as _cfg
        cfg = _cfg.load()
        if getattr(self, "_settings_win", None):
            self._settings_win.makeKeyAndOrderFront_(None); NSApp.activateIgnoringOtherApps_(True); return
        W, H = 420, 380
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable, 2, False)
        win.setTitle_("VibeVoice — Settings"); win.center(); win.setReleasedWhenClosed_(False)
        v = win.contentView()

        def _label(text, y):
            l = NSTextField.labelWithString_(text); l.setFrame_(NSMakeRect(20, y, 180, 22)); v.addSubview_(l); return l

        def _check(title, y, on, action):
            b = NSButton.buttonWithTitle_target_action_(title, self, action)
            b.setButtonType_(1); b.setFrame_(NSMakeRect(200, y, 200, 22)); b.setState_(1 if on else 0)
            v.addSubview_(b); return b

        _label("Language", H - 50)
        self._set_lang = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(200, H - 54, 120, 26), False)
        self._set_lang.addItemsWithTitles_(["it", "en"]); self._set_lang.selectItemWithTitle_(cfg["lang"])
        self._set_lang.setTarget_(self); self._set_lang.setAction_("settingsChanged:"); v.addSubview_(self._set_lang)

        _label("Autosend (paste)", H - 85);  self._set_as  = _check("enabled", H - 85, cfg["autosend"], "settingsChanged:")
        _label("Auto-Return", H - 115);      self._set_ar  = _check("press Return", H - 115, cfg["autosend_return"], "settingsChanged:")
        _label("Dock icon", H - 145);        self._set_dk  = _check("show in Dock", H - 145, cfg["dock"], "settingsChanged:")

        _label("History", H - 180)
        tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 40, 150)); tv.setEditable_(False)
        sc = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 20, W - 40, 150)); sc.setDocumentView_(tv); sc.setHasVerticalScroller_(True)
        v.addSubview_(sc); self._set_hist = tv
        self._reload_history()
        self._settings_win = win
        win.makeKeyAndOrderFront_(None); NSApp.activateIgnoringOtherApps_(True)

    def _reload_history(self):
        import json
        rows = []
        try:
            for ln in (STATE_DIR / "history.jsonl").read_text().splitlines()[::-1]:
                d = json.loads(ln); rows.append(f"• {d['text']}")
        except OSError:
            rows = ["(no transcriptions yet)"]
        self._set_hist.setString_("\n".join(rows) or "(empty)")

    def settingsChanged_(self, _sender):
        import config as _cfg
        cfg = {"lang": str(self._set_lang.titleOfSelectedItem()),
               "autosend": bool(self._set_as.state()),
               "autosend_return": bool(self._set_ar.state()),
               "dock": bool(self._set_dk.state())}
        _cfg.save(cfg)
        NSApp.setActivationPolicy_(
            NSApplicationActivationPolicyRegular if cfg["dock"] else NSApplicationActivationPolicyAccessory)
        # engine picks lang/autosend up on next restart; reuse the existing restart plumbing:
        self.restartEngine_(None) if hasattr(self, "restartEngine_") else None
```

Add the needed AppKit names to the import list if missing: `NSWindow, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable, NSMakeRect, NSTextField, NSButton, NSPopUpButton, NSTextView, NSScrollView, NSApplicationActivationPolicyRegular`. Check first with `grep -n "restartEngine\|restartPill" vibevoice.py` — reuse whatever engine-restart action already exists (there is a `restartPill:` at ~line 877; if no engine restart exists, kill via the same `pkill -f engine.py` pattern the file already uses and respawn with the Task 1 env block).

- [ ] **Step 3: Lint + tests** — `ruff check . && pytest -q` → green (window code is exercised manually; contract tests must stay green)

- [ ] **Step 4: Manual verify (effect, not UI)** — run `python3 vibevoice.py` (daily-driver PAUSED per Global Constraints):
  - menu → ⚙️ Settings… opens; toggle Dock OFF → icon leaves the Dock live; back ON → returns
  - switch Language to `en` → `cat ~/.vibevoice/config.json` shows `"lang": "en"`
  - History pane lists recent lines from `history.jsonl`
  - quit app, relaunch → settings persisted

- [ ] **Step 5: Commit** — `git add vibevoice.py && git commit -m "feat(pill): native settings window — lang, autosend, Dock toggle, history"`

---

### Task 4: Icon — candidates ⚠️ GATE, then `.icns` into the bundle

**Files:**
- Create: `assets/icon/make_icon.py` (dev-only generator, Pillow)
- Create: `assets/icon/VibeVoice.icns` (generated artifact, committed)
- Modify: `build_app.sh` (copy icns + `CFBundleIconFile`)
- Test: `tests/test_app_bundle.py` (append)

**Interfaces:**
- Produces: `make_icon.py --variant {1,2,3} --out <png>` (1024×1024 RGBA) and the final `VibeVoice.icns`.

- [ ] **Step 1: Write the generator**

```python
# assets/icon/make_icon.py
#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate VibeVoice icon candidates: teal squircle + waveform.

Scar sess.9161: the squircle must live INSIDE the canvas with a transparent
margin (~10%), never edge-to-edge, or macOS wraps it in a grey rounded rect.

Usage: python3 make_icon.py --variant 1 --out candidate1.png
"""
from __future__ import annotations

import argparse

from PIL import Image, ImageDraw

S = 1024
M = int(S * 0.10)                    # transparent margin (the scar)
R = int((S - 2 * M) * 0.225)         # macOS squircle-ish corner radius

PALETTES = {1: ((13, 148, 136), (4, 47, 46)),    # teal → deep teal (AI Accelerator family)
            2: ((45, 212, 191), (15, 118, 110)), # bright aqua → teal
            3: ((20, 184, 166), (2, 26, 25))}    # teal → near-black

BARS = {1: [.30, .55, .90, .65, 1.0, .70, .45, .25],
        2: [.20, .45, .75, 1.0, .75, .45, .20],
        3: [.35, .70, 1.0, .55, .85, .40]}


def build(variant: int) -> Image.Image:
    top, bot = PALETTES[variant]
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    for y in range(S):
        t = y / S
        grad.paste(tuple(int(a + (b - a) * t) for a, b in zip(top, bot)) + (255,), (0, y, S, y + 1))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([M, M, S - M, S - M], radius=R, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    bars = BARS[variant]
    bw = int((S - 2 * M) * 0.055)
    gap = int((S - 2 * M) * 0.045)
    total = len(bars) * bw + (len(bars) - 1) * gap
    x = (S - total) // 2
    max_h = (S - 2 * M) * 0.52
    for h in bars:
        bh = int(max_h * h)
        d.rounded_rectangle([x, (S - bh) // 2, x + bw, (S + bh) // 2], radius=bw // 2, fill=(255, 255, 255, 235))
        x += bw + gap
    return img


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", type=int, default=1, choices=sorted(PALETTES))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    build(a.variant).save(a.out)
    print(f"wrote {a.out}")
```

- [ ] **Step 2: Generate + show candidates** (`python3 -m pip show pillow || pip3 install pillow`):

```bash
cd assets/icon
for v in 1 2 3; do python3 make_icon.py --variant $v --out candidate$v.png; done
open -a Safari candidate1.png candidate2.png candidate3.png
```

- [ ] **Step 3: ⚠️ GATE — Mattia picks the variant.** Stop. Do not proceed on his behalf.

- [ ] **Step 4: Build the `.icns`** (replace `N` with the chosen variant):

```bash
cd assets/icon && mkdir -p VibeVoice.iconset
for sz in 16 32 64 128 256 512; do
  sips -z $sz $sz candidateN.png --out VibeVoice.iconset/icon_${sz}x${sz}.png >/dev/null
  sips -z $((sz*2)) $((sz*2)) candidateN.png --out VibeVoice.iconset/icon_${sz}x${sz}@2x.png >/dev/null
done
iconutil -c icns VibeVoice.iconset -o VibeVoice.icns && rm -rf VibeVoice.iconset
```

- [ ] **Step 5: Wire into `build_app.sh`** — after the `cp` of sources add `cp "$SRC/assets/icon/VibeVoice.icns" "$APP/Contents/Resources/"`, and in the Info.plist heredoc add:

```xml
    <key>CFBundleIconFile</key>
    <string>VibeVoice</string>
```

- [ ] **Step 6: Failing→passing bundle test (append to `tests/test_app_bundle.py`)**

```python
def test_bundle_has_icon(app: Path):
    assert (app / "Contents/Resources/VibeVoice.icns").stat().st_size > 10_000
    with (app / "Contents/Info.plist").open("rb") as f:
        assert plistlib.load(f)["CFBundleIconFile"] == "VibeVoice"


def test_icon_respects_transparent_margin():
    """Scar sess.9161: corners must be transparent (squircle inside the canvas)."""
    PIL = pytest.importorskip("PIL.Image")
    img = PIL.open(REPO / "assets/icon/VibeVoice.icns")  # PIL reads icns largest rep
    img = img.convert("RGBA")
    w, h = img.size
    for xy in [(2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3)]:
        assert img.getpixel(xy)[3] == 0, f"corner {xy} not transparent"
```

- [ ] **Step 7: Run** — `pytest tests/test_app_bundle.py -q` → green; `ruff check .` clean

- [ ] **Step 8: Verify live icon** — `./build_app.sh && open dist/` → VibeVoice.app shows the icon in Finder (if stale: `/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f dist/VibeVoice.app`)

- [ ] **Step 9: Commit** — `git add assets/ build_app.sh tests/test_app_bundle.py && git commit -m "feat(icon): teal squircle waveform icon + bundle wiring (variant N, Mattia-approved)"`

---

### Task 5: `packaging/` — self-contained .app via py2app

**Files:**
- Create: `packaging/setup_py2app.py`, `packaging/build_release.sh`
- Modify: `.gitignore` (add `packaging/build/`, `packaging/dist/`)

**Interfaces:**
- Produces: `packaging/dist/VibeVoice.app` fully self-contained (embedded Python). `build_release.sh` is the ONE entrypoint (py2app now; signing/DMG appended by Task 6).

- [ ] **Step 1: setup file**

```python
# packaging/setup_py2app.py
#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""py2app build: python3 packaging/setup_py2app.py py2app (run from repo root).

Fallback if py2app can't cope (document WHY in the commit):
  pyinstaller --windowed --name VibeVoice --icon assets/icon/VibeVoice.icns vibevoice.py
  then: check libportaudio via `otool -L` and graft with install_name_tool.
"""
from setuptools import setup

APP = ["vibevoice.py"]
DATA_FILES = ["engine.py", "autosend.py", "config.py", "requirements.txt"]
OPTIONS = {
    "iconfile": "assets/icon/VibeVoice.icns",
    "packages": ["numpy", "sounddevice", "mlx_whisper"],
    "includes": ["objc", "AppKit", "Foundation", "Quartz", "config"],
    "plist": {
        "CFBundleName": "VibeVoice",
        "CFBundleDisplayName": "VibeVoice",
        "CFBundleIdentifier": "com.vibevoice.app",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "LSMinimumSystemVersion": "12.0",
        "LSUIElement": False,  # Dock ON by default; runtime policy follows config.json
        "NSMicrophoneUsageDescription": "VibeVoice transcribes your voice into text, locally on your Mac.",
        "NSAppleEventsUsageDescription": "VibeVoice uses AppleScript to detect the frontmost app and press Return after dictation.",
    },
}

setup(app=APP, data_files=DATA_FILES, options={"py2app": OPTIONS}, setup_requires=["py2app"])
```

- [ ] **Step 2: build script**

```bash
# packaging/build_release.sh
#!/bin/bash
# SPDX-License-Identifier: MIT
# Self-contained VibeVoice.app (py2app). Run from repo root: bash packaging/build_release.sh
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip show py2app >/dev/null || python3 -m pip install py2app
rm -rf packaging/build packaging/dist
python3 packaging/setup_py2app.py py2app --dist-dir packaging/dist --bdist-base packaging/build
APP="packaging/dist/VibeVoice.app"
echo "── smoke: bundle is self-contained ──"
plutil -lint "$APP/Contents/Info.plist"
test -x "$APP/Contents/MacOS/VibeVoice"
test -d "$APP/Contents/Resources/lib" && echo "embedded python: OK"
echo "Built: $APP"
```

- [ ] **Step 3: Build** — `chmod +x packaging/build_release.sh && bash packaging/build_release.sh`. Expected: `Built: packaging/dist/VibeVoice.app`. If py2app chokes on mlx_whisper/sounddevice: try `"packages"` additions first, then the PyInstaller fallback in the setup docstring — record the failure verbatim in the commit body.

- [ ] **Step 4: Self-contained smoke (no homebrew)** — daily-driver PAUSED:

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin open packaging/dist/VibeVoice.app
sleep 8; stat -f %m ~/.vibevoice/levels.bin; sleep 3; stat -f %m ~/.vibevoice/levels.bin  # mtime must advance
osascript -e 'quit app "VibeVoice"'
```

First launch will prompt Microphone + Accessibility (new TCC identity) and download the whisper model — both expected.

- [ ] **Step 5: Commit** — `git add packaging/ .gitignore && git commit -m "feat(packaging): self-contained VibeVoice.app via py2app"`

---

### Task 6: Sign, notarize (best-effort), DMG

**Files:**
- Create: `packaging/entitlements.plist`, `packaging/make_dmg.sh`
- Modify: `packaging/build_release.sh` (append sign + dmg stages)

- [ ] **Step 1: entitlements**

```xml
<!-- packaging/entitlements.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>com.apple.security.device.audio-input</key><true/>
    <key>com.apple.security.automation.apple-events</key><true/>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
    <key>com.apple.security.cs.disable-library-validation</key><true/>
</dict></plist>
```

(the two `cs.*` keys are required: py2app ships its own dylibs and mlx JITs)

- [ ] **Step 2: append to `build_release.sh`**

```bash
echo "── codesign ──"
IDENTITY="$(security find-identity -v -p codesigning | awk -F'"' '/Developer ID Application/{print $2; exit}')"
if [ -n "$IDENTITY" ]; then
    codesign --force --deep --options runtime \
        --entitlements packaging/entitlements.plist -s "$IDENTITY" "$APP"
    codesign --verify --deep --strict "$APP" && echo "signed: $IDENTITY"
else
    echo "⚠️  no Developer ID Application cert — UNSIGNED build"
fi
echo "── notarize (best-effort) ──"
if xcrun notarytool history --keychain-profile vibevoice-notary >/dev/null 2>&1; then
    ditto -c -k --keepParent "$APP" packaging/dist/VibeVoice.zip
    xcrun notarytool submit packaging/dist/VibeVoice.zip --keychain-profile vibevoice-notary --wait
    xcrun stapler staple "$APP"
else
    echo "⚠️  notary profile 'vibevoice-notary' missing — skipping (GATE: Mattia must run:"
    echo "    xcrun notarytool store-credentials vibevoice-notary --apple-id <id> --team-id <team> --password <app-specific-pw>)"
fi
bash packaging/make_dmg.sh "$APP"
```

- [ ] **Step 3: DMG script**

```bash
# packaging/make_dmg.sh
#!/bin/bash
# SPDX-License-Identifier: MIT
set -euo pipefail
APP="${1:?usage: make_dmg.sh path/to/VibeVoice.app}"
OUT="$(dirname "$APP")/VibeVoice.dmg"
rm -f "$OUT"
STAGE="$(mktemp -d)"; cp -R "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "VibeVoice" -srcfolder "$STAGE" -ov -format UDZO "$OUT"
rm -rf "$STAGE"
echo "DMG: $OUT"
```

- [ ] **Step 4: Run full pipeline** — `bash packaging/build_release.sh`. Expected: `signed: Developer ID Application: …` (cert is in the keychain, sess.9157), the notary ⚠️ skip message (password not yet created — that's the known GATE, build must NOT block), `DMG: packaging/dist/VibeVoice.dmg`.

- [ ] **Step 5: Verify installability**

```bash
codesign --verify --deep --strict packaging/dist/VibeVoice.app && echo SIGN-OK
spctl -a -vv packaging/dist/VibeVoice.app || echo "spctl rejected: EXPECTED until notarized"
hdiutil attach packaging/dist/VibeVoice.dmg && ls /Volumes/VibeVoice && hdiutil detach /Volumes/VibeVoice
```

- [ ] **Step 6: Commit** — `git add packaging/ && git commit -m "feat(packaging): codesign + best-effort notarize + DMG"`

---

### Task 7: End-to-end collaudo + handoff

**Files:** none new (checklist run; fixes go where they belong)

- [ ] **Step 1: Pause daily-driver** (Global Constraints command) — verify `pgrep -f stt_bar.py` empty
- [ ] **Step 2: Install from DMG** — mount, drag to `/Applications` (replaces the legacy shell bundle: confirm with Mattia ⚠️ GATE, it's HIS daily launcher), or install to `~/Applications` to keep both
- [ ] **Step 3: Launch, grant Microphone + Accessibility, wait for model download**
- [ ] **Step 4: Dictate a real phrase** → text lands in frontmost app; `~/.vibevoice/history.jsonl` gained a line; pill waveform reacted
- [ ] **Step 5: Settings effect-check** — flip Dock OFF/ON live; switch lang it↔en and dictate again; toggle auto-Return and confirm behavioral change
- [ ] **Step 6: Full suite** — `ruff check . && pytest -q` green
- [ ] **Step 7: Resume daily-driver** — `rm ~/.local/run/jarvis/stt_disabled && launchctl kickstart gui/501/com.vibevoice.dictation`; verify `pgrep -f stt_bar.py` alive (Mattia keeps dictating with the legacy system until HE decides to switch)
- [ ] **Step 8: Push + close** — `git push origin main`; report DoD table (spec §Collaudo, items 1-8) with PASS/FAIL each
