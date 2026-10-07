import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import token_optimizer as to  # noqa: E402


def usage_line(tokens: int, sidechain: bool = False) -> str:
    return json.dumps({"type": "assistant", "isSidechain": sidechain,
                       "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": tokens - 10,
                                             "cache_creation_input_tokens": 0}}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.tmp), "ZELLIJ_SESSION_NAME": "s1",
                                                "ZELLIJ_PANE_ID": "3"})
        self.env.start()
        self.typed = []
        self.spawn = mock.patch.object(to, "spawn_typer", lambda p, text, submit=True: self.typed.append(text if submit else ("put", text)))
        self.spawn.start()
        self.work = self.tmp / "ai" / "work" / "T-1"
        self.work.mkdir(parents=True)
        (self.work / "TASK.md").write_text("task")
        self.transcript = self.tmp / "t.jsonl"

    def tearDown(self):
        self.spawn.stop()
        self.env.stop()

    def write_transcript(self, *lines):
        self.transcript.write_text("\n".join(lines) + "\n")

    def hook(self, tokens, sid="a"):
        self.write_transcript(usage_line(tokens))
        return {"session_id": sid, "transcript_path": str(self.transcript), "cwd": str(self.work)}


class ContextTokens(Base):
    def test_last_main_turn_counts(self):
        self.write_transcript(usage_line(1000), usage_line(5000), usage_line(99999, sidechain=True))
        self.assertEqual(to.context_tokens(str(self.transcript)), 5000)

    def test_pending_until_answer_written(self):
        user = json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})
        self.write_transcript(usage_line(5000), user)
        self.assertEqual(to.scan_context(str(self.transcript)), (5000, True))
        self.write_transcript(usage_line(5000), user, usage_line(7000))
        self.assertEqual(to.scan_context(str(self.transcript)), (7000, False))
        # gives up after the wait instead of blocking the hook
        self.write_transcript(user)
        self.assertEqual(to.settled_context_tokens(str(self.transcript), wait=0.3), 0)

    def test_compact_boundary_resets(self):
        self.write_transcript(usage_line(400000), json.dumps(
            {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"postTokens": 20000}}))
        self.assertEqual(to.context_tokens(str(self.transcript)), 20000)


class InputLine(unittest.TestCase):
    def test_last_prompt_line_wins_over_menu(self):
        screen = ("● Fertig.\n  → Jetzt /clear eingeben.\n\n  ❯ /clear      Start a new session\n"
                  "    /compress    Sitzung verdichten\n────────\n❯ /clear\n────────\n")
        self.assertEqual(to.input_line(screen), "/clear")

    def test_dim_suggestion_is_not_input(self):
        real = ('\x1b[38;2;102;102;102m\x1b[48;2;240;240;240m\x1b[22m❯\x1b[39m \x1b[38;2;0;0;0m/compress\x1b[39m\n'
                '\x1b[m❯\xa0\x1b[39m\x1b[49m\x1b[22m\x1b[24m\x1b[2m\x1b[23m/clear\n')
        self.assertEqual(to.input_line(real), "")
        self.assertEqual(to.input_line("\x1b[m❯\xa0\x1b[22m/clear\n"), "/clear")

    def test_suggestion_is_the_dim_part(self):
        screen = ('\x1b[m❯\xa0\x1b[22m\x1b[2mmach weiter mit den Tests\n')
        self.assertEqual(to.suggestion(screen), "mach weiter mit den Tests")
        self.assertEqual(to.suggestion('\x1b[m❯\xa0\x1b[2mTry "fix lint errors"\n'), "")

    def test_empty_input(self):
        self.assertEqual(to.input_line("● hi\n────\n❯ \n────\n"), "")


