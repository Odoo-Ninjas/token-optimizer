# token-optimizer

Hält Claude-Code-Sitzungen unter einer Kontextgrenze (Standard **350k Tokens**),
ohne dass der Stand verloren geht. Statt `/compact` (Claude fasst selbst
zusammen, man weiß nicht genau was) läuft unsere Kette:

1. **`/compress`**: die Sitzung schreibt Stand, offene Punkte, Begründungen und
   Freigaben in die `NEXT.md` im Ticket-Ordner.
2. **`/clear`**: frische Sitzung; der SessionStart-Hook gibt ihr die `NEXT.md`
   als Kontext mit.
3. **`/whats-next`**: die neue Sitzung listet die offenen Punkte auf.

Alle drei Kommandos tippt token-optimizer selbst in die zellij-Kachel, in der
die Sitzung läuft (`ZELLIJ_SESSION_NAME`/`ZELLIJ_PANE_ID`, die erben die Hooks).
`/clear` wird nur getippt, wenn die `NEXT.md` im `/compress`-Zug wirklich
geschrieben wurde. Sonst bleibt die Sitzung, wie sie ist, und es erscheint ein
Hinweis.

Außerhalb von zellij kann nichts getippt werden. Dort greift der Prompt-Hook:
er sagt Claude beim nächsten Prompt, erst `/compress` zu machen (inklusive
Jev-Triage, früher `context-guard.py`).

## Inhalt

| Datei | Zweck |
|---|---|
| `token_optimizer.py` | Hooks (`stop`, `session-start`, `prompt`), Tippen (`type`), `install` |
| `hooks/jev-context-triage.py` | Bewertet große Tool-Ausgaben lokal mit Laya (keep/gist/drop) |
| `skills/compress` | Skill `/compress`: NEXT.md schreiben |
| `skills/whats-next` | Skill `/whats-next`: offene Punkte kurz auflisten |

## Installation

```bash
git clone git@github.com:Odoo-Ninjas/token-optimizer.git ~/ai/token-optimizer
python3 ~/ai/token-optimizer/token_optimizer.py install
```

`install` ist idempotent und:

- trägt die drei Hooks in `~/.claude/settings.json` ein und entfernt die alten
  Einträge für `context-guard.py` und `next-after-clear.py`,
- verlinkt `~/.claude/skills/{compress,whats-next}` und
  `~/.claude/hooks/jev-context-triage.py` ins Repo,
- sichert alles Ersetzte vorher nach `~/.claude/token-optimizer/backup/<zeit>/`.

Wirkt in neu gestarteten Sitzungen.

## Einstellungen (Umgebungsvariablen)

| Variable | Standard | |
|---|---|---|
| `TOKEN_OPTIMIZER_THRESHOLD` | `350000` | Grenze in Tokens |
| `TOKEN_OPTIMIZER_STEP` | `50000` | Prompt-Fallback: erneute Erinnerung alle n Tokens |
| `TOKEN_OPTIMIZER_OFF` | | `1` schaltet alles ab |
| `TOKEN_OPTIMIZER_TRIAGE_OFF` | | `1`: Erinnerung ohne Jev-Triage |
| `TOKEN_OPTIMIZER_TRIAGE_TIMEOUT` | `120` | Sekunden für die Triage |

Kontextgröße = input + cache_read + cache_creation der letzten Antwort im
Hauptstrang (Subagenten zählen nicht); nach `/compact` gilt der `postTokens`-Wert.

## Wie es funktioniert

Der Zustand der Kette liegt pro zellij-Kachel in
`~/.claude/token-optimizer/state/chain-<session>-<pane>.json`. Nach `/clear`
gibt es eine neue Session-ID, die Kachel bleibt dieselbe.

- **Stop-Hook**: Kontext ≥ Grenze → Zustand `compress`, `/compress` tippen.
  Endet der `/compress`-Zug mit frischer NEXT.md → Zustand `clear`, `/clear`
  tippen. Ohne frische NEXT.md → `failed`, in dieser Sitzung kein neuer Versuch.
- **SessionStart-Hook (`clear`)**: NEXT.md als Kontext; steht die Kette auf
  `clear` → Zustand löschen, `/whats-next` tippen.
- Getippt wird aus einem abgekoppelten Prozess (der Hook muss erst
  zurückkehren), 2 s verzögert. Enter geht erst raus, wenn das Kommando auf dem
  Bildschirm steht. Klappt das Tippen nicht, wird der Zustand verworfen, und
  der nächste Stop beginnt neu.
- Ein Zustand, der 30 min nicht weiterkommt, gilt als verworfen.

Protokoll: `~/.claude/token-optimizer/log`.

## Tests

```bash
python3 -m unittest discover -s tests
```
