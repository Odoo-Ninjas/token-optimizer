---
name: compress
description: >-
  Sitzung verdichten: aktuellen Stand, anstehende Aufgaben sowie Ueberlegungen
  und begruendete Erkenntnisse der Sitzung in NEXT.md im Ticket-Ordner
  schreiben, damit danach mit /clear sauber weitergemacht werden kann.
  Nutzen bei "/compress", "sichere den Stand", "schreib alles in NEXT.md und
  dann clear", "Sitzung zusammenfassen fuer spaeter".
---

# /compress

Ziel: Nach `/clear` soll eine frische Sitzung allein aus `NEXT.md` (plus
`TASK.md`) weiterarbeiten koennen — ohne den Chatverlauf.

## 1. Ticket-Ordner finden

Der Ordner unter `~/ai/work/...`, in dem die `TASK.md` liegt (ggf. vom cwd aus
nach oben suchen; Code-Unterordner `ZO-XXXXX-<repo>` ist NICHT der Ort).
Gibt es keinen Ticket-Ordner: `NEXT.md` ins aktuelle Arbeitsverzeichnis.

## 1b. Jev-Triage (was bleibt, was fliegt)

Transkript der aktuellen Sitzung: neueste `.jsonl` im Projektordner des
**Primary working directory** (Pfad mit `/` und `.` durch `-` ersetzt):

```bash
uv run --script ~/.claude/hooks/jev-context-triage.py \
  "$(ls -t ~/.claude/projects/<slug>/*.jsonl | head -1)"
```

Die Tabelle stuft die grossen Tool-Ausgaben ein: `keep_verbatim` praezise
uebernehmen (Pfade, Zeilen, Befehle), `keep_gist` auf die relevanten Fakten
kuerzen, `drop` weglassen. Bewertet wird lokal mit Laya (LaunchAgent
`de.zebroo.laya`, Port 8781) - es verlaesst nichts den Rechner, daher auch fuer
DIN-Sitzungen. Scheitert das Skript (z.B. Laya-Dienst laeuft nicht), ohne
Bewertung weitermachen und das in der Abschlussmeldung erwaehnen.

## 2. NEXT.md schreiben / aktualisieren

Existiert die Datei schon: **ergaenzen und bereinigen, nicht blind
ueberschreiben** — Erledigtes nach `## Erledigt` (mit Datum), neue Punkte
einsortieren, Veraltetes streichen. Das Format oben bleibt kompatibel zu
`/whats-next` und `/go` (nummerierte Liste zuerst).

```markdown
# Next

1. [ ] Kurzer Satz, was zu tun ist — (Kontext/warum)
2. [ ] ...

## Stand (YYYY-MM-DD)
- Was ist fertig, was halb fertig (Branch, Commit, PR-Link, geaenderte Dateien)
- Was laeuft/ist deployed wo (Instanz, Host, Port)
- Offene Rueckfragen an Kunde/Kollegen

## Autorisierungen
- Erlaubt: <Schritt> fuer <Gegenstand: Branch/PR/Host> — seit YYYY-MM-DD,
  Marcs Wortlaut kurz („ja pushen")
- Ausdruecklich NICHT: <Grenze> (z.B. „Bindung nicht aendern")

## Erkenntnisse & Begruendungen
- Erkenntnis — **Warum:** worauf sie beruht (Log, Code-Stelle `datei:zeile`,
  Test, Messung)
- Entscheidung X statt Y — **Warum:** ...
- Verworfene Ansaetze und warum sie nicht funktioniert haben (damit sie nicht
  nochmal probiert werden)

## Stolpersteine / Befehle
- Nicht-offensichtliche Befehle, Pfade, Flags, die in der Sitzung gebraucht
  wurden

## Erledigt
- [x] YYYY-MM-DD Was fertig ist
```

Regeln:
- **Konkret statt Prosa**: Dateipfade, Funktionsnamen, IDs, Befehle, Commits.
- **Begruendungen sind Pflicht** bei Erkenntnissen und Entscheidungen — genau
  das geht bei `/clear` sonst verloren.
- Nur festhalten, was nicht ohnehin im Code/Git steht.
- **Autorisierungen**: jede in der Sitzung ausdruecklich erteilte Freigabe
  (pushen, mergen, installieren, deployen …) samt Grenzen in
  `## Autorisierungen` uebernehmen, damit die naechste Sitzung nicht erneut
  fragt. Nur was Marc wirklich gesagt hat, nichts ableiten; schon Erledigtes
  und Zurueckgenommenes streichen. Abschnitt weglassen, wenn es keine gibt.
- **DIN-Projekte / personenbezogene Daten**: keine Echtdaten in NEXT.md, nur
  Aggregate und Pfade zu lokalen Ergebnisdateien.
- Keine Secrets/Passwoerter.

## 3. Abschluss

1. Kurz (max. 5 Zeilen) melden: Pfad der NEXT.md, Anzahl offener Punkte,
   was `/go` als naechstes nehmen wuerde.
2. Als letzte Zeile: **`→ Jetzt /clear eingeben.`**

`/clear` ist ein eingebautes CLI-Kommando und kann vom Modell nicht selbst
ausgeloest werden. Hat token-optimizer (`~/ai/token-optimizer`, Plugin
`token-optimizer`) dieses `/compress` wegen der Kontextgrenze oder per
`/tokopt-chain` ausgeloest, fuehrt er `/clear` und danach `/whats-next` selbst
aus - vorausgesetzt, die NEXT.md wurde in diesem Zug geschrieben. Sonst tippt
der User `/clear`. Nach dem Clear bekommt die neue Sitzung die NEXT.md
automatisch als Kontext (auch nach einem `/clear` von Hand, dann mit dem
ersten Prompt); weiter mit `/go`.
