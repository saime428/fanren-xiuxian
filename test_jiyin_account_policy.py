import json
import plistlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ACCOUNTS = {
    "tg9100000001": ("demo_player_01", ".献上魂魄", "tg9100000001"),
    "tg9100000002": ("demo_player_02", ".收敛气息", "tg9100000002"),
    "tg9100000003": ("demo_player_03", ".收敛气息", "tg9100000003"),
    "tg9100000005": ("demo_player_05", ".收敛气息", "tg9100000005"),
    "tg9100000004": ("demo_player_04", ".收敛气息", "tg9100000004"),
    "tg9100000006": ("demo_player_06", ".收敛气息", "tg9100000007"),
}


class JiyinAccountPolicyTests(unittest.TestCase):
    def test_each_account_has_exact_attributed_command(self):
        for account, (username, command, source) in ACCOUNTS.items():
            wrapper = (ROOT / "scripts" / f"send_jiyin_choice_{account}.sh").read_text(encoding="utf-8")
            # The shared runner sources the credential env and sets the business
            # account itself; the wrapper only names them.
            self.assertIn(f"exec ./scripts/run_jiyin_choice_account.sh '{account}' '{source}' '{username}' '{command}'",
                          wrapper)
            shared = (ROOT / "scripts" / "run_jiyin_choice_account.sh").read_text(encoding="utf-8")
            self.assertIn('source "./config/accounts/${source_slug}.env"\nexport XIU_ACCOUNT_SLUG="$account"', shared)
            self.assertIn("TG_TRIGGER_MARKER_TEXT='回复本消息|.献上魂魄|.收敛气息'", shared)
            self.assertIn("TG_REPLY_TO_EVENT='1'", shared)
            self.assertIn("TG_AUTO_REPLY_RECONCILE_SECONDS='10'", shared)
            self.assertIn("TG_TRIGGER_MAX_EVENT_AGE_SECONDS='10800'", shared)
            self.assertIn("TG_LOG_IDLE='1'", shared)
            self.assertNotIn("TG_AUTO_REPLY_POLL_SECONDS='1'", shared)

    def test_each_account_has_loaded_persistent_sender_plist(self):
        for account in ACCOUNTS:
            payload = plistlib.loads((ROOT / "scripts" / f"ai.hermes.xiuxian.{account}.jiyin_choice.send.plist").read_bytes())
            self.assertEqual(f"ai.hermes.xiuxian.{account}.jiyin_choice.send", payload["Label"])
            self.assertTrue(payload["RunAtLoad"])
            self.assertNotIn("StartInterval", payload)

    def test_registry_contains_all_account_attributed_flows(self):
        payload = json.loads((ROOT / "config" / "active_flows.json").read_text(encoding="utf-8"))
        rows = {(row.get("account"), row.get("flow")): row for row in payload["flows"]}
        for account in ACCOUNTS:
            self.assertTrue(rows[(account, "jiyin_choice")]["enabled"])
            self.assertIn("send", rows[(account, "jiyin_choice")]["kinds"])


if __name__ == "__main__":
    unittest.main()
