# AGENTS.md — VibeVoice

Orientation for AI coding agents working in this repository. Read this before
editing. It captures the architecture, the **invariants you must not break**, and
how to run and verify changes. (Human contributors: see `README.md` first; this
file is the engineering contract underneath it.)

---

## 1. What this is

VibeVoice is a macOS speech-to-text utility with a "Dynamic Island" UI. You speak,
it transcribes on-device with Whisper (Apple Silicon / MLX), and it pastes the text
into whatever app is frontmost — optionally pressing Return so a dictated sentence is
*sent*.

It is built as **three decoupled processes** that never import each other. They
communicate **only through small files** under `~/.vibevoice/`. This decoupling is
the single most important design property of the codebase — preserve it.

```
  microphone ──► engine.py ──► ~/.vibevoice/{state,levels.bin,raw.txt} ──► vibevoice.py (the pill UI)
                    │                                                          ▲
                    └──► pbcopy + Cmd+V into frontmost app                     │ menu-bar icon
                                                                               │ launches / kills engine.py
  global keystrokes ──► autosend.py ──► simulated Return  (independent, shares nothing but an optional pause flag)
```

| File | Role | Process kind |
|------|------|--------------|
| `engine.py` | Mic capture → energy VAD → Whisper transcription → paste. **Sole writer** of the state files. | headless daemon |
| `vibevoice.py` | The "pill": borderless NSPanel under the notch. **Reads** the state files and draws. Menu-bar icon is the master switch that launches/kills `engine.py`. | AppKit GUI |
| `autosend.py` | Standalone `pynput` daemon that presses Return after typing goes quiet. **One-shot** by design. | headless daemon |

---

## 2. The state-file contract (the spine)

Everything flows through `~/.vibevoice/`. **`engine.py` is the only writer** of the
first three files; `vibevoice.py` is strictly a reader. `autosend.py` owns only its
own `autosend` flag.

| Path | Format | Writer | Reader |
|------|--------|--------|--------|
| `~/.vibevoice/state` | text: `idle` \| `recording` \| `transcribing` | engine | pill |
| `~/.vibevoice/levels.bin` | **exactly 60 × float32 little-endian**, RMS 0..1, written atomically (`tmp` + `os.replace`) | engine | pill |
| `~/.vibevoice/raw.txt` | last transcription, plain text (the sentence only) | engine | pill |
| `~/.vibevoice/autosend` | text: `on` \| `off` (armed state) | autosend.py | autosend.py |
| `/tmp/vibevoice_autosend_pause` | unix timestamp; suspends autosend for `PAUSE_TTL_SECONDS` (60s, anti-deadlock) | external tools | autosend.py |

If you change this contract, you must change **both** the writer and every reader in
the same commit. The `60` in `levels.bin` is duplicated as `LEVELS_LEN` (engine) and a
hard-coded `60` in the pill's `struct.unpack("<60f", ...)` — keep them in lockstep.

---

## 3. Hard invariants — DO NOT break these

These are load-bearing. Violating one produces a regression that is hard to spot
because the code keeps "working" in the happy path.

1. **Engine is the sole writer of `state` / `levels.bin` / `raw.txt`.** Never make the
   pill write them. The pill only reads + draws.
2. **`levels.bin` is exactly 60 float32 LE, written atomically.** The pill guards
   against torn reads (`if len(data) < 60*4: skip frame`). Keep the atomic
   `tmp + os.replace` write and keep both sides agreeing on `60`.
3. **The audio callback (`Engine._audio_callback`) must never raise.** It runs on the
   realtime `sounddevice` thread; it swallows all exceptions on purpose. File I/O and
   thread spawning are deferred to *outside* the lock. Do not add work that can throw
   or block inside the lock.
4. **Keep `self._busy = threading.Semaphore(2)`.** Two transcriptions may be in flight
   so the tail of a long utterance isn't dropped while the previous blob is still being
   transcribed. Reverting to `Semaphore(1)` reintroduces the dropped-monologue bug
   (fixed in commit `9e6ee0e`, "sustain rhythm on long dictation").
5. **`MAX_DUR = 15.0` is deliberate, not arbitrary.** It force-finalizes an utterance so
   each audio blob stays within the recognizer's comfort window and long dictation keeps
   a steady cadence. Don't bump it back up to 30 without re-testing long monologues.
6. **Paste uses `CGEventPost` at `kCGHIDEventTap`** (key codes V=9, Return=36). This is
   what lets the keystroke reach **sandboxed Electron-based editors**. Keep the
   `osascript` path as the no-PyObjC fallback — don't delete it.
7. **`autosend.py` is one-shot.** After it fires one Return it disarms itself
   (`set_enabled(False)`). This prevents a "zombie ON" state from pressing Return while
   the user later types by hand. Do not make it persistent-by-default.
8. **The master switch finds the engine by process name `engine.py`** (`pgrep -f` /
   `pkill -f` in the pill). If you rename `engine.py`, you break start/stop/“is it
   running” detection in `vibevoice.py`. Update all three call sites if you must rename.
