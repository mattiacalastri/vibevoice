# VibeVoice — Build app macOS distribuibile stile Wispr Flow (design)

Data: 2026-07-07 · sess.9180 · Approvato da Mattia in brainstorming

## Obiettivo

Portare VibeVoice (questo repo) da "bundle lightweight" a **prodotto macOS
completo**: icona propria, presenza nel Dock, finestra impostazioni, Python
embedded (nessuna dipendenza da homebrew sul Mac di destinazione), firmato,
notarizzato, consegnato via DMG — l'esperienza di Wispr Flow.

## Decisioni prese (brainstorming)

| Domanda | Scelta |
|---|---|
| Profondità build | Package pro del motore esistente (no riscrittura) |
| Base di partenza | **Questo repo** (lignaggio pulito: `engine.py` · `vibevoice.py` · `autosend.py`, contratto `~/.vibevoice/`) |
| Motore STT | whisper-turbo del repo; il daily-driver di Mattia (`~/scripts/stt_bar.py`, Apple SR it-IT) resta INTATTO |
| Distribuzione | Distribuibile ad altri (prodotto) |
| Presenza UI | Dock icon + finestra impostazioni (oltre a pillola + menu bar) |
| Icona | Waveform su squircle teal, famiglia visiva Astra/AI Accelerator |
| Nomenclatura | Già armonizzata nel repo — si preserva; nessun nuovo alias |

## Stato di partenza (ground truth)

- Repo pubblico (GitHub `mattiacalastri/vibevoice`, MIT, CI ruff+pytest verde).
- Tre processi disaccoppiati che comunicano SOLO via file sotto `~/.vibevoice/`
  (invarianti hard in `AGENTS.md` — vincolanti per tutto questo progetto):
  `engine.py` (capture/VAD/whisper/paste) · `vibevoice.py` (pillola + menu bar
  master switch) · `autosend.py` (auto-Return).
- `build_app.sh` esistente produce un bundle *lightweight*: sorgenti + launcher
  bash + Info.plist con identità TCC propria. Dichiara esso stesso il prossimo
  passo: "for a fully self-contained, signed, notarized app use
  py2app/PyInstaller on top of this". Manca inoltre: icona, Dock, settings.

## Lavori (4 pacchetti)

### WP1 — Icona

- Concetto: waveform su squircle teal (gradiente teal/verde scuro), coerente
  con AI Accelerator nel Dock.
- Processo: 3-4 candidati reali → anteprima a Mattia (Safari/Preview) → scelta
  → `VibeVoice.icns` completo via `iconutil` → `assets/icon/` nel repo +
  `CFBundleIconFile` in build.
- Vincolo (cicatrice sess.9161): squircle disegnato DENTRO il canvas con
  margine trasparente — mai edge-to-edge (macOS lo avvolge col bordo grigio).
  Post-install: `lsregister` refresh.

### WP2 — Dock + finestra impostazioni

- `vibevoice.py` passa da ActivationPolicy *Accessory* a *Regular* quando
  l'utente lo vuole: **default Dock ON** (scelta di Mattia), toggle nelle
  impostazioni per tornare menu-bar-only stile Wispr Flow.
- Finestra impostazioni nativa AppKit nello STESSO processo pillola (nessun
  nuovo processo, niente import cross-processo — invariante #3):
  on/off motore · lingua (en/it, mappa `VIBEVOICE_LANG`) · toggle autosend +
  auto-Return · history ultime trascrizioni (legge i file di stato) · toggle
  Dock. Persistenza: `~/.vibevoice/config.json` (nuovo file di stato: writer
  + reader nello stesso commit, test di contratto aggiornati — invariante #4).
- Aperta da: click icona Dock, voce menu bar.

### WP3 — Python embedded

- py2app prima scelta (nativo PyObjC); PyInstaller fallback documentato nello
  script. Output: .app self-contained (pyobjc, sounddevice+libportaudio,
  numpy, mlx_whisper).
- Modello whisper-turbo NON imbarcato nel DMG (troppo pesante): primo avvio lo
  scarica con progress nella pillola; documentato nel README.
- `packaging/` nel repo: `setup_py2app.py`, `entitlements.plist`,
  `build_app.sh` evoluto (mantiene la modalità lightweight con un flag).
- Solo Apple Silicon (mlx): dichiarato in Info.plist e README.

### WP4 — Firma, notarizzazione, DMG

1. Firma Developer ID Application (cert già nel portachiavi, sess.9157) +
   hardened runtime + entitlements (mic, Apple Events).
2. Notarizzazione `notarytool` + `stapler`.
   ⚠️ **Unico gesto umano**: app-specific password notarytool (stesso blocco
   di AI Accelerator). La build NON aspetta: firmata gira subito sui Mac di
   Mattia; la notarizzazione si aggancia quando la password arriva.
3. DMG brand con drag-to-Applications (`create-dmg`), `make_dmg.sh` in
   `packaging/`.

## Compatibilità

- Contratto di stato del prodotto: `~/.vibevoice/` (quello del repo). Il
  daily-driver di Mattia (`~/scripts/stt_bar.py`, run-dir
  `~/.local/run/jarvis/`, LaunchAgent `com.vibevoice.dictation`) NON si tocca:
  lignaggio parallelo finché Mattia non decide di migrare.
- ⚠️ Un solo sistema di dettatura attivo alla volta (due mic-capture + due
  autosend = conflitto): il collaudo dell'app si fa col daily-driver in pausa
  (kill-switch `stt_disabled`), e si riaccende dopo.
- Invarianti `AGENTS.md` restano legge: decoupling a 3 processi, contratto
  file, callback audio che non solleva, ruff+pytest verdi a ogni commit.

## Error handling

- py2app che inciampa su una dipendenza → fallback PyInstaller; se entrambi
  falliscono su sounddevice → imbarcare `libportaudio.dylib` a mano con
  `install_name_tool`.
- Prima run = identità TCC nuova → onboarding minimo: alert che spiega i
  permessi Microfono + Accessibilità (già gestito in parte dal launcher).
- Download modello fallito al primo avvio → messaggio chiaro nella pillola +
  retry, mai crash.

## Collaudo (definition of done)

1. `ruff check .` + `pytest` verdi (CI inclusa).
2. Build self-contained: gira senza Python homebrew (test con
   `VIBEVOICE_*` env puliti e PATH minimale).
3. E2E: dettatura reale → trascrizione → paste → auto-Return nel frontmost.
4. Finestra impostazioni: ogni controllo scrive/legge il config e ha effetto
   reale (verifica-EFFETTO, non solo UI).
5. Icona corretta in Dock/Finder/Launchpad, no bordo grigio.
6. `codesign --verify` + `spctl -a` PASS (notarizzazione quando arriva la
   password).
7. DMG monta, drag-to-Applications, app parte dal path installato.
8. Daily-driver di Mattia riacceso e funzionante a fine collaudo.

## Fuori scope (YAGNI)

- Riscrittura Tauri/Swift; backend Apple SR nel prodotto (resta whisper).
- App Store / Sparkle auto-update.
- Onboarding grafico multi-step; basta l'alert permessi.
- Migrazione del daily-driver di Mattia (decisione separata, dopo).