class Chain(Base):
    def test_below_threshold_does_nothing(self):
        self.assertIsNone(to.on_stop(self.hook(to.THRESHOLD - 1)))
        self.assertEqual(self.typed, [])

    def test_full_chain(self):
        out = to.on_stop(self.hook(to.THRESHOLD))
        self.assertIn("/compress", out["systemMessage"])
        self.assertEqual(self.typed, ["/compress"])
        # the typer parks Claude Code's suggestion in the state before /compress
        st = to.load_state(to.pane())
        st["suggestion"] = "mach weiter"
        to.state_path(to.pane()).write_text(json.dumps(st))

        # /compress turn ends with a fresh NEXT.md -> /clear
        time.sleep(0.01)
        (self.work / "NEXT.md").write_text("# Next\n1. [ ] weiter\n")
        to.on_stop(self.hook(to.THRESHOLD + 5000))
        self.assertEqual(self.typed, ["/compress", "/clear"])

        # new session after /clear -> context + /whats-next, chain done
        out = to.on_session_start({"source": "clear", "session_id": "b", "cwd": str(self.work)})
        self.assertIn("weiter", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.typed, ["/compress", "/clear", "/whats-next"])

        # the fresh session starts above the threshold: no loop
        to.on_stop(self.hook(to.THRESHOLD + 10000, sid="b"))
        self.assertEqual(self.typed[-1], ("put", "mach weiter"))
        to.on_stop(self.hook(to.THRESHOLD + 20000, sid="b"))
        self.assertEqual(len(self.typed), 4)
        # it fires again once the session has grown by STEP
        to.on_stop(self.hook(to.THRESHOLD + 10000 + to.STEP, sid="b"))
        self.assertEqual(self.typed[-1], "/compress")

    def test_no_clear_without_fresh_next_md(self):
        (self.work / "NEXT.md").write_text("old")
        os.utime(self.work / "NEXT.md", (time.time() - 3600,) * 2)
        to.on_stop(self.hook(to.THRESHOLD))
        out = to.on_stop(self.hook(to.THRESHOLD))
        self.assertEqual(self.typed, ["/compress"])
        self.assertIn("nicht geschrieben", out["systemMessage"])
        # failed chain does not retry in the same session
        to.on_stop(self.hook(to.THRESHOLD))
        self.assertEqual(self.typed, ["/compress"])

    def test_unchanged_next_md_ok_when_compress_says_done(self):
        (self.work / "NEXT.md").write_text("old")
        os.utime(self.work / "NEXT.md", (time.time() - 3600,) * 2)
        to.on_stop(self.hook(to.THRESHOLD))
        answer = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Nichts Neues.\n\n→ Jetzt /clear eingeben."}]}})
        self.write_transcript(usage_line(to.THRESHOLD), answer)
        to.on_stop({"session_id": "a", "transcript_path": str(self.transcript), "cwd": str(self.work)})
        self.assertEqual(self.typed, ["/compress", "/clear"])

    def test_outside_zellij_nothing_is_typed(self):
        with mock.patch.dict(os.environ, {"ZELLIJ_PANE_ID": ""}):
            self.assertIsNone(to.on_stop(self.hook(to.THRESHOLD)))
        self.assertEqual(self.typed, [])

    def test_stale_state_is_dropped(self):
        to.save_state(to.pane(), phase="clear", session_id="a", at=time.time() - to.STALE - 1)
        self.assertIsNone(to.load_state(to.pane()))

    def test_session_start_other_source_ignored(self):
        self.assertIsNone(to.on_session_start({"source": "startup", "cwd": str(self.work)}))


class Prompt(Base):
    def setUp(self):
        super().setUp()
        os.environ["TOKEN_OPTIMIZER_TRIAGE_OFF"] = "1"

    def tearDown(self):
        os.environ.pop("TOKEN_OPTIMIZER_TRIAGE_OFF", None)
        super().tearDown()

    def test_reminds_once_per_step(self):
        data = {**self.hook(to.THRESHOLD), "prompt": "mach weiter"}
        self.assertIn("CONTEXT GUARD", to.on_prompt(data)["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(to.on_prompt(data))

    def test_skips_own_commands_and_running_chain(self):
        self.assertIsNone(to.on_prompt({**self.hook(to.THRESHOLD), "prompt": "/compress"}))
        to.save_state(to.pane(), phase="compress", session_id="a")
        self.assertIsNone(to.on_prompt({**self.hook(to.THRESHOLD), "prompt": "hallo"}))


class Install(Base):
    def test_replaces_legacy_hooks_and_links_skills(self):
        claude = self.tmp / ".claude"
        (claude / "hooks").mkdir(parents=True)
        (claude / "skills" / "compress").mkdir(parents=True)
        (claude / "hooks" / "context-guard.py").write_text("old")
        (claude / "settings.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "other"}]},
            {"hooks": [{"type": "command", "command": 'python3 "$HOME/.claude/hooks/context-guard.py"'}]},
        ]}}))
        with mock.patch("builtins.print"):
            to.install()
            to.install()  # idempotent
        hooks = json.loads((claude / "settings.json").read_text())["hooks"]
        cmds = [h["command"] for g in hooks["UserPromptSubmit"] for h in g["hooks"]]
        self.assertEqual(cmds[0], "other")
        self.assertEqual(sum("token_optimizer.py" in c for c in cmds), 1)
        self.assertFalse(any("context-guard" in c for c in cmds))
        self.assertEqual(len(hooks["Stop"]), 1)
        self.assertEqual(hooks["SessionStart"][0]["matcher"], "clear")
        self.assertEqual((claude / "skills" / "compress").resolve(), to.ROOT / "skills" / "compress")
        self.assertFalse((claude / "hooks" / "context-guard.py").exists())


if __name__ == "__main__":
    unittest.main()
