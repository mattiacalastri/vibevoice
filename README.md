<h1 align="center">VibeVoice</h1>

<p align="center">
  <strong>A Matrix-green Dynamic Island for your voice.</strong><br>
  Live speech-to-text in your Mac's notch — dictate, and your words land where the cursor is.
</p>

<p align="center">
  <img src="docs/hero.png" alt="VibeVoice — the live waveform pill in the notch, showing a transcription" width="640">
</p>

<p align="center">
  <a href="https://github.com/mattiacalastri/vibevoice/actions/workflows/ci.yml"><img src="https://github.com/mattiacalastri/vibevoice/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/platform-macOS%2012%2B-black?logo=apple" alt="macOS 12+">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/STT-whisper--turbo-9cf" alt="whisper-turbo">
  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen" alt="PRs welcome">
</p>

---

## Table of Contents

- [What it is](#what-it-is)
- [Built for vibe coding](#built-for-vibe-coding)
- [Features](#features)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [Architecture](#architecture)
- [Auto-start (LaunchAgent)](#auto-start-launchagent)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)
- [Credits](#credits)

---

## What it is

VibeVoice is a Dynamic Island that lives in your Mac's notch and transcribes your
voice in real time with `whisper-turbo`, then automatically pastes the text into
the frontmost app. Speak, watch the Matrix-green waveform react, and your words
land exactly where the cursor is — no clicks, no copy-paste dance.

It's purpose-built for **live coding with Claude Code**: keep your hands on the
keyboard, dictate the next instruction, and let VibeVoice drop it straight into
the terminal (with an optional auto-Return so the prompt fires the moment you
stop talking).

<p align="center">
  <img src="docs/sequence.svg" alt="VibeVoice end to end: speak, VAD onset, record, silence, whisper, paste, optional Return" width="760">
</p>

## Built for vibe coding

[Vibe coding](https://en.wikipedia.org/wiki/Vibe_coding) — the term Andrej
Karpathy coined in early 2025 — is the flow where you describe what you want in
plain language and let an AI agent (Claude Code, Cursor, Copilot) write the code.
The bottleneck stops being syntax and becomes **how fast you can express intent**.

Typing is the friction. VibeVoice removes it: keep your hands resting, *talk* to
your agent, and your instruction lands in the terminal the instant you stop — with
an optional auto-Return that fires the prompt for you. It's the missing input
device for vibe coding: **think out loud, ship.**

> Born for **Claude Code**, but it pastes into *any* frontmost app — editor, chat
> box, browser field. See [Two ways to fire the prompt](#two-ways-to-fire-the-prompt).

## Features

- **Matrix pixel waveform** — a live, retro-green RMS waveform rendered in the notch.
- **Immediate onset** — the pill reacts the instant you start speaking; silence makes it disappear.
- **Live text while you talk** — the words appear in the notch *as you speak them*,
  not after you stop. The open utterance is re-decoded every 0.6 s and a word is shown
  only once two successive passes agree on it (LocalAgreement-2), so the draft grows
  steadily instead of flickering — and never un-says something you have already read.
  `VIBEVOICE_STREAMING=0` restores the pure batch behaviour.
- **The text lands as you speak** — each confirmed chunk is typed straight into the
  frontmost app (direct unicode keystrokes, your clipboard is left alone), so you are
  not waiting on the silence at the end of the sentence. Measured on a 10.3 s sentence:
  first characters in the app after **1.3 s** instead of 11.8 s. Only the last word or
  two, the ones the stream never got to confirm, arrive with the final paste.
  `VIBEVOICE_STREAM_PASTE=0` goes back to one atomic paste at the end.
- **Universal autosend (CGEvent)** — pastes into *any* frontmost app via synthetic
  keyboard events, no app-specific integration required.
- **One-shot auto-Return daemon** — an optional `autosend.py` that presses Return
  for you after dictation settles. Armed with `Cmd+Shift+Space`, fires once, then
  disarms itself — with window-level locking so it never fires into the wrong window.
- **Inline copy (⧉)** — one tap copies the last transcription to the clipboard.
- **Robot command center** — a custom-drawn robot icon in the menu bar *and* a small
  floating robot widget you can drag anywhere (position is saved). Click either for the
  full menu; the eyes light green when listening, amber when the auto-send loop is armed.
  The floating widget exists because recent macOS can park a freshly-created status item
  off-screen behind the notch — the pill detects that and self-heals, but the widget
  guarantees an always-visible control.
- **🔇 Mute (pause, not kill)** — tap the mute icon (or the menu) to pause the
  mic without tearing down the engine, so resuming never re-prompts for the
  microphone permission.
- **🔒 Lock (pin visible)** — keep the pill on screen instead of auto-hiding on
  silence — handy while positioning it or watching levels.
- **🔁 Auto-send loop** — a one-tap toggle (pill icon or menu) that arms `autosend.py`
  for continuous mode and spawns it if it isn't already running: every dictation gets an
  automatic Return.
- **TTS-reactivity (optional)** — a self-contained hook: any external text-to-speech can
  write `~/.vibevoice/tts*` and the pill turns red, typing out the spoken sentence in
  sync with the audio — a mirror of the green dictation waveform. Off unless enabled.
- **Hides on silence** — the island stays out of your way until you speak again.

## Requirements

- **macOS 12+** (a Mac with a notch is recommended — that's where the island lives).
- **Python 3.10+**
- Python packages: **PyObjC**, **mlx-whisper**, **sounddevice**, **numpy**
  (plus **pynput** if you use the optional `autosend.py` daemon).
- System permissions:
  - **Microphone** access (System Settings → Privacy & Security → Microphone).
  - **Accessibility** access for synthetic keystrokes / autosend
    (System Settings → Privacy & Security → Accessibility).

## Install

### Option A — build the app (recommended, all-in-one)

Assemble a double-clickable `VibeVoice.app` and launch it. The bundle carries its
own identity, so the **Microphone** and **Accessibility** prompts attach to
*VibeVoice* (not to your Terminal), and one double-click brings up the whole
capture → transcribe → paste stack.

```bash
git clone https://github.com/mattiacalastri/vibevoice.git
cd vibevoice
pip3 install -r requirements.txt   # interpreter deps (the .app reuses your python3)
./build_app.sh                     # -> dist/VibeVoice.app
open dist/VibeVoice.app            # first launch prompts for Microphone + Accessibility
```

> The bundle is **lightweight**: it ships the source + a launcher + an
> `Info.plist`, and runs the first `python3` it finds that has the deps. It does
> not embed Python, so it's small and reliable but **not signed/notarized** —
> perfect for your own Mac; for distribution to others, sign + notarize it (or
> wrap it with py2app/PyInstaller).

### Option B — run the scripts directly (dev)

```bash
git clone https://github.com/mattiacalastri/vibevoice.git
cd vibevoice
pip install -r requirements.txt

# Start the engine (mic capture + STT, writes state files)
python3 engine.py &

# Start the pill (Dynamic Island UI, reads state files)
python3 vibevoice.py &
```

> **First run:** macOS will prompt for **Microphone** and **Accessibility**
> permissions. Grant both to the app you launch from (Terminal, iTerm2, etc.) —
> see [Troubleshooting](#troubleshooting) if the pill stays invisible or text
> doesn't paste.

## Usage

1. **Speak** — start talking; the island appears in the notch.
2. **Transcribe** — the Matrix waveform reacts live while `whisper-turbo` works.
3. **Autosend** — when you stop, the text is pasted into the frontmost app
   (optionally followed by Return — see [Configuration](#configuration)).
4. **⧉ Copy** — tap the inline copy glyph to put the last transcription on the clipboard.
5. **✕ / robot** — dismiss the pill with ✕, or click the **robot** (menu bar or the
   floating widget) to toggle dictation on/off and reach Mute / Lock / Auto-send.

### Preview the design (no mic, no engine)

Two flags let you see and position the pill on its own — handy for screenshots or
to nudge it under your notch:

```bash
python3 vibevoice.py --demo     # animated demo: canned text + synthetic waveform (ignores the mic)
python3 vibevoice.py --place    # placement mode: the pill stays visible so you can position it
```

<p align="center">
  <img src="docs/demo_pill.png" alt="VibeVoice pill in --demo mode: Matrix-green pixel waveform, transcribed line, inline copy glyph" width="540">
</p>
<p align="center"><sub>The pill in <code>--demo</code> mode — a fixed sample line, not live dictation.</sub></p>

## Architecture

<p align="center">
  <img src="docs/architecture.svg" alt="VibeVoice data flow: mic into engine.py; engine writes state files under ~/.vibevoice; the pill reads them and draws under the notch; engine pastes into the frontmost app; an optional autosend daemon presses Return" width="780">
</p>

<details>
<summary><sub>Same diagram in plain text</sub></summary>

```
   🎤 mic
    │
    ▼
┌─────────────┐   writes    ┌──────────────────┐   reads    ┌──────────────┐
│  engine.py  │ ──────────▶ │  ~/.vibevoice/   │ ◀───────── │ vibevoice.py │
│  capture +  │   state     │  state · levels  │   state    │  the pill /  │
│  whisper    │   files     │  raw.txt         │   files    │  Dynamic Is. │
└─────────────┘             └──────────────────┘            └──────────────┘
       │                                                            │
       │ paste text into frontmost app                              │ draws
       ▼                                                            ▼
┌──────────────────────────────────┐                         📺 the notch
│  autosend.py  (optional daemon)  │  presses Return after typing settles
│  Cmd+Shift+Space · one-shot      │  — armed, fires once, disarms itself
└──────────────────────────────────┘
```

</details>

VibeVoice is split into **decoupled processes** that communicate only through a
small set of files under `~/.vibevoice/`:

- **`engine.py`** — captures the microphone, runs STT, and **writes** the state files.
- **`vibevoice.py`** — the pill / Dynamic Island UI. It **reads** the state files
  and draws the waveform, transcription, and controls.
- **`autosend.py`** *(optional)* — a standalone daemon that presses Return after
  you stop typing/dictating. It shares nothing with the engine except the
  optional pause flag, so you can run it with any STT (or not at all).

Because the only contract between them is the state directory, you can
**bring your own engine**: swap `engine.py` for anything that respects the
contract below, and the pill keeps working unchanged.

> **Going deeper / editing the code?** See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**
> for the threading model, VAD state machine, render loop, and tunable constants.
> Working with an AI agent? Point it at **[AGENTS.md](AGENTS.md)** — it lists the
> invariants that must not be broken.

### Engine states

The engine is a small state machine driven by an energy-based voice activity
detector (VAD). It writes the current state to `~/.vibevoice/state` on every
transition; the pill reads it to decide when to appear, animate, and fade.

<p align="center">
  <img src="docs/state-machine.svg" alt="Engine state machine: idle to recording on voice onset (RMS >= 0.015), recording to transcribing after 1.5s silence or 15s max, then back to idle once the text is pasted" width="780">
</p>

### Two ways to fire the prompt

Pressing Return after the text lands has two independent paths — use whichever
fits:

1. **Engine-driven (simple).** Set `VIBEVOICE_AUTOSEND_RETURN=1` and the engine
   presses Return right after it pastes. Zero extra processes.
2. **Daemon-driven (`autosend.py`, robust).** A global keystroke listener that
   fires Return only after typing goes quiet, **armed one-shot** via
   `Cmd+Shift+Space`, with window-signature locking so a delayed Return can't
   land in a window you switched to. Best when you dictate into a terminal/editor
   and want a hard guarantee the Return won't misfire.

### State-file contract (shared pill ↔ engine)

The engine **writes** these files; the pill **reads** them. Honor this contract
exactly.

- **State directory:** `~/.vibevoice/` — expand `$HOME`, create it if missing.
- **`~/.vibevoice/state`** — a text file containing exactly **one** of:
  `idle` | `recording` | `transcribing`
- **`~/.vibevoice/levels.bin`** — **60 `float32`** values, **little-endian**
  (RMS levels in the `0..1` range). Must be written **atomically**
  (write to a temp file, then `os.replace`).
- **`~/.vibevoice/raw.txt`** — the last transcription as **plain text**
  (just the sentence — no logs, no timestamps).
- **`~/.vibevoice/autosend`** *(written by `autosend.py` only)* — `on` | `off`,
  the armed state of the auto-Return daemon. Independent of the pill/engine.

### Auto-send daemon (`autosend.py`)

Optional, standalone. It listens to global keystrokes (`pynput`) and, while a
target app is frontmost, presses Return once typing has been quiet for
`--delay` seconds (default `0.8`).

- **Arm / disarm:** `Cmd+Shift+Space` — *tink* = armed, *submarine* = disarmed,
  plus a desktop notification when armed.
- **One-shot:** after the first Return fires it disarms itself, so it never
  presses Return while you type by hand afterwards.
- **Window lock:** it snapshots the frontmost window and skips the send if you
  switched windows during the silence window.
- **External pause:** write a unix timestamp into `/tmp/vibevoice_autosend_pause`
  to suspend it (auto-clears after 60s); delete the file to resume.

```bash
pip install pynput
python3 autosend.py            # then arm with Cmd+Shift+Space and dictate
python3 autosend.py --delay 3  # wait 3s of silence before pressing Return
```

Needs **Accessibility** permission for the app that launches it (it reads global
keys and simulates Return).

## Auto-start (LaunchAgent)

A LaunchAgent template is included as **[`com.vibevoice.pill.plist`](com.vibevoice.pill.plist)**
(`RunAtLoad` + `KeepAlive`). It runs `python3 ~/projects/vibevoice/vibevoice.py`
on login and restarts it if it **crashes**.

Crash, not quit: `KeepAlive` is `{SuccessfulExit: false}`, not plain `true`.
With `true`, launchd relaunches the pill whatever the exit code — which makes
the menu's own **Quit (close everything)** a lie, since a clean quit exits 0 and
comes straight back (with the engine behind it, when
`VIBEVOICE_ENGINE_AUTOSTART=1`). The only escape is then `launchctl bootout`,
which nobody should need to know. If you already installed a copy with
`KeepAlive: true`, replace it and reload:

```bash
launchctl bootout gui/$UID/com.vibevoice.pill    # ignore "No such process"
cp com.vibevoice.pill.plist ~/Library/LaunchAgents/   # re-edit the placeholders
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.vibevoice.pill.plist
```

**All-in-one:** the template sets `VIBEVOICE_ENGINE_AUTOSTART=1`, so the pill
spawns `engine.py` itself (in its own GUI/TCC context, where the mic permission
resolves) — loading this **single** agent brings up the whole capture →
transcribe → paste stack. You don't need a separate engine LaunchAgent. Set that
key to `0` (or remove it) if you'd rather flip the engine on with the 🎙 menu-bar
toggle.

The template uses two placeholders, `__HOME__` and `__PYTHON__` — **replace both
before installing**.

`__PYTHON__` must be the interpreter you installed the requirements into, and it
is not `/usr/bin/python3`: the system python (3.9 on macOS 12–15) has neither
`pynput` nor `AppKit`, so the daemon dies on import and `KeepAlive` relaunches
the corpse every 10 seconds, forever, with `~/.vibevoice/autosend.err` as the
only trace. The template used to hardcode it, which meant anyone following these
instructions never had a working auto-Return daemon.

```bash
cp com.vibevoice.pill.plist ~/Library/LaunchAgents/
# edit the copy: __HOME__ → your home path, __PYTHON__ → `which python3`
launchctl load ~/Library/LaunchAgents/com.vibevoice.pill.plist
```

To also auto-start the one-shot auto-Return daemon, install
**[`com.vibevoice.autosend.plist`](com.vibevoice.autosend.plist)** the same way:

```bash
cp com.vibevoice.autosend.plist ~/Library/LaunchAgents/
# edit the copy: replace every __HOME__ with your home path
launchctl load ~/Library/LaunchAgents/com.vibevoice.autosend.plist
```

## Configuration

Behavior is controlled by environment variables read by `engine.py`:

| Variable                     | Default                          | Description                                                        |
| ---------------------------- | -------------------------------- | ------------------------------------------------------------------ |
| `VIBEVOICE_LANG`             | `it`                             | Whisper transcription language code (e.g. `en`, `it`, `de`, `fr`). |
| `VIBEVOICE_MODEL`            | `mlx-community/whisper-turbo`    | The `mlx_whisper` model to load.                                   |
| `VIBEVOICE_AUTOSEND`         | `1`                              | `1` to auto-paste into the frontmost app, `0` to copy only.        |
| `VIBEVOICE_AUTOSEND_RETURN`  | `0`                              | `1` to press Return right after pasting (fires the prompt), `0` to skip. |
| `VIBEVOICE_ENGINE_AUTOSTART` | `0`                              | Read by the **pill**, not the engine. `1` makes `vibevoice.py` spawn `engine.py` on launch (all-in-one — one LaunchAgent runs the whole stack); `0` waits for the 🎙 menu-bar toggle. The bundled `com.vibevoice.pill.plist` sets this to `1`. |

```bash
# Example: English, copy-to-clipboard only, no auto-Return
VIBEVOICE_LANG=en VIBEVOICE_AUTOSEND=0 python3 engine.py
```

## Troubleshooting

| Symptom | Fix |
| ------- | --- |
| **Pill never appears** | The engine isn't running or lacks **Microphone** access. Check `python3 engine.py` output and System Settings → Privacy & Security → Microphone. |
| **Transcription works but text doesn't paste** | Missing **Accessibility** permission for the launching app. Add Terminal/iTerm2 under Privacy & Security → Accessibility, then restart it. |
| **Auto-Return fires into the wrong window** | Use the `autosend.py` daemon instead of `VIBEVOICE_AUTOSEND_RETURN` — it locks onto the window you dictated into. |
| **`autosend.py` does nothing when I dictate** | It's one-shot: arm it first with **`Cmd+Shift+Space`** (you'll hear *tink* + a notification). It disarms after one send. |
| **Wrong language transcribed** | Set `VIBEVOICE_LANG` (default is `it`). |
| **`ModuleNotFoundError: pynput`** | `pip install pynput` — only needed for the optional `autosend.py` daemon. |

## FAQ

**Do I need a Mac with a notch?**
No — the pill renders under the notch area, but it works on any macOS 12+ display. A notch just makes it feel native.

**Does it send my audio anywhere?**
No. Transcription runs **locally** via `mlx-whisper` on Apple Silicon. Nothing leaves your machine.

**Can I use a different STT engine?**
Yes. The pill only reads the [state-file contract](#state-file-contract-shared-pill--engine). Swap `engine.py` for anything that writes those files.

**Is it only for Claude Code?**
No. It pastes into *any* frontmost app — terminal, editor, chat box, browser field. Claude Code is just the workflow it was born for.

## Roadmap

This is **v0.x** — it works end to end (capture → transcribe → paste → send). Planned:

- [x] Packaged `.app` bundle (`./build_app.sh` — double-click, own mic identity, no LaunchAgent editing)
- [ ] Self-contained signed/notarized bundle (embed Python via py2app/PyInstaller)
- [ ] Configurable theme (beyond Matrix green)
- [ ] In-pill language switcher
- [ ] Demo GIF + short screencast
- [ ] Optional streaming partial transcripts

Ideas and PRs welcome — see [Contributing](#contributing).

## Contributing

Contributions are welcome. Open an issue to discuss a change, or send a PR:

```bash
git clone https://github.com/mattiacalastri/vibevoice.git
cd vibevoice
pip install -r requirements.txt
# make your change, test engine.py + vibevoice.py + autosend.py, then open a PR
```

Keep the **state-file contract** stable — it's the seam that lets people bring
their own engine. If you change it, document it in the same PR.

Before you start, read **[AGENTS.md](AGENTS.md)** (engineering contract +
invariants) and **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** (how the runtime
fits together). These also brief any AI coding agent you point at the repo.

## License

[MIT](LICENSE) — Copyright (c) 2026 Mattia Calastri.

## Credits

Built with Claude Code (Opus) + Mattia Calastri.
