# VibeVoice

> A Matrix-green Dynamic Island for your voice — live STT + autosend for macOS, built for Claude Code live-coding.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## Table of Contents

- [What it is](#what-it-is)
- [Demo](#demo)
- [Features](#features)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [Architecture](#architecture)
- [Auto-start (LaunchAgent)](#auto-start-launchagent)
- [Configuration](#configuration)
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

## Demo

![demo](docs/demo.gif)

## Features

- **Matrix pixel waveform** — a live, retro-green RMS waveform rendered in the notch.
- **Immediate onset** — the pill reacts the instant you start speaking; silence makes it disappear.
- **Universal autosend (CGEvent)** — pastes into *any* frontmost app via synthetic
  keyboard events, no app-specific integration required.
- **Inline copy (⧉)** — one tap copies the last transcription to the clipboard.
- **Menu bar toggle** — a 🎙 menu bar item flips dictation on/off at any time.
- **Hides on silence** — the island stays out of your way until you speak again.

## Requirements

- **macOS 12+** (a Mac with a notch is recommended — that's where the island lives).
- **Python 3.10+**
- Python packages: **PyObjC**, **mlx-whisper**, **sounddevice**, **numpy**.
- System permissions:
  - **Microphone** access (System Settings → Privacy & Security → Microphone).
  - **Accessibility** access for synthetic keystrokes / autosend
    (System Settings → Privacy & Security → Accessibility).

## Install

```bash
git clone https://github.com/your-username/vibevoice.git
cd vibevoice
pip install -r requirements.txt

# Start the engine (mic capture + STT, writes state files)
python3 engine.py &

# Start the pill (Dynamic Island UI, reads state files)
python3 vibevoice.py &
```

## Usage

1. **Speak** — start talking; the island appears in the notch.
2. **Transcribe** — the Matrix waveform reacts live while `whisper-turbo` works.
3. **Autosend** — when you stop, the text is pasted into the frontmost app
   (optionally followed by Return — see [Configuration](#configuration)).
4. **⧉ Copy** — tap the inline copy glyph to put the last transcription on the clipboard.
5. **✕ / menu bar** — dismiss the pill with ✕, or use the **🎙** menu bar item to
   toggle dictation on/off.

## Architecture

VibeVoice is split into **two fully decoupled processes** that communicate only
through a small set of files under `~/.vibevoice/`:

- **`engine.py`** — captures the microphone, runs STT, and **writes** the state files.
- **`vibevoice.py`** — the pill / Dynamic Island UI. It **reads** the state files
  and draws the waveform, transcription, and controls.

Because the only contract between them is the state directory, you can
**bring your own engine**: swap `engine.py` for anything that respects the
contract below, and the pill keeps working unchanged.

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

## Auto-start (LaunchAgent)

A LaunchAgent template is included as **[`com.vibevoice.pill.plist`](com.vibevoice.pill.plist)**
(`RunAtLoad` + `KeepAlive`). It runs `python3 ~/projects/vibevoice/vibevoice.py`
on login and keeps it alive.

The template uses a `__HOME__` placeholder — **replace it with your absolute home
directory path** (e.g. `/Users/yourname`) before installing:

```bash
cp com.vibevoice.pill.plist ~/Library/LaunchAgents/
# edit the copy: replace every __HOME__ with your home path
launchctl load ~/Library/LaunchAgents/com.vibevoice.pill.plist
```

## Configuration

Behavior is controlled by environment variables:

| Variable                     | Default | Description                                                        |
| ---------------------------- | ------- | ------------------------------------------------------------------ |
| `VIBEVOICE_LANG`             | `en`    | Whisper transcription language code (e.g. `en`, `it`).             |
| `VIBEVOICE_AUTOSEND`         | `1`     | `1` to auto-paste into the frontmost app, `0` to copy only.        |
| `VIBEVOICE_AUTOSEND_RETURN`  | `1`     | `1` to press Return after pasting (fires the prompt), `0` to skip. |

## License

[MIT](LICENSE) — Copyright (c) 2026 Mattia Calastri.

## Credits

Built with Claude Code (Opus) + Mattia Calastri.
