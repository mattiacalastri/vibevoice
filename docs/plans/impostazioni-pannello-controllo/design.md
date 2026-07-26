# Design — P5, le Impostazioni diventano un pannello di controllo

> Fase PLAN · sess.9669 · legge `spec.md`
> **Nessun codice finché Mattia non approva.** Due decisioni sono sue: D1 e D6.

## Principio guida

> La logica esce dalle callback AppKit e diventa funzioni pure.

È ciò che rende collaudabile una finestra che altrimenti si può solo guardare — e
il motivo per cui oggi `settingsChanged_` può riavviare il motore a sproposito senza
che nessun test se ne accorga.

---

## ⚠️ D1 — Quali manopole esporre → **decisione di Mattia**

Ti avevo mostrato tre manopole nascoste. Dopo aver letto il codice **ne raccomando una**,
e ti dico perché sulle altre due ho cambiato idea.

**Esporre `VIBEVOICE_VP`** (elaborazione vocale di sistema: cancellazione d'eco +
soppressione rumore). È un booleano netto, ha un default sicuro (`1`), e per la
degradazione è già contratto (regola #8: se fallisce, ripiega su sounddevice e il motore
si comporta come prima). È anche la manopola che governa il salto half→full-duplex —
lo stesso tema del branch su cui siamo.

**NON esporre `VIBEVOICE_MODEL`.** Non per pigrizia: la tua deep-research sess.9465 dice
esplicitamente che i benchmark degli STT su Apple Silicon sono **solo in inglese** e che
l'italiano va misurato in casa **prima di ogni swap**. Un menu a tendina invita a cambiare
modello a intuito, contro una conclusione che hai già pagato per ottenere. Un campo di
testo libero è peggio: un refuso nel nome del repo rompe la trascrizione in silenzio.
Se lo vuoi, la forma giusta è dopo un benchmark it-IT, con un elenco chiuso di nomi
verificati — non prima.

**NON esporre `VIBEVOICE_SILERO_MODEL`.** È il percorso a un file di modello: un
override da sviluppatore, non un'impostazione. In un'interfaccia diventa un campo dove
si può solo sbagliare, e la degradazione (senza Silero si ripiega sulla soglia RMS) è
già contratto.

> **Se preferisci tutte e tre, si fanno** — cambia solo il contenuto della tabella dei
> campi, non l'architettura. Dimmelo annotando qui.

