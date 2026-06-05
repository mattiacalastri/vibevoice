# CLAUDE.md — agent rules for VibeVoice

Operating rules for AI coding agents (Claude Code, Cursor, Copilot) working in
this repo. This file is intentionally short: the full engineering contract —
architecture, the state-file spine, and the **hard invariants you must not
break** — lives in [`AGENTS.md`](AGENTS.md). **Read `AGENTS.md` before editing.**
For runtime internals see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## What this is, in one line

A macOS speech-to-text utility: three decoupled processes (`engine.py`,
`vibevoice.py`, `autosend.py`) that never import each other and communicate
**only** through small files under `~/.vibevoice/`.

## Non-negotiable rules

1. **Green before commit.** Run `ruff check .` and `pytest` and keep both green.
   CI (`.github/workflows/ci.yml`) runs them on macOS for every push/PR.
2. **Never touch the live runtime in tests.** Tests must redirect state into a
   `tmp_path` via the `monkeypatch` fixtures (see `tests/test_contract.py`).
   Never read or write the real `~/.vibevoice/` from a test.
3. **Preserve the decoupling.** The three processes share no Python imports.
   Do not "simplify" by importing `engine` into `vibevoice` (or vice-versa).
   Coupling is via the `~/.vibevoice/` files only — that is the crash isolation.
4. **The state-file contract is load-bearing.** If you change a file's format,
   change the **writer and every reader in the same commit**. The `60` in
   `levels.bin` is duplicated in `engine.py` (`LEVELS_LEN`) and the pill's
   `struct.unpack("<60f", ...)` — keep them in lockstep. Grep first:
   `grep -rn "levels.bin\|raw.txt\|\.vibevoice/state" .`
5. **Don't rename `engine.py`** without updating all three `pgrep/pkill -f
   engine.py` call sites in `vibevoice.py` and keeping it a sibling of the pill
   (invariants #8 / #10 in `AGENTS.md`).
6. **The audio callback must never raise.** `Engine._audio_callback` runs on the
   realtime audio thread and swallows exceptions on purpose. Do not add throwing
   or blocking work inside its lock; defer I/O and thread spawns to outside it.
7. **macOS-only.** AppKit, CoreAudio, and `mlx_whisper` (Apple Silicon). Don't
   add cross-platform shims; gate optional deps with lazy imports as the code
   already does.

## Before you commit — definition of done

- [ ] `ruff check .` clean
- [ ] `pytest` green
- [ ] If you touched the state-file contract: writer + all readers in one commit
- [ ] You exercised the path you changed (see `AGENTS.md` §6 "How to verify")
- [ ] Conventional commit message (`feat:`, `fix:`, `docs:`, `test:`, `chore:`)

## Where to look

`AGENTS.md` §7 has the full "where to look for what" map. Quick version:
`engine.py` = capture/VAD/transcribe/paste · `vibevoice.py` = pill UI + menu-bar
master switch · `autosend.py` = standalone auto-Return daemon.
