#!/usr/bin/env python3
"""token-optimizer: keep Claude Code sessions below a context budget.

Once the context of a session reaches the threshold (250k tokens by default),
the session is handed over to a fresh one without losing anything that
matters:

    /compress   -> the session writes its state into NEXT.md
    /clear      -> fresh session that gets NEXT.md handed over
    /whats-next -> the new session lists the open items

The chain itself runs in the Claude Code mod in plugin/ (function hooks,
plugin/hooks/register.ts). This script only installs everything:

    install    links the skills and the triage script into ~/.claude, removes
               the settings hooks of earlier versions and installs the plugin
               from this folder (marketplace "token-optimizer")

stop, session-start and prompt were the settings hooks of earlier versions.
They are kept as no-ops so a settings.json that still names them does not
break a session before `install` ran again.
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRIAGE = ROOT / "hooks" / "jev-context-triage.py"
SKILLS = ("compress", "whats-next")
PLUGIN = "token-optimizer@token-optimizer"

# Settings hooks this tool used to register, or replaced; install removes them.
LEGACY_HOOKS = ("context-guard.py", "next-after-clear.py", "token_optimizer.py")
LEGACY_COMMANDS = ("stop", "session-start", "prompt")


def home() -> Path:
    return Path.home()


def data_dir() -> Path:
    d = home() / ".claude" / "token-optimizer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def remove_legacy_hooks(settings: dict) -> int:
    """Drop every settings hook that runs one of LEGACY_HOOKS; returns how many."""
    removed = 0
    hooks = settings.get("hooks", {})
    for event, groups in list(hooks.items()):
        kept = []
        for g in groups:
            before = g.get("hooks", [])
            g["hooks"] = [h for h in before if not any(s in h.get("command", "") for s in LEGACY_HOOKS)]
            removed += len(before) - len(g["hooks"])
            if g["hooks"]:
                kept.append(g)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return removed


def claude(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["claude", "plugin", *args], capture_output=True, text=True)


def install_plugin() -> None:
    """Add this folder as marketplace and install the mod from it.

    A marketplace that is a folder is read from the folder itself, so edits
    reach a session with /reload-plugins, no reinstall needed.
    """
    if not shutil.which("claude"):
        print("claude CLI not found: install the plugin by hand with\n"
              f"  claude plugin marketplace add {ROOT}\n  claude plugin install {PLUGIN}")
        return
    added = claude("marketplace", "add", str(ROOT))
    if added.returncode != 0 and "already" not in (added.stdout + added.stderr).lower():
        print(f"marketplace add failed: {(added.stderr or added.stdout).strip()}")
        return
    done = claude("install", PLUGIN, "--scope", "user")
    out = (done.stdout or done.stderr).strip()
    print(out if done.returncode == 0 else f"plugin install failed: {out}")


def install() -> None:
    claude_dir = home() / ".claude"
    backup = data_dir() / "backup" / time.strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True)

    settings_file = claude_dir / "settings.json"
    if settings_file.exists():
        shutil.copy2(settings_file, backup / "settings.json")
        settings = json.loads(settings_file.read_text())
        removed = remove_legacy_hooks(settings)
        if removed:
            settings_file.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
            print(f"{removed} old settings hook(s) removed from {settings_file}")

    # Skills and the triage script are symlinked, so the repo stays the only copy.
    links = {claude_dir / "skills" / s: ROOT / "skills" / s for s in SKILLS}
    links[claude_dir / "hooks" / "jev-context-triage.py"] = TRIAGE
    for dst, src in links.items():
        if dst.is_symlink() and dst.resolve() == src:
            continue
        if dst.exists() or dst.is_symlink():
            shutil.move(str(dst), backup / dst.name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)
        print(f"{dst} -> {src}")
    for name in LEGACY_HOOKS[:2]:
        old = claude_dir / "hooks" / name
        if old.exists():
            shutil.move(str(old), backup / name)
            print(f"{old} moved to backup")

    install_plugin()
    print(f"backup: {backup}")


def main() -> None:
    args = sys.argv[1:]
    if args == ["install"]:
        install()
    elif len(args) == 1 and args[0] in LEGACY_COMMANDS:
        sys.stdin.read()  # an old settings hook: take its input, do nothing
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
