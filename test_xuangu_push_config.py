import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class XuanguPushConfigTests(unittest.TestCase):
    def test_shared_runner_uses_push_with_thirty_second_reconciliation(self) -> None:
        shared = (ROOT / "scripts" / "lib" / "run_xuangu_zuoda.sh").read_text(encoding="utf-8")
        self.assertIn('TG_AUTO_REPLY_RECONCILE_SECONDS="${TG_AUTO_REPLY_RECONCILE_SECONDS:-30}"', shared)
        self.assertIn('TG_LOG_IDLE="${TG_LOG_IDLE:-0}"', shared)
        self.assertNotIn("TG_AUTO_REPLY_POLL_SECONDS", shared)


if __name__ == "__main__":
    unittest.main()