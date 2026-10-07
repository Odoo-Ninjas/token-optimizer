---
name: whats-next
description: >-
  Ganz knapp sagen, was im aktuellen Arbeitsverzeichnis als Naechstes ansteht.
  Nutzen bei "/whats-next", "was ist sonst zu tun", "was steht an", "wie
  weiter", "was kommt jetzt". Liest NEXT.md im Ticket-Ordner (legt sie an,
  wenn sie fehlt) und gibt eine kurze Liste aus - keine Prosa, keine
  Erklaerungen, keine Arbeit. Zum Abarbeiten gibt es /go.
---

# /whats-next

Eine kurze Liste. Sonst nichts.

## Woher die Liste kommt

`NEXT.md` im Ticket-Ordner (der Arbeitsordner unter `~/ai/work/...`, in dem die
`TASK.md` liegt) ist die Quelle. Format, bewusst simpel:

```markdown
# Next

1. [ ] Kurzer Satz, was zu tun ist — optional: warum/Kontext in Klammern
2. [ ] ...

## Erledigt
- [x] 2026-09-13 Was fertig ist
```

Fehlt die Datei: **einmal** aus dem vorhandenen Material aufbauen — `TASK.md`,
`DEPLOY.md`, Review-/Befund-Notizen im Ordner, offene PRs im zugehoerigen Repo
(`gh pr list`), offensichtlich offene Punkte aus dem Chatverlauf. Danach ist
`NEXT.md` die Wahrheit und wird gepflegt, nicht jedes Mal neu hergeleitet.

## Ausgabe

- **Hoechstens fuenf Punkte**, nach Wichtigkeit sortiert, einer pro Zeile.
- Ein Satz pro Punkt. Kein Warum, keine Optionen, keine Codebloecke.
- Nummeriert, damit `/go 3` funktioniert.
- Darunter genau eine Zeile: was der Vorschlag fuer `/go` ohne Argument waere.
- Steht nichts an: ein Satz, der das sagt.

Beispiel fuer die gesamte Antwort:

```
1. Offsite-Backup konfigurieren — der Notnagel ist ein lokaler Dump.
2. cicd.agent_exec_mode auf argv stellen (Gate ist gruen).
3. PR #24 durchsehen und mergen.

/go nimmt 1.
```

## Was dieser Skill NICHT tut

- Nicht arbeiten. Kein Code, keine Deploys, keine Recherche ueber das
  Zusammenstellen der Liste hinaus.
- Nicht begruenden. Wer das Warum will, fragt nach.
- Keine Statusberichte, keine Zusammenfassung des bisher Erreichten.
