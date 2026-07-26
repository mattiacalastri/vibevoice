# P5 — Le Impostazioni diventano un pannello di controllo

> Fase RESEARCH · sess.9669 · 26 lug 2026
> Stato: **da rivedere** — nessuna decisione presa, nessuna riga di codice scritta

## Problema

La finestra Impostazioni espone **tutta** la configurazione esistente, e questo è il
punto: la configurazione è sottile. Quattro interruttori, mentre il motore legge sei
variabili — tre delle quali non hanno alcun modo di essere cambiate se non a mano
nell'ambiente. Fra queste c'è `VIBEVOICE_VP`, l'elaborazione vocale di sistema che è
la base del salto half→full-duplex.

E la finestra ha un difetto di comportamento indipendente dal contenuto: **ogni
modifica riavvia il motore**, anche quelle che col motore non c'entrano nulla.
Spuntare «Dock icon» uccide la trascrizione in corso.

## Stato attuale verificato

Letto, non ipotizzato.

### La finestra (`vibevoice.py:1022-1107`)

420×380, `NSWindowStyleMaskTitled | NSWindowStyleMaskClosable` — **non ridimensionabile**.
Coordinate assolute calcolate a mano su `H - 50`, `H - 85`, `H - 115`, `H - 145`, `H - 180`.
Contiene: Language (menu a tendina it/en) · Autosend · Auto-Return · Dock icon ·
riquadro History in sola lettura alto 150px.

### Le manopole del motore

| variabile | letta da | esposta? |
|---|---|---|
| `VIBEVOICE_LANG` | `engine.py:100` | ✅ |
| `VIBEVOICE_AUTOSEND` | `engine.py:102` | ✅ |
| `VIBEVOICE_AUTOSEND_RETURN` | `engine.py:103` | ✅ |
| `VIBEVOICE_MODEL` | `engine.py:101` | ❌ mai impostata dalla pillola |
| `VIBEVOICE_VP` | `engine.py:104` | ❌ mai impostata dalla pillola |
| `VIBEVOICE_SILERO_MODEL` | `engine.py:618` | ❌ mai impostata dalla pillola |

`_start_engine()` (righe 650-661) costruisce l'ambiente del figlio e imposta **solo le
prime tre**. Le altre restano ai default cablati nel motore.

### Il riavvio indiscriminato

`settingsChanged_` (righe 1095-1107) raccoglie i 4 valori, salva, applica la politica
del Dock, e poi chiama `self.restartEngine_(None)` **incondizionatamente**. `dock` è
un'impostazione della sola pillola: non finisce nemmeno nell'ambiente del motore.

### La History

`history.jsonl` è `{"ts": float, "text": str}`, **cappata a 20 righe**
(`engine.py:113 HISTORY_MAX = 20`), scritta dal motore. `_reload_history()` (1081-1093)
scarta il timestamp — che **esiste** — e stampa solo `• testo`. Viene chiamata **una
volta sola**, all'apertura: la finestra aperta non si aggiorna mai.

### Compatibilità della configurazione — verificata

`config.load()` fa `{k: raw.get(k, v) for k, v in DEFAULTS.items()}`: aggiungere chiavi a
`DEFAULTS` è **retrocompatibile per costruzione**, i file esistenti prendono i default
senza migrazione. `save()` scrive solo le chiavi di `DEFAULTS` (write atomica su tmp +
`os.replace`).

### ❌ Il selettore microfono NON è costruibile qui

Il motore **non sa scegliere il dispositivo**: nessuna variabile di device, nessun
`sd.default.device`. Usa l'ingresso di sistema. Aggiungerlo significa modificare
`_SounddeviceCapture` e `_VoiceProcessingCapture`, cioè il percorso di cattura protetto
dagli invarianti #3 e #8. **Fuori perimetro** — era nel mockup approvato, va ritirato
esplicitamente.

## Vincoli & rischi

1. **Regola #1 del repo**: `ruff check .` e `pytest` verdi prima di ogni commit. La CI
   li rigira su macOS. Disponibili: ruff 0.15.14, pytest 9.0.3.
2. **Invariante #4 — il contratto dei file di stato è portante.** Se cambia il formato di
   un file, cambiano writer **e tutti i reader nello stesso commit**.
3. **Invariante #1** — il motore è l'unico scrittore di `state`/`levels.bin`/`raw.txt`.
   La finestra Impostazioni **legge** `history.jsonl`: se acquisisce un pulsante «Svuota»,
   diventa uno scrittore di un file del motore. Da decidere consapevolmente (→ D5).
4. **Regola #2** — i test non toccano mai `~/.vibevoice/` vero: si devia su `tmp_path`.
5. **`config.py` è un modulo foglia**, importato solo da `vibevoice.py`. Il motore riceve
   i valori **via ambiente allo spawn**, non importando config. Non rompere il disaccoppiamento.
6. **Modifica non committata di un altro lavoro** su questo branch: `tests/test_contract.py`
   risulta `M`. Non è mia. Commit chirurgici, mai `git add -A`.
7. Branch corrente: `dev/full-duplex-fase-1-2-salto-half-full-dup` — lavoro full-duplex
   in corso, che è esattamente ciò che `VIBEVOICE_VP` governa. Possibile sovrapposizione.
8. La finestra gira dentro l'app: verificarla richiede di far ripartire `VibeVoice.app`.

## Punti-decisione aperti

- **D1 — Quali delle tre manopole nascoste si espongono, e come?** `VP` è un booleano
  chiaro. `MODEL` è una stringa: elenco chiuso, campo libero, o elenco + «altro»?
  `SILERO_MODEL` è un percorso a un file: ha senso in un'interfaccia, o resta da ambiente?
- **D2 — Con quale regola si decide se riavviare il motore?** Confronto chiave-per-chiave
  fra vecchia e nuova configurazione, o una lista dichiarata di chiavi «lato motore»?
  E cosa si mostra all'utente quando il riavvio avviene?
- **D3 — Come si organizza il layout?** Le coordinate assolute non reggono l'aggiunta di
  righe. Sezioni con intestazioni, finestra ridimensionabile, e quale altezza minima?
- **D4 — La History si aggiorna a finestra aperta?** Con un timer, o su notifica? E i
  timestamp: orario assoluto o «2 min fa»?
- **D5 — Il pulsante «Svuota» viola l'invariante #1?** `history.jsonl` lo scrive il motore.
  Se lo cancella la pillola, il motore lo ricrea in append: da verificare se l'invariante
  copre `history.jsonl` (il §2 lo elenca fra i file di stato?) o solo `state`/`levels`/`raw`.
- **D6 — Lingua dell'interfaccia.** Tutto il repo — codice, `AGENTS.md`, menu — è in
  inglese; il repo però è **privato** dall'8 lug, quindi la motivazione «è OSS pubblico»
  non vale più. Si resta in inglese per coerenza o si passa all'italiano come il daily driver?
- **D7 — Cosa si collauda in automatico?** `config.py` ha già `tests/test_config.py`. La
  finestra è AppKit: quanto della logica si può estrarre e testare senza aprire una finestra?

## Fuori perimetro (esplicitamente)

- Selettore del microfono (richiede modifiche al percorso di cattura — vedi sopra).
- P3, i due stack che si contendono il microfono.
- Qualunque modifica a `engine.py`: qui si **espone** ciò che il motore già legge.
