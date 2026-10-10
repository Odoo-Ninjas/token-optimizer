import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codex_chain  # noqa: E402
import token_optimizer  # noqa: E402


class Chain(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.tmp), "ZELLIJ_SESSION_NAME": "s"})
        self.env.start()
        self.task = self.tmp / "work" / "t"
        self.task.mkdir(parents=True)
        (self.task / "TASK.md").write_text("x")
        self.typed = []
        self.data = {"cwd": str(self.task), "transcript_path": str(self.rollout(200_000))}

    def tearDown(self):
        self.env.stop()

    def rollout(self, tokens):
        p = self.tmp / f"rollout-{tokens}.jsonl"
        ev = {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": tokens - 10, "total_tokens": tokens}}}}
        p.write_text(json.dumps({"type": "session_meta"}) + "\n" + json.dumps(ev) + "\n")
        return p

    def stop(self):
        return codex_chain.on_stop(self.data, lambda d, steps: self.typed.append(steps))

    def test_below_threshold_does_nothing(self):
        self.data["transcript_path"] = str(self.rollout(100_000))
        self.assertEqual(self.stop(), "idle")
        self.assertEqual(self.typed, [])

    def test_full_chain(self):
        self.assertEqual(self.stop(), "compress")
        triage, prompt = self.typed[0]
        self.assertEqual(triage[:2], ["triage", self.data["transcript_path"]])
        self.assertIn("compress", prompt[1])
        self.assertIn(triage[2], prompt[1])
        (self.task / "NEXT.md").write_text("# Next\n")
        os.utime(self.task / "NEXT.md", (9e9, 9e9))
        self.assertEqual(self.stop(), "new")
        self.assertEqual(self.typed[1][0], [True, "/new"])
        self.assertIn("whats-next", self.typed[1][1][1])
        self.data["transcript_path"] = str(self.rollout(30_000))
        self.assertEqual(self.stop(), "done")
        self.assertEqual(self.stop(), "idle")

    def test_no_next_written_waits_for_growth(self):
        self.stop()
        self.assertEqual(self.stop(), "stopped")
        self.assertEqual(self.stop(), "idle")
        self.data["transcript_path"] = str(self.rollout(260_000))
        self.assertEqual(self.stop(), "compress")

    def test_outside_zellij(self):
        del os.environ["ZELLIJ_SESSION_NAME"]
        self.assertEqual(self.stop(), "not in zellij")


class Triage(unittest.TestCase):
    def test_table_or_failure_lands_in_file(self):
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "t.triage.md"
        ok = mock.Mock(returncode=0, stdout="# Context triage\n\nDroppable: ~1k of ~2k tokens.\n", stderr="")
        with mock.patch.dict(os.environ, {"HOME": str(tmp)}), mock.patch("subprocess.run", return_value=ok):
            codex_chain.run_triage("r.jsonl", str(out))
        self.assertIn("Droppable", out.read_text())
        bad = mock.Mock(returncode=2, stdout="Laya not reachable or failed (x): refused\n", stderr="")
        with mock.patch.dict(os.environ, {"HOME": str(tmp)}), mock.patch("subprocess.run", return_value=bad):
            codex_chain.run_triage("r.jsonl", str(out))
        self.assertIn("fehlgeschlagen", out.read_text())
        self.assertIn("refused", out.read_text())


class HookBlock(unittest.TestCase):
    def test_replaced_not_duplicated(self):
        cfg = 'model = "x"\n[hooks.state]\n'
        once = token_optimizer.with_codex_hook(cfg)
        self.assertEqual(token_optimizer.with_codex_hook(once), once)
        cmd = tomllib.loads(once)["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn("codex-stop", cmd)


if __name__ == "__main__":
    unittest.main()
