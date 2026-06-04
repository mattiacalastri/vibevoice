# Architecture

Deep reference for VibeVoice's runtime. For the editing contract and invariants, read
`AGENTS.md` at the repo root first; this document explains *how* the pieces work so you
can change them safely.

---

## 1. Process topology

Three processes, zero shared imports, file-only IPC:

```
                       ┌──────────────────────────────────────────────┐
                       │                ~/.vibevoice/                  │
                       │   state        levels.bin        raw.txt      │
                       └──────────────────────────────────────────────┘
                              ▲   ▲          ▲   ▲           ▲   ▲
                  writes      │   │          │   │           │   │   reads
                              │   └──────────┼───┴───────────┼───┘
           ┌──────────────────┴──┐          │               │   ┌──────────────────────┐
           │      engine.py      │          └───────────────┴──►│     vibevoice.py     │
           │  (capture + ASR +   │                              │   (the pill UI)      │
           │   paste daemon)     │◄─── pgrep/pkill -f engine.py ─┤  menu-bar switch     │
           └─────────┬───────────┘                              └──────────────────────┘
                     │ pbcopy + Cmd+V (CGEvent @ HID tap)
                     ▼
            frontmost application

           ┌─────────────────────┐      reads/writes
           │     autosend.py     │◄───── ~/.vibevoice/autosend  ("on"|"off")
           │  (pynput one-shot   │◄───── /tmp/vibevoice_autosend_pause (optional)
           │   auto-Return)      │─────► synthetic Return into target app
           └─────────────────────┘
```

Why file-IPC instead of sockets/imports: it gives **crash isolation** (the GUI can die
and respawn without losing the engine, and vice-versa), makes each process independently
runnable and testable, and lets `autosend.py` work with *any* STT, not just this engine.

---

## 2. The engine pipeline (`engine.py`)

### 2.1 Threads

| Thread | Created by | Job |
|--------|-----------|-----|
| main | process start | opens `sd.InputStream`, then blocks on `self._stop.wait(0.25)` |
| audio callback | `sounddevice` | one call per ~100 ms block; runs the VAD; **must never raise** |
| transcription worker(s) | `_finalize` | up to **2** at once (`Semaphore(2)`); runs Whisper, writes `raw.txt`, spawns paste |
| paste | `_transcribe_worker` | `autosend()` off-thread so state returns to `idle` promptly |

Locking: a single `threading.Lock` guards the VAD/capture state (`_speaking`, `_buf`,
`_pre`, timers, `_rms_history`). The rule the code follows religiously: **decide inside
the lock, do I/O and spawn threads outside it.** The callback computes a `do_finalize`
flag under the lock, releases, then performs `write_state` / `_finalize` outside. Keep
this discipline — file writes inside the audio lock would risk realtime glitches.

### 2.2 VAD state machine

Energy-based voice-activity detection on per-block RMS:

```
            rms ≥ VAD_THRESHOLD                 rms < VAD_THRESHOLD
                  (speech)                            (silence)
   ┌────────┐  onset: start utterance,   ┌───────────┐  start silence timer
   │  idle  │ ───────────────────────►   │ recording │ ─────────────────────┐
   │        │  include PRE_ROLL_BLOCKS    │           │                      │
   └────────┘                            └───────────┘                       │
        ▲                                   │      ▲                          │
        │                                   │      │ speech resumes           │
        │       silence ≥ SILENCE_SEC       │      └── (timer reset)          │
        │       OR elapsed ≥ MAX_DUR        ▼                                 │
        │                              ┌──────────────┐                       │
        └──────────────────────────────│ finalize +   │◄──────────────────────┘
            (worker done → idle)        │ transcribe   │
                                        └──────────────┘
```

- **Pre-roll**: `PRE_ROLL_BLOCKS` (5 ≈ 0.5 s) of pre-speech audio is retained in a
  `deque` and prepended on onset, so the first phoneme isn't clipped.
