#!/usr/bin/env python3
"""token-optimizer: keep Claude Code sessions below a context budget.

Once the context of a session reaches THRESHOLD tokens, the session is handed
over to a fresh one without losing anything that matters:

    /compress   -> the session writes its state into NEXT.md
    /clear      -> fresh session; the SessionStart hook injects NEXT.md
    /whats-next -> the new session lists the open items

In zellij all three commands are typed into the pane the session runs in
(ZELLIJ_SESSION_NAME / ZELLIJ_PANE_ID, inherited by the hooks). Outside
zellij nothing can be typed; there the prompt hook falls back to asking
Claude to run /compress before it handles the next prompt.

Hook entry points (registered by `install`):

    stop           Stop hook: starts the chain and advances it after /compress
    session-start  SessionStart hook (matcher "clear"): NEXT.md as context,
                   then /whats-next
    prompt         UserPromptSubmit hook: fallback reminder plus Jev triage

Context size = input + cache_read + cache_creation of the last main-thread
assistant turn; a later compact_boundary resets it to its postTokens.

Env: TOKEN_OPTIMIZER_THRESHOLD (350000), TOKEN_OPTIMIZER_STEP (50000, how
often the fallback reminder repeats above the threshold),
TOKEN_OPTIMIZER_OFF=1 to disable everything, TOKEN_OPTIMIZER_TRIAGE_OFF=1 for
the reminder without Jev, TOKEN_OPTIMIZER_TRIAGE_TIMEOUT (120) seconds.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRIAGE = ROOT / "hooks" / "jev-context-triage.py"
SKILLS = ("compress", "whats-next")

THRESHOLD = int(os.environ.get("TOKEN_OPTIMIZER_THRESHOLD", "350000"))
STEP = int(os.environ.get("TOKEN_OPTIMIZER_STEP", "50000"))
TRIAGE_TIMEOUT = int(os.environ.get("TOKEN_OPTIMIZER_TRIAGE_TIMEOUT", "120"))

# A chain step that has not advanced after this long is abandoned; the next
# Stop starts over instead of waiting forever.
STALE = 30 * 60
# Pause before typing: the hook fires while Claude Code is still finishing its
# turn, the input box is ready a moment later.
TYPE_DELAY = 2.0
# How long to wait for the typed command to show up on screen before Enter.
TYPE_SETTLE = 4.0
NEXT_MAX_CHARS = 12000
# Closing line of the compress skill (skills/compress/SKILL.md, section 3).
COMPRESS_DONE = "Jetzt /clear eingeben"
# Stop hook: how long to wait for the final answer to reach the transcript.
STOP_WAIT = 5.0


def home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def data_dir() -> Path:
    d = home() / ".claude" / "token-optimizer"
    (d / "state").mkdir(parents=True, exist_ok=True)
    return d


def log(msg: str) -> None:
    try:
        with open(data_dir() / "log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


# --- Measuring ---------------------------------------------------------------

def scan_context(transcript: str) -> tuple[int, bool]:
    """Context size and whether a user line came after the last measured
    answer (i.e. the answer to it is not in the transcript yet)."""
    tokens, pending = 0, False
    with open(transcript, encoding="utf-8") as fh:
        for line in fh:
            if '"usage"' not in line and "compact_boundary" not in line and '"user"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("isSidechain"):
                continue
            if o.get("type") == "user":
                pending = True
            elif o.get("type") == "system" and o.get("subtype") == "compact_boundary":
                tokens = (o.get("compactMetadata") or {}).get("postTokens") or 0
            elif o.get("type") == "assistant":
                u = (o.get("message") or {}).get("usage") or {}
                total = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
                    + (u.get("cache_creation_input_tokens") or 0)
                if total:
                    tokens, pending = total, False
    return tokens, pending


def context_tokens(transcript: str) -> int:
    return scan_context(transcript)[0]


def settled_context_tokens(transcript: str, wait: float = STOP_WAIT) -> int:
    """Context size at the end of a turn. Claude Code fires Stop around the
    same moment it appends the final answer (measured: the answer line can
    land after the hook has read the file), so wait until it is there."""
    deadline = time.time() + wait
    while True:
        tokens, pending = scan_context(transcript)
        if not pending or time.time() > deadline:
            return tokens
        time.sleep(0.2)


def last_answer(data: dict) -> str:
    """Text of the turn's final answer. Newer Claude Code passes it to the
    Stop hook; otherwise read the last main-thread answer from the transcript."""
    if data.get("last_assistant_message"):
        return str(data["last_assistant_message"])
    text = ""
    try:
        with open(data.get("transcript_path") or "", encoding="utf-8") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") != "assistant" or o.get("isSidechain"):
                    continue
                parts = [c.get("text", "") for c in (o.get("message") or {}).get("content") or []
                         if isinstance(c, dict) and c.get("type") == "text"]
                if parts:
                    text = "\n".join(parts)
    except OSError:
        pass
    return text


def find_next(cwd: Path) -> Path | None:
    """NEXT.md of the ticket folder: the one holding TASK.md, searched upwards
    from cwd; without one, NEXT.md sits in cwd itself. Same lookup as /compress."""
    for d in [cwd, *cwd.parents]:
        if (d / "TASK.md").is_file():
            p = d / "NEXT.md"
            return p if p.is_file() else None
        if d == home():
            break
    p = cwd / "NEXT.md"
    return p if p.is_file() else None


# --- Chain state -------------------------------------------------------------
#
# Keyed by zellij pane, not by Claude session: /clear starts a new session id,
# the pane stays the same.

def pane() -> tuple[str, str] | None:
    session = os.environ.get("ZELLIJ_SESSION_NAME")
    pane_id = os.environ.get("ZELLIJ_PANE_ID")
    if not session or not pane_id or not pane_id.isdigit():
        return None
    return session, pane_id


def state_path(p: tuple[str, str]) -> Path:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{p[0]}-{p[1]}")
    return data_dir() / "state" / f"chain-{name}.json"


def load_state(p) -> dict | None:
    try:
        st = json.loads(state_path(p).read_text())
    except (OSError, ValueError):
        return None
    if time.time() - st.get("at", 0) > STALE:
        state_path(p).unlink(missing_ok=True)
        return None
    return st


def save_state(p, **st) -> None:
    st.setdefault("at", time.time())
    state_path(p).write_text(json.dumps(st))


def drop_state(p) -> None:
    state_path(p).unlink(missing_ok=True)


# --- Typing into the pane ----------------------------------------------------

def spawn_typer(p, text: str) -> None:
    """Type `text` from a detached process: the hook has to return first,
    otherwise Claude Code is still busy with it when the keys arrive."""
    with open(data_dir() / "log", "a") as out:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "type", p[0], p[1], text],
            stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
        )


def zellij(session: str, *args: str) -> str:
    out = subprocess.run(["zellij", "--session", session, "action", *args],
                         capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"zellij {args[0]} failed")
    return out.stdout


ANSI = re.compile(r"\x1b\[([0-9;]*)m|\x1b\[[0-9;?]*[A-Za-z]")


def visible_text(line: str) -> str:
    """Plain text of an ANSI line without the dimmed parts: Claude Code shows
    its prompt suggestion as dim ghost text in the input box, which is not
    typed input."""
    out, dim, pos = [], False, 0
    for m in ANSI.finditer(line):
        if not dim:
            out.append(line[pos:m.start()])
        pos = m.end()
        if m.group(1) is None:
            continue
        codes = m.group(1).split(";") if m.group(1) else ["0"]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c in ("38", "48", "58"):  # colour arguments are not attributes
                i += 5 if codes[i + 1:i + 2] == ["2"] else 3
                continue
            if c == "2":
                dim = True
            elif c in ("0", "22"):
                dim = False
            i += 1
    if not dim:
        out.append(line[pos:])
    return "".join(out)


def input_line(screen: str) -> str:
    """Content of Claude Code's input box (screen dumped with ANSI codes): the
    LAST line starting with the prompt sign. Counting occurrences of the
    command on screen does not work: the slash-command menu opens while typing
    and pushes older mentions (e.g. "→ Jetzt /clear eingeben.") out of the
    viewport. The menu marks its selected entry with the same sign, but always
    above the input box."""
    last = ""
    for line in screen.splitlines():
        s = visible_text(line).replace("\xa0", " ").strip()
        if s[:1] in ("❯", ">"):
            last = s[1:].strip()
    return last


def type_command(session: str, pane_id: str, text: str, delay: float = TYPE_DELAY) -> bool:
    """Type a slash command and press Enter, but only once the command is
    visibly in the input box. The pane id goes in as the bare number:
    `-p terminal_N` is accepted by write-chars and silently types nowhere."""
    time.sleep(delay)
    try:
        pending = input_line(zellij(session, "dump-screen", "-a", "-p", pane_id))
        if pending:
            # Someone is typing (or a draft is parked): appending would mangle both.
            log(f"{session}/{pane_id}: input box not empty, {text} not typed")
            return False
        zellij(session, "write-chars", "-p", pane_id, "--", text)
        deadline = time.time() + TYPE_SETTLE
        while input_line(zellij(session, "dump-screen", "-a", "-p", pane_id)) != text:
            if time.time() > deadline:
                log(f"{session}/{pane_id}: {text} not visible after typing, no Enter sent")
                return False
            time.sleep(0.2)
        zellij(session, "write", "-p", pane_id, "13")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        log(f"{session}/{pane_id}: typing {text} failed: {exc}")
        return False
    log(f"{session}/{pane_id}: typed {text}")
    return True


# --- Hooks -------------------------------------------------------------------

def on_stop(data: dict) -> dict | None:
    p = pane()
    if not p:
        log(f"stop {data.get('session_id')}: not in zellij, nothing to type")
        return None  # outside zellij: the prompt hook reminds instead
    st = load_state(p)
    sid = data.get("session_id")

    if st and st["phase"] == "compress" and st.get("session_id") == sid:
        # This Stop ends the /compress turn: only clear if NEXT.md was written,
        # otherwise the fresh session would have nothing to continue from.
        nxt = find_next(Path(data.get("cwd") or os.getcwd()))
        fresh = nxt and nxt.stat().st_mtime >= st["at"]
        # An unchanged NEXT.md is fine when /compress says it is done: it only
        # rewrites the file when there is something new to record.
        if nxt and (fresh or COMPRESS_DONE in last_answer(data)):
            save_state(p, phase="clear", session_id=sid, next_md=str(nxt))
            spawn_typer(p, "/clear")
            return None
        save_state(p, phase="failed", session_id=sid)
        log(f"{p[0]}/{p[1]}: /compress ended without a fresh NEXT.md, no /clear")
        return {"systemMessage": "token-optimizer: NEXT.md wurde nicht geschrieben – kein automatisches /clear."}
    if st and st.get("session_id") == sid:
        return None  # chain already running or failed for this session

    transcript = data.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        return None
    tokens = settled_context_tokens(transcript)
    log(f"{p[0]}/{p[1]}: stop, context {tokens // 1000}k, threshold {THRESHOLD // 1000}k")
    if tokens < THRESHOLD:
        return None
    save_state(p, phase="compress", session_id=sid, tokens=tokens)
    spawn_typer(p, "/compress")
    return {"systemMessage": f"Kontext ~{tokens // 1000}k ≥ {THRESHOLD // 1000}k – "
                             "token-optimizer tippt /compress, danach /clear und /whats-next"}


def on_session_start(data: dict) -> dict | None:
    if data.get("source", "clear") != "clear":
        return None
    p = pane()
    st = load_state(p) if p else None
    if st and st["phase"] == "clear":
        drop_state(p)
        spawn_typer(p, "/whats-next")

    path = find_next(Path(data.get("cwd") or os.getcwd()))
    if not path:
        return None
    text = path.read_text(errors="replace")
    if len(text) > NEXT_MAX_CHARS:
        text = text[:NEXT_MAX_CHARS] + f"\n\n[... gekürzt, vollständig in {path}]"
    context = (
        f"Stand aus der vorigen Sitzung ({path}), geschrieben von /compress. "
        "Kurz (wie /whats-next) die offenen Punkte nennen, wenn Marc fragt "
        "oder mit /go weitermacht:\n\n" + text
    )
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}


def run_triage(transcript: str) -> str | None:
    """Run the Jev triage and return its table, or None if it did not work.

    Never raises: the prompt must go through even if the local Laya server is
    down or uv is missing.
    """
    if os.environ.get("TOKEN_OPTIMIZER_TRIAGE_OFF") == "1" or not TRIAGE.exists():
        return None
    try:
        out = subprocess.run(
            ["uv", "run", "--script", str(TRIAGE), transcript],
            capture_output=True, text=True, timeout=TRIAGE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    table = (out.stdout or "").strip()
    if out.returncode != 0 or not table:
        return None
    # The script also prints a table when it did NOT ask Jev; those rows carry
    # "-" instead of a verdict. Look for an actual verdict in the first column.
    verdicts = {"keep_verbatim", "keep_gist", "drop"}
    judged = any(
        len(parts := line.split("|")) > 2 and parts[1].strip() in verdicts
        for line in table.splitlines() if line.startswith("|")
    )
    return table if judged else None


def on_prompt(data: dict) -> dict | None:
    """Fallback for sessions the chain cannot type into (no zellij) or did
    not reach in time: ask Claude to compress before handling the prompt."""
    prompt = (data.get("prompt") or "").strip()
    if prompt.startswith(("/compress", "/clear", "/whats-next")):
        return None
    p = pane()
    if p and load_state(p):
        return None  # the chain is on it
    transcript = data.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        return None
    tokens = context_tokens(transcript)

    state_file = data_dir() / "state" / f"prompt-{data.get('session_id', 'x')}.json"
    try:
        last = json.loads(state_file.read_text()).get("last_notified", 0)
    except (OSError, ValueError):
        last = 0
    if tokens < THRESHOLD:
        if last:
            state_file.unlink(missing_ok=True)  # compacted/cleared -> re-arm
        return None
    if last and tokens < last + STEP:
        return None
    state_file.write_text(json.dumps({"last_notified": tokens}))

    k = f"{tokens // 1000}k"
    table = run_triage(transcript)
    if table:
        triage = ("The Jev triage below has already been run - do not run it again. "
                  "It rates the large tool outputs of this session: carry "
                  "keep_verbatim items over precisely (paths, lines, commands), "
                  "reduce keep_gist to the facts that matter, leave drop items out.")
    else:
        triage = ("Decide yourself which parts of the session still matter; leave out "
                  "superseded tool output, dead ends and finished work.")
    ctx = (
        f"CONTEXT GUARD: the conversation context is ~{k} tokens "
        f"(threshold {THRESHOLD // 1000}k). Do not start on the user's prompt yet. "
        f"{triage} Then run the `compress` skill to write NEXT.md, and put the user's "
        "current prompt verbatim as open item 1 so nothing gets lost. Finish with "
        "`→ Jetzt /clear eingeben.` Only if the user explicitly says to continue without "
        "compressing, handle the prompt normally. If the user compacts instead of "
        "clearing, the same verdicts apply to what the summary must keep."
    )
    if table:
        ctx += "\n\n" + table
    note = " (Jev-Triage liegt bei)" if table else " (ohne Jev)"
    return {
        "systemMessage": f"Kontext ~{k} ≥ {THRESHOLD // 1000}k – Sitzung wird verdichtet (NEXT.md, dann /clear){note}",
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx},
    }


HOOKS = {"stop": on_stop, "session-start": on_session_start, "prompt": on_prompt}


def run_hook(name: str) -> None:
    if os.environ.get("TOKEN_OPTIMIZER_OFF") == "1":
        return
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    try:
        out = HOOKS[name](data)
    except Exception as exc:  # never break a session because of this hook
        log(f"{name}: {exc!r}")
        print(f"token-optimizer {name}: {exc}", file=sys.stderr)
        return
    if out:
        json.dump(out, sys.stdout)


# --- Install -----------------------------------------------------------------

# Hooks this tool replaces; their entries are removed from settings.json.
LEGACY_HOOKS = ("context-guard.py", "next-after-clear.py")


def hook_cmd(name: str) -> str:
    return f'python3 "{Path(__file__).resolve()}" {name}'


def install() -> None:
    claude = home() / ".claude"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = data_dir() / "backup" / stamp
    backup.mkdir(parents=True, exist_ok=True)

    settings_file = claude / "settings.json"
    settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    if settings_file.exists():
        shutil.copy2(settings_file, backup / "settings.json")
    hooks = settings.setdefault("hooks", {})
    for event, groups in list(hooks.items()):
        kept = []
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", [])
                          if not any(s in h.get("command", "") for s in (*LEGACY_HOOKS, "token_optimizer.py"))]
            if g["hooks"]:
                kept.append(g)
        hooks[event] = kept
    hooks.setdefault("Stop", []).append(
        {"hooks": [{"type": "command", "command": hook_cmd("stop"), "timeout": 10}]})
    hooks.setdefault("SessionStart", []).append(
        {"matcher": "clear", "hooks": [{"type": "command", "command": hook_cmd("session-start"), "timeout": 10}]})
    hooks.setdefault("UserPromptSubmit", []).append(
        {"hooks": [{"type": "command", "command": hook_cmd("prompt"), "timeout": TRIAGE_TIMEOUT + 10}]})
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    print(f"hooks registered in {settings_file}")

    # Skills and the triage script are symlinked, so the repo stays the only copy.
    links = {claude / "skills" / s: ROOT / "skills" / s for s in SKILLS}
    links[claude / "hooks" / "jev-context-triage.py"] = TRIAGE
    for dst, src in links.items():
        if dst.is_symlink() and dst.resolve() == src:
            continue
        if dst.exists() or dst.is_symlink():
            shutil.move(str(dst), backup / dst.name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)
        print(f"{dst} -> {src}")
    for name in LEGACY_HOOKS:
        old = claude / "hooks" / name
        if old.exists():
            shutil.move(str(old), backup / name)
            print(f"{old} moved to backup")
    print(f"backup: {backup}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    if args[0] in HOOKS:
        run_hook(args[0])
    elif args[0] == "type" and len(args) == 4:
        if type_command(*args[1:]):
            sys.exit(0)
        # Nothing was sent: forget the chain so the next Stop starts over
        # instead of waiting for a command that never ran.
        drop_state((args[1], args[2]))
        sys.exit(1)
    elif args[0] == "install":
        install()
    else:
        print(f"unknown command: {' '.join(args)}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