**Fuori perimetro comunque**: il selettore del microfono. Il motore non sa scegliere il
dispositivo e aggiungerlo significa toccare il percorso di cattura (invarianti #3 e #8).
Era nel mockup che hai approvato: lo ritiro esplicitamente, non lo lascio sparire.

## D2 — Quando riavviare il motore → **solo se cambia una chiave che il motore legge**

Una costante dichiarata, non un'euristica:

```
ENGINE_KEYS = {"lang", "autosend", "autosend_return", "vp"}   # "dock" NON c'è
```

`settingsChanged_` confronta la configurazione vecchia con la nuova e riavvia **solo** se
l'intersezione delle chiavi cambiate con `ENGINE_KEYS` non è vuota. Spuntare «Dock icon»
non tocca più la trascrizione in corso.

Il confronto vive in una **funzione pura** `engine_restart_needed(old, new) -> bool`,
collaudabile senza aprire una finestra. È il cuore del difetto: merita un test, non un `if`
sepolto in una callback.

Quando il riavvio avviene, la finestra lo **dice** (una riga di stato che si spegne dopo
qualche secondo). Oggi il motore riparte in silenzio e chi sta dettando non capisce perché
la frase si è interrotta.

## D3 — Layout → **sezioni generate da un cursore, finestra ridimensionabile**

Le coordinate assolute a mano (`H - 50`, `H - 85`, `H - 115`…) non reggono l'aggiunta di
una riga: ogni inserimento richiede di ricalcolare tutte le successive. Si sostituiscono
con un piccolo cursore verticale che scende, e tre intestazioni di sezione:

```
VOICE          Language · Voice processing (eco + rumore)
BEHAVIOUR      Auto-paste · Auto-Return
APPEARANCE     Dock icon
HISTORY        [lista]                                    [Clear]
```

Finestra `+ NSWindowStyleMaskResizable`, dimensione minima pari all'altezza del contenuto,
e la History con `autoresizingMask` in crescita: allargando la finestra cresce il riquadro
storico, che è l'unica cosa che ha senso far crescere.

## D4 — History → **si aggiorna, con l'orario**

Il timestamp **esiste già** in `history.jsonl` (`{"ts": float, "text": str}`) e viene
buttato via. Si mostra come `HH:MM  testo`.

Aggiornamento con un `NSTimer` da 2s **attivo solo a finestra visibile**, invalidato alla
chiusura: una finestra Impostazioni aperta non deve costare nulla quando è chiusa.

La formattazione di una riga diventa una funzione pura `format_history_line(record)` —
testabile, e con un comportamento definito sui record malformati (record senza `ts` o
senza `text` non devono far saltare l'intera lista).

## D5 — Pulsante «Svuota» → **lecito, verificato**

L'invariante #1 copre **esattamente** `state` / `levels.bin` / `raw.txt`. `history.jsonl`
**non** è compreso (verificato in `AGENTS.md:124`). La pillola può troncarlo.

Il motore vi scrive in append: se svuoti mentre sta trascrivendo, la frase successiva
ricomincia da capo la lista. Comportamento corretto, nessuna corsa distruttiva.

## ⚠️ D6 — Lingua dell'interfaccia → **decisione di Mattia** (raccomando inglese)

Tutto il repo è in inglese: codice, commenti, `AGENTS.md`, le voci di menu esistenti.
Il fatto che il repo sia diventato **privato** l'8 luglio toglie l'argomento «è OSS
pubblico», ma non ne crea uno a favore dell'italiano: mezzo menu in una lingua e mezzo
nell'altra sarebbe peggio di entrambe le scelte.

**Raccomando inglese**, per coerenza col resto del repo. Ma è il tuo strumento: se lo
vuoi in italiano come il daily driver, si traduce **tutto** il menu nello stesso commit,
non solo la finestra.

## D7 — Cosa si collauda

Tre funzioni pure estratte dalle callback, tutte senza AppKit:

| funzione | test |
|---|---|
| `engine_restart_needed(old, new)` | `dock` da solo → **False** (il difetto) · `lang` → True · niente cambiato → False |
| `format_history_line(record)` | orario formattato · record senza `ts` · senza `text` · JSON corrotto non fa saltare la lista |
| `config.DEFAULTS` estesa | i file di configurazione esistenti (senza `vp`) prendono il default senza migrazione |

`tests/test_config.py` ha già il pattern `_redirect` con monkeypatch: si riusa.
Nessun test tocca `~/.vibevoice/` vero (regola #2).

La finestra in sé resta verificabile solo a occhio → è l'ultimo passo, con Mattia.

## Sequenza di costruzione

1. `config.py` — la chiave `vp` nei DEFAULTS (foglia, retrocompatibile per costruzione)
2. `_start_engine()` — passa `VIBEVOICE_VP` allo spawn (senza questo la chiave è ornamentale)
3. `engine_restart_needed()` + il suo uso in `settingsChanged_` — **il difetto vero**
4. `format_history_line()` + aggiornamento dal vivo + pulsante Svuota
5. Il layout a sezioni e la finestra ridimensionabile — il più visibile, ultimo
6. Test su 1/3/4, `ruff check .` + `pytest` verdi (regola #1)
7. Verifica a occhio: riavvio di `VibeVoice.app`, gesto di Mattia

## Rischi dichiarati

- **Il branch è `dev/full-duplex-fase-1-2-salto-half-full-dup`**, cioè lavoro in corso
  proprio su ciò che `VIBEVOICE_VP` governa. Esporre quella manopola potrebbe sovrapporsi
  a modifiche non ancora committate di quel lavoro. Da verificare prima di iniziare.
- **`tests/test_contract.py` risulta modificato e non è mio.** Commit chirurgici.
- **`engine.py` non si tocca**: qui si espone ciò che il motore già legge. Se durante
  l'esecuzione servisse modificarlo, è un buco di design → si torna a Plan.
