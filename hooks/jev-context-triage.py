# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx"]
# ///
"""Rate the large tool outputs of a Claude Code session with a Jev-style model.

Usage: uv run --script jev-context-triage.py <transcript.jsonl> [--no-jev]

Reads the transcript since the last compaction, turns every tool result above
MIN_CHARS into a segment and asks Jev whether the remaining work still needs
it (keep_verbatim / keep_gist / drop). Prints only metadata (tool, path or
command, size, verdict) — never segment content.

Default backend is Laya running locally (LaunchAgent de.zebroo.laya,
http://127.0.0.1:8781), which speaks Jev's /v1/systemone protocol. Nothing
leaves the machine, so DIN sessions are rated as well. JEV_URL overrides the
endpoint.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

URL = os.environ.get("JEV_URL", "http://127.0.0.1:8781/v1/systemone")
MIN_CHARS = 4000       # ~1k tokens; smaller outputs are not worth judging
MAX_SEGMENTS = 60      # cap Jev calls on huge sessions
BATCH = 6
HEAD, TAIL = 1500, 500
VERDICTS = {
    "keep_verbatim": "The exact content is still needed, e.g. code that is being edited, "
                     "an error that is still being debugged, data still being worked on.",
    "keep_gist": "Only a few facts or conclusions from it still matter; the full text does not.",
    "drop": "Obsolete or irrelevant for the remaining work (superseded, dead end, already done).",
}



def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def describe(inp: dict) -> str:
    for key in ("file_path", "path", "pattern", "url", "query", "description", "command", "prompt"):
        if inp.get(key):
            return str(inp[key]).replace("\n", " ")[:90]
    return json.dumps(inp, ensure_ascii=False)[:90]


def load(transcript: str):
    entries = []
    for line in open(transcript, encoding="utf-8"):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("isSidechain"):
            continue
        if o.get("type") == "system" and o.get("subtype") == "compact_boundary":
            entries = []
        elif o.get("type") in ("user", "assistant"):
            entries.append(o)

    tools, segments, prompts, cwd = {}, [], [], ""
    for o in entries:
        cwd = o.get("cwd") or cwd
        content = (o.get("message") or {}).get("content")
        if o["type"] == "user" and isinstance(content, str):
            if not content.startswith("<"):
                prompts.append(content[:800])
            continue
        for b in content if isinstance(content, list) else []:
            if b.get("type") == "tool_use":
                tools[b["id"]] = (b.get("name", "?"), describe(b.get("input") or {}))
            elif b.get("type") == "text" and o["type"] == "user" and not b.get("text", "").startswith("<"):
                prompts.append(b["text"][:800])
            elif b.get("type") == "tool_result":
                text = text_of(b.get("content"))
                if len(text) >= MIN_CHARS:
                    name, desc = tools.get(b.get("tool_use_id"), ("?", ""))
                    segments.append({"tool": name, "desc": desc, "chars": len(text), "text": text})
    return segments, prompts, cwd


def goal_text(prompts: list[str], cwd: str) -> str:
    parts = ["First requests:", *prompts[:2], "Latest requests:", *prompts[-3:]]
    d = Path(cwd) if cwd else None
    while d and d != d.parent:
        nxt = d / "NEXT.md"
        if nxt.exists():
            parts += ["Open items (NEXT.md):", nxt.read_text(encoding="utf-8")[:2000]]
            break
        d = d.parent
    return "\n".join(parts)


def judge(batch: list[dict], goal: str) -> dict:
    state = {
        "goal": goal,
        "segments": [
            {"tool": s["tool"], "target": s["desc"],
             "excerpt": s["text"][:HEAD] + ("\n[...]\n" + s["text"][-TAIL:] if len(s["text"]) > HEAD + TAIL else "")}
            for s in batch
        ],
    }
    questions = {
        f"s{i}": {
            "type": "choice",
            "instructions": (f"`segments[{i}]` is a tool output from a coding session whose goal "
                             "and open items are in `goal`. The session is about to be condensed. "
                             f"How much of `segments[{i}]` does the remaining work still need?"),
            "criteria": VERDICTS,
        }
        for i in range(len(batch))
    }
    r = httpx.post(URL, json={"state": state, "questions": questions}, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"Laya HTTP {r.status_code}: {r.text[:300]}")
    return r.json()["answers"]


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    segments, prompts, cwd = load(args[0])
    segments.sort(key=lambda s: s["chars"], reverse=True)
    segments = segments[:MAX_SEGMENTS]
    total = sum(s["chars"] for s in segments) // 4
    goal = goal_text(prompts, cwd)

    use_jev = "--no-jev" not in sys.argv and bool(segments)
    if use_jev:
        batches = [segments[i:i + BATCH] for i in range(0, len(segments), BATCH)]
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda b: judge(b, goal), batches))
        for b, answers in zip(batches, results):
            for i, s in enumerate(b):
                a = answers.get(f"s{i}", {})
                s["verdict"], s["conf"] = a.get("choice", "?"), a.get("confidence", 0)
                if s["conf"] < 0.5 and s["verdict"] == "drop":  # unsure -> don't drop
                    s["verdict"] = "keep_gist"

    print(f"# Context triage — {len(segments)} large tool outputs, ~{total // 1000}k tokens")
    if not use_jev:
        print("Jev not used (--no-jev or nothing to judge).")
    print()
    print("| verdict | conf | ~tokens | tool | target |")
    print("|---|---|---|---|---|")
    for s in segments:
        print(f"| {s.get('verdict', '-')} | {s.get('conf', 0):.2f} | {s['chars'] // 4} "
              f"| {s['tool']} | {s['desc'].replace('|', '/')} |")
    if use_jev:
        drop = sum(s["chars"] for s in segments if s.get("verdict") == "drop") // 4
        print(f"\nDroppable: ~{drop // 1000}k of ~{total // 1000}k tokens.")


if __name__ == "__main__":
    main()