- **Finalize triggers**: trailing silence ≥ `SILENCE_SEC` (1.5 s) **or** utterance
  length ≥ `MAX_DUR` (15 s, mid-speech force-flush — see invariant #5 in `AGENTS.md`).
- **Drop rule**: utterances shorter than `MIN_DUR` (0.4 s) are discarded silently.
- **Backpressure**: if both transcription slots are busy at finalize, that utterance is
  dropped and state returns to `idle` rather than queuing unbounded work.

### 2.3 Transcription & paste

`transcribe()` writes the float32 buffer to a temp 16 kHz/16-bit WAV and calls
`mlx_whisper.transcribe(path_or_hf_repo=MODEL, language=LANG)`. `mlx_whisper` is imported
**lazily** (`_ensure_mlx_whisper`) so the process starts and the UI is usable even before
the model is present; the user gets actionable install/download guidance instead of a
crash.

Paste (`autosend()` in the engine): `pbcopy` to the clipboard, then `Cmd+V` via
`CGEventCreateKeyboardEvent` posted at `kCGHIDEventTap`. The HID tap is below the app
sandbox, which is what makes paste land in Electron editors. If Quartz/PyObjC is missing,
it falls back to `osascript ... keystroke "v"`. Return (key 36) is pressed only when
`VIBEVOICE_AUTOSEND_RETURN=1`, after `RETURN_DELAY`.

---

## 3. The pill (`vibevoice.py`)

A borderless, non-activating `NSPanel` pinned flush under the notch. It is a pure
**consumer** of the state files plus a process supervisor for the engine.

### 3.1 Render loop
A 24 fps `NSTimer` (`tick:`) drives everything:
1. read `levels.bin` → 60 floats, downsample to `N_BARS` (32), apply `GAIN` and a
   perceptual curve `v**0.6` so quiet speech is still visible;
2. read `state` and `raw.txt`;
3. decide visibility — appears immediately when raw RMS clears `VOICE_THRESH` or state is
   `recording`/`transcribing`; hides after `IDLE_HIDE_S` (1.5 s) of silence, or 2.5 s
   while transcribed text is shown (so the user can click ⧉ to copy);
4. on a visibility *change* only, trigger the native AppKit expand/collapse animation
   (the "Dynamic Island" grow-from-notch / shrink-into-notch).

### 3.2 Geometry
`_build_window` locates the screen **with the notch** via `safeAreaInsets().top > 0`
(not `mainScreen`), computes two frames — `exp` (full pill, flush top edge) and `col`
(exact notch footprint from `auxiliaryTopLeft/RightArea`) — and animates between them.

### 3.3 Hit-targets
`drawRect_` paints the waveform LEDs, the blinking caret, the transcript (Menlo 11,
Matrix green), an inline ⧉ copy glyph at the end of the text, and a ✕ stop glyph
top-right. `mouseDown_` maps clicks: top-right rect → `_stop_engine()`; the recorded
`copy_rect` → re-copy the sentence to the pasteboard. Hover state is polled via
`mouseLocationOutsideOfEventStream()` each tick (no event stream needed for a
non-activating panel).

### 3.4 Master switch
The menu-bar item (`🎙`/`🔇`) toggles the engine by shelling out:
`pgrep -f engine.py` (running?), `Popen([python, engine.py], start_new_session=True)`
(start), `pkill -f engine.py` (stop). This is why the engine's **filename** is part of
the contract (invariant #8).

---

## 4. The autosend daemon (`autosend.py`)

A `pynput` global keyboard listener, independent of the engine. Behavior:
- **Arming**: `Cmd+Shift+Space` toggles the `~/.vibevoice/autosend` flag (`tink` = armed,
  `submarine` = disarmed) and posts a desktop notification when armed.
- **Silence timer**: any non-modifier key in a **target app** (re)starts a
  `threading.Timer(delay)`; manual Return or Esc cancels it.
- **Window locking**: when the timer is scheduled it snapshots a *window signature*
  (`get_frontmost_signature` — window **id** for Terminal/iTerm, position for
  Electron/Ghostty/Warp, title prefix otherwise). At fire time it re-checks the signature
  and the app name and **skips** if either changed — so a Return never fires into a window
  you've since switched away from.
- **One-shot**: after firing, it calls `set_enabled(False)`. Re-arm per dictated message.
- **Pause hook**: an external tool can write a timestamp to
  `/tmp/vibevoice_autosend_pause` to suspend firing for up to `PAUSE_TTL_SECONDS` (60 s),
  with TTL auto-clear to prevent a stuck deadlock.

---

## 5. Tunable constants (and why they're set where they are)

| Constant | File | Value | Rationale |
|----------|------|-------|-----------|
| `SAMPLE_RATE` | engine | 16000 | mlx_whisper expects 16 kHz mono |
| `BLOCKSIZE` | engine | 1600 | ~100 ms/block → VAD reacts within a tenth of a second |
| `VAD_THRESHOLD` | engine | 0.015 | RMS gate between noise floor and speech |
| `SILENCE_SEC` | engine | 1.5 | trailing silence that ends an utterance |
| `MIN_DUR` | engine | 0.4 | below this = noise, dropped |
| `MAX_DUR` | engine | 15.0 | force-flush to keep blobs in the recognizer's comfort window + steady cadence on long dictation |
| `PRE_ROLL_BLOCKS` | engine | 5 | ~0.5 s pre-speech so the first word isn't clipped |
| `Semaphore` | engine | 2 | concurrent transcriptions → don't drop a long utterance's tail |
| `RETURN_DELAY` | engine | 1.5 | gap between paste and the auto-Return |
| `LEVELS_LEN` / `LEVELS_HZ` | engine | 60 / 10 | waveform ring buffer size / write rate |
| `N_BARS` | pill | 32 | waveform columns (downsampled from the 60 stored levels) |
| `GAIN` / `VOICE_THRESH` | pill | 12.0 / 0.018 | waveform sensitivity / immediate-onset threshold |
| `IDLE_HIDE_S` / `TICK` | pill | 1.5 / 1/24 | fade-out delay / 24 fps render |
| `AUTO_SEND_DELAY` | autosend | 0.8 | typing-silence before auto-Return |
| `PAUSE_TTL_SECONDS` | autosend | 60 | max suspension from the pause flag (anti-deadlock) |

---

## 6. Failure modes & where they surface

| Symptom | Likely cause | Where to look |
|---------|-------------|---------------|
| Pill never appears | engine not running, or no `levels.bin` | menu-bar switch; `~/.vibevoice/state` |
| Pill flickers / blank waveform | torn read of `levels.bin` | pill `_read_levels` guard; engine atomic write |
| "Transcription disabled" on stderr | `mlx_whisper` not installed / model not downloaded | `_ensure_mlx_whisper` |
| Text not pasted | missing Accessibility grant, or sandbox blocking the keystroke | `_press_key_cg` (HID tap) + osascript fallback |
| Return fires twice / unexpectedly | both engine `AUTOSEND_RETURN` and `autosend.py` active | `AGENTS.md` §4 |
| Long monologue truncated | `Semaphore` reduced to 1, or `MAX_DUR` too low | engine `_finalize`, invariant #4/#5 |
| Mic permission error at start | Microphone privacy grant | `Engine.run` error message |
```