9. **The three processes share no Python imports.** Coupling is via files only. Do not
   "simplify" by importing `engine` into `vibevoice` (or vice-versa) — it would couple
   their lifecycles and defeat the crash-isolation the file contract buys.
10. **`engine.py` and `vibevoice.py` must stay siblings.** The pill resolves the engine
    as `Path(__file__).parent / "engine.py"`. Moving one without the other breaks launch.

---

## 4. Two independent Return mechanisms (common confusion)

There are **two** separate ways a Return can be pressed. They do not know about each
other and can both fire:

- **In-engine** (`engine.autosend`): pastes via Cmd+V and, *only if*
  `VIBEVOICE_AUTOSEND_RETURN=1`, presses Return after `RETURN_DELAY` (1.5s).
- **Standalone** (`autosend.py`): a `pynput` daemon that watches *all* typing and fires
  Return after `AUTO_SEND_DELAY` (0.8s) of silence in a target app, then disarms.

If you are debugging "Return fired twice" or "Return fired unexpectedly," check whether
both are active. They are intentionally orthogonal — `autosend.py` works with any STT,
not just this engine.

---

## 5. Run & develop

```bash
pip install -r requirements.txt        # pyobjc, mlx-whisper, sounddevice, numpy
pip install pynput                     # only needed for autosend.py

python3 vibevoice.py                   # live pill (reads engine state files)
python3 vibevoice.py --demo            # animated preview, no mic — use to iterate on UI
python3 vibevoice.py --place           # placement mode: pill stays visible

python3 engine.py                      # run the capture/transcription engine standalone
python3 autosend.py --delay 1.0        # standalone auto-Return daemon
```

LaunchAgents: `com.vibevoice.pill.plist`, `com.vibevoice.autosend.plist`.

### Environment variables (engine)
| Var | Default | Meaning |
|-----|---------|---------|
| `VIBEVOICE_LANG` | `it` | Whisper language code |
| `VIBEVOICE_MODEL` | `mlx-community/whisper-turbo` | mlx_whisper model id (downloaded on first use) |
| `VIBEVOICE_AUTOSEND` | `1` | paste transcription into frontmost app |
| `VIBEVOICE_AUTOSEND_RETURN` | `0` | press Return after pasting |

### macOS permissions (changes here are usually permission problems, not code bugs)
- **Microphone** → `engine.py` (System Settings ▸ Privacy & Security ▸ Microphone).
- **Accessibility** → `autosend.py` (pynput global listener + synthetic keys) and the
  CGEvent paste in the engine. The *launching app* (Terminal/editor) needs the grant.

---

## 6. How to verify a change

**Contract tests:** `pytest` (config in `pyproject.toml`). They run headless — no mic,
no GUI, no model download — and lock the state-file contract + pure helpers against the
real modules. CI (`.github/workflows/ci.yml`) runs `ruff check .` + `pytest` on macOS for
every push/PR. Run both locally before you commit.

Tests cover the contract and pure logic, **not** the realtime audio/GUI paths — those are
still verified behaviorally. After any change, also exercise the path you touched:

- **UI / pill changes** → `python3 vibevoice.py --demo` and watch the waveform, the
  fade in/out, the typewriter text, the ✕/⧉ hit-targets. No mic needed.
- **Engine / VAD / transcription** → run `python3 engine.py`, speak a short phrase and a
  long monologue; confirm `~/.vibevoice/state` cycles `idle→recording→transcribing→idle`,
  `raw.txt` updates, and the long monologue is not truncated (invariant #4/#5).
- **Paste / autosend** → focus a terminal *and* an Electron editor; confirm the text
  lands in both (invariant #6) and Return behaves as configured (section 4).
- **Contract changes** → grep for every reader before editing a writer:
  `grep -rn "levels.bin\|raw.txt\|\.vibevoice/state" .`

Style: the repo is `ruff`-clean (a `.ruff_cache` is present). Run `ruff check .` if
available and keep it green. Match the existing comment density — the code favors short
"why" comments over "what" comments; follow that.

---

## 7. Map: where to look for what

| If you're touching… | Go to |
|---------------------|-------|
| VAD thresholds, silence/duration tuning, transcription | `engine.py` → `Engine`, module constants |
| The paste mechanism / Electron compatibility | `engine.py` → `_press_key_cg`, `autosend` |
| Pill geometry, notch detection, animation | `vibevoice.py` → `Controller._build_window`, `_animate_` |
| Waveform / text rendering | `vibevoice.py` → `PillView.drawRect_` |
| Menu-bar master switch, engine start/stop | `vibevoice.py` → `_engine_running`, `_start_engine`, `_stop_engine` |
| Auto-Return timing, target-app gating, one-shot logic | `autosend.py` → `AutoSendDaemon` |
| Deeper data-flow / threading model | `docs/ARCHITECTURE.md` |
