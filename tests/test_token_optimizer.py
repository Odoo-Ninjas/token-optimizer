import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import token_optimizer as to  # noqa: E402

# The chain itself is tested in plugin/tests (claude plugin test plugin).


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.tmp)})
        self.env.start()
        self.calls = []

        def fake_claude(*args):
            self.calls.append(args)
            return mock.Mock(returncode=0, stdout="ok", stderr="")
        self.cli = mock.patch.object(to, "claude", fake_claude)
        self.cli.start()
        self.which = mock.patch.object(to.shutil, "which", lambda name: "/usr/bin/claude")
        self.which.start()

    def tearDown(self):
        self.which.stop()
        self.cli.stop()
        self.env.stop()

    def test_removes_old_hooks_links_skills_installs_plugin(self):
        claude = self.tmp / ".claude"
        (claude / "hooks").mkdir(parents=True)
        (claude / "skills" / "compress").mkdir(parents=True)
        (claude / "hooks" / "context-guard.py").write_text("old")
        (claude / "settings.json").write_text(json.dumps({"hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "other"}]},
                {"hooks": [{"type": "command", "command": 'python3 "$HOME/.claude/hooks/context-guard.py"'}]},
                {"hooks": [{"type": "command", "command": 'python3 "/x/token_optimizer.py" prompt'}]},
            ],
            "Stop": [{"hooks": [{"type": "command", "command": 'python3 "/x/token_optimizer.py" stop'}]}],
        }}))
        with mock.patch("builtins.print"):
            to.install()
            to.install()  # idempotent

        hooks = json.loads((claude / "settings.json").read_text())["hooks"]
        self.assertEqual(hooks, {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other"}]}]})
        self.assertEqual((claude / "skills" / "compress").resolve(), to.ROOT / "skills" / "compress")
        self.assertFalse((claude / "hooks" / "context-guard.py").exists())
        self.assertIn(("marketplace", "add", str(to.ROOT)), self.calls)
        self.assertIn(("install", to.PLUGIN, "--scope", "user"), self.calls)

    def test_existing_marketplace_is_fine(self):
        def fake_claude(*args):
            self.calls.append(args)
            if args[0] == "marketplace":
                return mock.Mock(returncode=1, stdout="", stderr="Marketplace 'token-optimizer' is already installed")
            return mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(to, "claude", fake_claude), mock.patch("builtins.print"):
            to.install()
        self.assertEqual(self.calls[-1][0], "install")


class LegacyHooks(unittest.TestCase):
    def test_old_hook_commands_do_nothing(self):
        for name in to.LEGACY_COMMANDS:
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["token_optimizer.py", name]), \
                    mock.patch.object(sys, "stdin", io.StringIO('{"session_id": "a"}')), \
                    mock.patch.object(sys, "stdout", out):
                to.main()
            self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
