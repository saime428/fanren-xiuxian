import re
import unittest
from pathlib import Path


EXPECTED_ACCOUNT_USERNAMES = {
    "tg9100000006": "demo_player_06",  # Business identity; credentials still come from tg9100000007.
    "tg9100000001": "demo_player_01",
    "tg9100000005": "demo_player_05",
    "tg9300000001": "AliceI005",
    "tg9100000007": "demo_player_06",
    "tg9200000001": "wuxingweiren",
    "tg9100000004": "demo_player_04",
    "tg9100000002": "demo_player_02",
    "tg9100000003": "demo_player_03",
}


class AccountUsernameMappingTests(unittest.TestCase):
    def test_trigger_wrappers_use_the_username_owned_by_their_account(self) -> None:
        checked = []
        for script in sorted(Path("scripts").glob("*.sh")):
            text = script.read_text(encoding="utf-8", errors="ignore")
            username_match = re.search(r"export TG_TRIGGER_TARGET_USERNAME='([^']+)'", text)
            if username_match is None:
                continue
            account_match = re.search(r"(tg\d+)", script.name)
            if account_match is None:
                # Generic/non-account scripts are outside the per-account ownership rule.
                continue
            account = account_match.group(1)
            expected = EXPECTED_ACCOUNT_USERNAMES.get(account)
            self.assertIsNotNone(expected, f"No canonical username mapping for {account} in {script}")
            actual = username_match.group(1)
            self.assertEqual(
                actual,
                expected,
                f"{script} watches @{actual} but account {account} owns @{expected}; "
                "reports must be handled by the account matching the @ mention",
            )
            checked.append((account, actual, str(script)))
        self.assertTrue(checked, "No per-account trigger wrappers were checked")


if __name__ == "__main__":
    unittest.main()
