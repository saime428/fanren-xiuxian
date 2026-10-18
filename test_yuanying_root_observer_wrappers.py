import json
import re
import unittest
from pathlib import Path

from test_account_username_mapping import EXPECTED_ACCOUNT_USERNAMES


class YuanyingRootObserverWrapperTests(unittest.TestCase):
    def _active_yuanying_return_observer_accounts(self) -> set[str]:
        payload = json.loads(Path("config/active_flows.json").read_text(encoding="utf-8"))
        accounts: set[str] = set()
        for item in payload.get("flows", []):
            if not isinstance(item, dict):
                continue
            if item.get("flow") != "yuanyingchuqiao" or not item.get("enabled", True):
                continue
            kinds = set(item.get("kinds") or [])
            if "return_observer" in kinds:
                accounts.add(str(item.get("account") or ""))
        return accounts

    def test_all_active_yuanying_observers_allow_root_topic_return_events(self) -> None:
        scripts_by_account: dict[str, Path] = {}
        for script in sorted(Path("scripts").glob("run_yuanyingchuqiao_return_observer_tg*.sh")):
            match = re.search(r"(tg\d+)", script.name)
            self.assertIsNotNone(match, f"Cannot infer account from {script}")
            account = match.group(1)
            scripts_by_account[account] = script

            with self.subTest(script=str(script)):
                text = script.read_text(encoding="utf-8")
                expected_username = EXPECTED_ACCOUNT_USERNAMES.get(account)
                self.assertIsNotNone(expected_username, f"No known username mapping for {account}")
                markers = "元神归窍总结|你的元婴在虚空中神游八小时"
                self.assertIn(f"export TG_RETURN_MARKER_TEXT='{markers}'", text)
                self.assertIn("export TG_RETURN_ALLOW_ROOT_TOPIC_EVENTS='1'", text)
                self.assertIn(f"export TG_TRIGGER_TARGET_USERNAME='{expected_username}'", text)
                self.assertIn("export TG_OBSERVER_KICK_RESET_PENDING='1'", text)

        active_accounts = self._active_yuanying_return_observer_accounts()
        self.assertTrue(active_accounts, "No active yuanyingchuqiao return observers configured")
        self.assertEqual(
            active_accounts,
            set(scripts_by_account),
            "Every active yuanyingchuqiao return_observer account must have exactly one root-enabled observer wrapper",
        )


if __name__ == "__main__":
    unittest.main()
