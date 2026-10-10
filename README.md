# token-optimizer

Hält Claude-Code-Sitzungen unter einer Kontextgrenze (Standard **250k Tokens**),
ohne dass der Stand verloren geht. Statt `/compact` (Claude fasst selbst
zusammen, man weiß nicht genau was) läuft unsere Kette:

1. **`/compress`**: die Sitzung schreibt Stand, offene Punkte, Begründungen und
   Freigaben in die `NEXT.md` im Ticket-Ordner.
2. **`/clear`**: frische Sitzung; sie bekommt die `NEXT.md` als Kontext mit.
3. **`/whats-next`**: die neue Sitzung listet die offenen Punkte auf.

Die Kette führt ein **Claude-Code-Mod** aus (Plugin `token-optimizer` in
`plugin/`, Function Hooks in TypeScript). Er läuft im Claude-Code-Prozess selbst
und ruft die Kommandos über die Plugin-API auf. Das funktioniert überall –
Terminal, Desktop-App, VS Code, mit oder ohne zellij. Früher wurden die
Kommandos per `zellij write-chars` in die Kachel getippt; das ist entfallen.

## Was man sieht

- In der Statuszeile: `token-optimizer: ctx 162k/250k`.
- Ab der Grenze startet die Kette von selbst. `/tokopt-chain` startet sie sofort,
  egal wie voll der Kontext ist.
- Ein gedimmter Vorschlag im Eingabefeld (Tab zum Übernehmen) wird vor
  `/compress` gemerkt und nach `/whats-next` wieder angezeigt.
- Wird die `NEXT.md` im `/compress`-Zug nicht geschrieben, stoppt die Kette mit
  einem Hinweis; ein neuer Versuch erst, wenn der Kontext um weitere 50k
  gewachsen ist.
- Nach einem `/clear` von Hand bekommt der erste Prompt die `NEXT.md` mit.

## Inhalt

| Pfad | Zweck |
|---|---|
| `plugin/` | Der Mod: `hooks/register.ts`, Tests in `tests/` |
| `.claude-plugin/marketplace.json` | Macht das Repo zum Marketplace `token-optimizer` |
| `skills/compress` | Skill `/compress`: NEXT.md schreiben |
| `skills/whats-next` | Skill `/whats-next`: offene Punkte kurz auflisten |
| `hooks/jev-context-triage.py` | Bewertet große Tool-Ausgaben lokal mit Laya (keep/gist/drop); läuft in der Kette vor `/compress` (Mod bzw. `codex_chain.py`), beim `/compress` von Hand ruft es der Skill auf. Protokoll: `~/.claude/token-optimizer/triage.log` |
| `token_optimizer.py` | `install` |

## Installation

```bash
git clone git@github.com:Odoo-Ninjas/token-optimizer.git ~/ai/token-optimizer
python3 ~/ai/token-optimizer/token_optimizer.py install
```

`install` ist idempotent und:

- verlinkt `~/.claude/skills/{compress,whats-next}` und
  `~/.claude/hooks/jev-context-triage.py` ins Repo,
- entfernt die Settings-Hooks früherer Versionen (`token_optimizer.py stop`,
  `session-start`, `prompt` sowie `context-guard.py`, `next-after-clear.py`)
  aus `~/.claude/settings.json`,
- trägt das Repo als Marketplace ein und installiert das Plugin
  (`claude plugin marketplace add ~/ai/token-optimizer`,
  `claude plugin install token-optimizer@token-optimizer --scope user`),
- sichert alles Ersetzte vorher nach `~/.claude/token-optimizer/backup/<zeit>/`.

Wirkt in neu gestarteten Sitzungen. Weil der Marketplace ein Ordner ist, liest
Claude Code das Plugin direkt aus dem Repo: nach `git pull` reicht
`/reload-plugins`, kein Neuinstallieren.

## Einstellungen

Über `/config` (Zeilen `token-optimizer.*`) oder
`claude plugin configure token-optimizer@token-optimizer`:

| Option | Standard | |
|---|---|---|
| `threshold` | `250000` | Grenze in Tokens |
| `auto` | `true` | Kette ab der Grenze selbst starten; aus = nur `/tokopt-chain` |
| `injectNext` | `true` | `NEXT.md` nach `/clear` als Kontext mitgeben |

Die früheren Umgebungsvariablen `TOKEN_OPTIMIZER_*` gibt es nicht mehr.

## Wie es funktioniert

- **Kontextgröße**: Ereignis `session.measure` nach jedem Turn (dieselbe Zahl
  wie in der Statuszeile von Claude Code).
- **Kette**: `/compress` über `$.command.run`; der nächste Turn ist der des
  Skills. Ist danach die `NEXT.md` neuer (oder nennt der Skill `/clear`), folgen
  `/clear` und `/whats-next`.
- **Übergabe der NEXT.md**: nach `/clear` aus der Kette per `$.session.append`
  als Notiz in die neue Unterhaltung, nach `/clear` von Hand als `context` am
  ersten Prompt.
- Ein Zustand, der 30 min nicht weiterkommt, gilt als verworfen.

## Tests

```bash
claude plugin test plugin           # der Mod
python3 -m unittest discover -s tests  # install
```

Typprüfung des Mods: Claude Code legt die Typen beim Laden nach
`plugin/.claude-plugin/types/` (nicht eingecheckt), danach `npx -p typescript@5 tsc -p plugin`.
