# Part of Zebroo GmbH. See LICENSE file for full copyright and licensing details (www.zebroo.de/license).
"""The token-optimizer chain for Codex CLI.

Claude Code runs the chain in a mod (plugin/). Codex has no mods, but it has
settings hooks in the shape of Claude Code's: on every Stop it runs a command
with the hook payload (cwd, session_id, transcript_path) on stdin. This module
is that command (`token_optimizer.py codex-stop`). Once the context of the
session reaches the threshold, it types into the session's tile through
`shelltask type`:

    compress prompt  -> the session writes its state into NEXT.md
    /new             -> fresh conversation (Codex' /clear)
    whats-next prompt-> the new conversation lists the open items

Before the compress prompt the detached typer runs the Laya triage
(hooks/jev-context-triage.py) on the rollout and leaves its table in a file the
prompt points to, so the rating does not depend on the model running it.

The steps go out one per Stop, like in the mod: /new only once NEXT.md was
really written in the compress turn. The context size comes from the rollout
file Codex writes (last `token_count` event).

Only sessions inside zellij are handled: the same hooks also run for Codex in
the desktop app, where there is no tile to type into.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

THRESHOLD = 180_000  # Codex' window is ~258k, it compacts on its own near the end
STEP = 50_000        # after the chain (or a failed try) wait for this much growth
STALE = 30 * 60      # a chain stuck this long is dropped
TYPE_DELAY = 2.0     # let Codex finish the turn before keys arrive
NEW_SETTLE = 4.0     # /new needs a moment before the composer takes text
TRIAGE_TIMEOUT = 300  # Laya rates up to 60 outputs in batches
TRIAGE = Path(__file__).resolve().parent / "hooks" / "jev-context-triage.py"

COMPRESS = ("Kontext ist voll ({k}): Führe jetzt den Skill compress aus — "
            "schreib Stand, offene Punkte, Begründungen und Freigaben in NEXT.md im "
            "Ticket-Ordner (Anleitung: ~/.codex/skills/compress/SKILL.md). Die Laya-Triage "
            "ist schon gelaufen, nicht erneut starten: Ergebnis in {triage}. Danach nichts weiter.")
WHATS_NEXT = ("Weiter nach /new: Führe den Skill whats-next aus "
              "(~/.codex/skills/whats-next/SKILL.md) — lies TASK.md und NEXT.md im Ticket-Ordner "
              "und liste kurz, was ansteht.")


def data_dir() -> Path:
    d = Path.home() / ".codex" / "token-optimizer"
    (d / "state").mkdir(parents=True, exist_ok=True)
    return d


def log(msg: str) -> None:
    with open(data_dir() / "log", "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def threshold() -> int:
    try:
        return int(os.environ.get("TOKEN_OPTIMIZER_CODEX_THRESHOLD", "")) or THRESHOLD
    except ValueError:
        return THRESHOLD


# --- Context size ------------------------------------------------------------

def find_rollout(data: dict) -> Path | None:
    p = data.get("transcript_path")
    if p and Path(p).is_file():
        return Path(p)
    sid = data.get("session_id") or ""
    if not sid:
        return None
    hits = sorted((Path.home() / ".codex" / "sessions").glob(f"*/*/*/rollout-*{sid}.jsonl"),
                  key=lambda f: f.stat().st_mtime)
    return hits[-1] if hits else None


def context_tokens(rollout: Path) -> int:
    """Size of the context after the last model call: last_token_usage of the
    newest token_count event (input plus the answer that is now part of it)."""
    tokens = 0
    with open(rollout, errors="replace") as f:
        for line in f:
            if '"token_count"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            payload = ev.get("payload", ev)
            info = (payload or {}).get("info") or {}
            last = info.get("last_token_usage") or {}
            if last.get("total_tokens"):
                tokens = int(last["total_tokens"])
    return tokens


# --- Where things are --------------------------------------------------------

def ticket_dir(cwd: Path) -> Path:
    """The folder with TASK.md above cwd (NEXT.md lives there), else cwd."""
    for d in [cwd, *cwd.parents]:
        if (d / "TASK.md").is_file():
            return d
        if d == Path.home():
            break
    return cwd


def mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def state_path(dir: Path) -> Path:
    return data_dir() / "state" / (hashlib.sha1(str(dir).encode()).hexdigest()[:16] + ".json")


def load_state(dir: Path) -> dict:
    try:
        st = json.loads(state_path(dir).read_text())
    except (OSError, ValueError):
        return {}
    if st.get("phase", "idle") != "idle" and time.time() - st.get("at", 0) > STALE:
        return {"phase": "idle", "base": st.get("base", 0)}
    return st


def save_state(dir: Path, **st) -> None:
    st["at"] = time.time()
    state_path(dir).write_text(json.dumps(st))


# --- Typing ------------------------------------------------------------------

def shelltask() -> str:
    return shutil.which("shelltask") or str(Path.home() / ".local" / "bin" / "shelltask")


def spawn(dir: Path, steps: list) -> None:
    """Run the typing steps from a detached process: the hook has to return
    first, Codex is still finishing the turn when it runs."""
    with open(data_dir() / "log", "a") as out:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().parent / "token_optimizer.py"),
             "codex-type", str(dir), json.dumps(steps)],
            stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
        )


def triage_path(dir: Path) -> Path:
    return state_path(dir).with_suffix(".triage.md")


def run_triage(rollout: str, out: str) -> None:
    """Rate the rollout with Laya; the table (or why there is none) goes to out."""
    try:
        r = subprocess.run(["uv", "run", "-q", "--script", str(TRIAGE), rollout],
                           capture_output=True, text=True, timeout=TRIAGE_TIMEOUT)
        if r.returncode == 0 and r.stdout.strip():
            text = r.stdout.strip()
        else:
            lines = (r.stdout.strip() or r.stderr.strip()).splitlines()
            text = f"Laya-Triage fehlgeschlagen ({lines[-1] if lines else f'exit {r.returncode}'})"
    except (OSError, subprocess.SubprocessError) as exc:
        text = f"Laya-Triage fehlgeschlagen ({exc})"
    Path(out).write_text(text + "\n", encoding="utf-8")
    log(f"{out}: {text.splitlines()[-1]}")


def run_steps(dir: str, steps: list) -> None:
    """steps: [[raw, text], ...] — one after the other, each via shelltask type.
    A step ["triage", rollout, out] runs the Laya triage instead of typing."""
    time.sleep(TYPE_DELAY)
    for i, (raw, text, *rest) in enumerate(steps):
        if raw == "triage":
            run_triage(text, rest[0])
            continue
        if i:
            time.sleep(NEW_SETTLE)
        cmd = [shelltask(), "type", *(["--raw"] if raw else []), dir, "--", text]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            log(f"{dir}: typing {text[:40]!r} failed: {(r.stderr or r.stdout).strip()}")
            return
        log(f"{dir}: typed {text[:40]!r}")


# --- The hook ----------------------------------------------------------------

def on_stop(data: dict, typer=spawn) -> str:
    """One step of the chain per Stop. Returns what happened (for tests/log)."""
    if not os.environ.get("ZELLIJ_SESSION_NAME"):
        return "not in zellij"
    cwd = Path(data.get("cwd") or os.getcwd())
    rollout = find_rollout(data)
    if not rollout:
        return "no rollout"
    tokens = context_tokens(rollout)
    task = cwd
    nxt = ticket_dir(cwd) / "NEXT.md"
    st = load_state(task)
    phase, base = st.get("phase", "idle"), st.get("base", 0)

    if phase == "compress":
        if mtime(nxt) > st.get("next_mtime", 0):
            typer(task, [[True, "/new"], [False, WHATS_NEXT]])
            save_state(task, phase="whatsnext", base=0)
            log(f"{task}: NEXT.md written, /new + whats-next")
            return "new"
        save_state(task, phase="idle", base=tokens)
        log(f"{task}: NEXT.md not written in the compress turn, chain stopped")
        return "stopped"

    if phase == "whatsnext":
        # The first turn of the fresh conversation: its size is the new base.
        save_state(task, phase="idle", base=tokens)
        return "done"

    limit = threshold()
    if tokens >= limit and (not base or tokens >= base + STEP):
        out = triage_path(task)
        typer(task, [["triage", str(rollout), str(out)],
                     [False, COMPRESS.format(k=f"{tokens // 1000}k/{limit // 1000}k", triage=out)]])
        save_state(task, phase="compress", base=base, next_mtime=mtime(nxt))
        log(f"{task}: context {tokens} >= {limit}, compress")
        return "compress"
    return "idle"


def hook_main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    try:
        on_stop(data)
    except Exception as exc:  # a hook must never break the session
        log(f"codex-stop failed: {exc!r}")
