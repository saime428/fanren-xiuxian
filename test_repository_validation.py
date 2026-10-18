import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import hermes_shadow_check as check


class RepositoryValidationTests(unittest.TestCase):
    def test_live_ignored_state_is_normal_but_tracked_runtime_files_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text(".env\nstate/\n")
            (root / ".env").write_text("LOCAL_FIXTURE=1\n")
            (root / "state").mkdir()
            (root / "state/result.json").write_text("{}")
            (root / "app.py").write_text("value = 1\n")
            self.assertEqual(check.tracked_runtime_files(root), [])
            self.assertEqual({p.name for p in check.source_files(root)}, {".gitignore", "app.py"})
            subprocess.run(["git", "-C", str(root), "add", "-f", ".env", "state/result.json"], check=True)
            self.assertEqual(check.tracked_runtime_files(root), [".env", "state/result.json"])
            accounts = root / "config/accounts"
            accounts.mkdir(parents=True)
            for name in ("account.env.example", "tg1.env.bak", "tg1.env.orig", ".tg1.env.swp"):
                (accounts / name).write_text("export TG_API_ID=''\n")
            subprocess.run(["git", "-C", str(root), "add", "-f", "config/accounts"], check=True)
            self.assertEqual(check.tracked_runtime_files(root), [
                ".env", "config/accounts/.tg1.env.swp", "config/accounts/tg1.env.bak",
                "config/accounts/tg1.env.orig", "state/result.json"])

    def test_legacy_documentation_is_allowed_but_live_scheduler_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "README.md").write_text("Historical path: /Users/operator/.openclaw/workspace/xiuxian\n")
            output = root / "state/report.json"
            with patch("sys.argv", ["check", "--root", str(root), "--output", str(output)]):
                self.assertEqual(check.main(), 0)
                (root / "bad.plist").write_bytes(plistlib.dumps({
                    "Label": "ai.hermes.xiuxian.fixture", "ProgramArguments": [
                        "/Users/operator/.openclaw/workspace/xiuxian/send.py"]}))
                self.assertEqual(check.main(), 2)
            self.assertEqual(json.loads(output.read_text())["legacy_runtime_files"], ["bad.plist"])


if __name__ == "__main__":
    unittest.main()
