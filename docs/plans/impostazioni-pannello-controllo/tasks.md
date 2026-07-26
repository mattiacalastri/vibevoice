# Tasks — P5, le Impostazioni diventano un pannello di controllo

> Fase PLAN · sess.9669 · consumata da IMPLEMENT
> Ogni task = 1 commit atomico. `git add -- <path>`, **mai** `-A`
> (`tests/test_contract.py` è modificato da un altro lavoro su questo branch).
> Regola #1 del repo: `ruff check .` + `pytest` verdi prima di ogni commit.

---

- [ ] **T0 — Verifica la sovrapposizione col lavoro full-duplex** *(rischio dichiarato)*
  - il branch è `dev/full-duplex-fase-1-2-salto-half-full-dup`: controllare che esporre
    `VIBEVOICE_VP` non collida con modifiche in corso su quel fronte
  - se collide → **STOP**, si torna a Plan invece di improvvisare
  - nessun commit: è un cancello

- [ ] **T1 — `config.py`: la chiave `vp`** *(D1)*
  - `DEFAULTS` diventa `{..., "vp": True}` — retrocompatibile per costruzione
    (`load()` riempie le chiavi mancanti dai default: verificato)
  - test in `tests/test_config.py`: un file esistente senza `vp` carica con `vp=True`
  - commit: `feat(config): chiave vp per l'elaborazione vocale di sistema`

- [ ] **T2 — `_start_engine()` passa `VIBEVOICE_VP`** *(D1)*
  - riga ~656, accanto alle altre tre `env.setdefault(...)`
  - senza questo la chiave è ornamentale: il motore continuerebbe a leggere il default
  - commit: `feat(pill): passa VIBEVOICE_VP al motore allo spawn`

- [ ] **T3 — `engine_restart_needed()` e la fine del riavvio a sproposito** *(D2)* ⭐
  - funzione pura a livello di modulo + costante `ENGINE_KEYS`
    (`lang`, `autosend`, `autosend_return`, `vp` — **`dock` escluso**)
  - `settingsChanged_` carica la configurazione **prima** di salvare, confronta, e riavvia
    solo se serve
  - test: `dock` da solo → False (**è il difetto**) · `lang` → True · nulla → False
  - commit: `fix(settings): "Dock icon" non riavvia più il motore`

- [ ] **T4 — History viva, con l'orario e lo svuotamento** *(D4, D5)*
  - `format_history_line(record)` pura: `HH:MM  testo`, tollerante ai record malformati
  - `NSTimer` 2s attivo **solo** a finestra visibile, invalidato alla chiusura
  - pulsante «Clear» (lecito: l'invariante #1 non copre `history.jsonl`, verificato)
  - test sulla funzione pura: orario · manca `ts` · manca `text` · riga corrotta
  - commit: `feat(settings): storico che si aggiorna, con orario e svuotamento`

- [ ] **T5 — Layout a sezioni, finestra ridimensionabile** *(D3)*
  - cursore verticale al posto delle coordinate a mano (`H - 50`, `H - 85`, …)
  - tre intestazioni: VOICE · BEHAVIOUR · APPEARANCE, più HISTORY
  - `+ NSWindowStyleMaskResizable`, dimensione minima, History che cresce con la finestra
  - il più visibile e il meno critico: **ultimo**
  - commit: `refactor(settings): layout a sezioni e finestra ridimensionabile`

- [ ] **T6 — Verde di repo** *(regola #1)*
  - `ruff check .` pulito · `pytest` verde (l'intera suite, non solo i nuovi)
  - se `tests/test_contract.py` (modificato da altri) è rosso **prima** delle mie
    modifiche, va detto e non nascosto sotto il mio lavoro

- [ ] **T7 — Verifica a occhio** *(gesto di Mattia)*
  - riavvio di `VibeVoice.app`, apertura di Settings
  - spuntare «Dock icon» → l'icona nel Dock cambia e **il motore NON riparte** (la prova)
  - cambiare Language → il motore riparte e la finestra lo dice
  - la History mostra gli orari e si aggiorna mentre detti

---

## Ordine

T0 (cancello) → T1 → T2 → T3 → T4 → T5 → T6 → T7.
Dal foglio alla superficie: `config.py` non ha dipendenti, la finestra li ha tutti.

**T3 è il cuore.** Se si facesse un solo task, sarebbe quello: è l'unico che ripara un
comportamento sbagliato invece di aggiungere una funzione.
