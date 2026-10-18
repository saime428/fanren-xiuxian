import os
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ACCOUNTS = (
    "tg9100000001",
    "tg9100000005",
    "tg9100000004",
    "tg9100000002",
    "tg9100000003",
)
USERNAMES = {
    "tg9100000001": "demo_player_01",
    "tg9100000005": "demo_player_05",
    "tg9100000004": "demo_player_04",
    "tg9100000002": "demo_player_02",
    "tg9100000003": "demo_player_03",
}


class YuanyingWrapperUnificationTests(unittest.TestCase):
    LIB = ROOT / "scripts/lib/run_yuanyingchuqiao_send.sh"

    def test_all_senders_exit_immediately_on_cached_countdown(self):
        text = self.LIB.read_text(encoding="utf-8")
        self.assertIn("still cooling down; skip this run", text)
        self.assertIn("command_confirmation.py --cooldown-remaining status_probe", text)
        self.assertNotIn('sleep "${wait_seconds}"', text)

    def test_all_senders_fail_closed_on_unknown_or_blocked_status(self):
        text = self.LIB.read_text(encoding="utf-8")
        self.assertIn('if [[ "$code" == "11" ]]', text)
        self.assertIn('if [[ "$code" == "12" ]]', text)

    def test_sender_templates_are_identical_after_account_normalization(self):
        normalized = []
        for account in ACCOUNTS:
            text = (ROOT / f"scripts/send_yuanyingchuqiao_{account}.sh").read_text(encoding="utf-8")
            self.assertTrue(text.endswith("source ./scripts/lib/account_flow_paths.sh\n\n"
                                          "source ./scripts/lib/run_yuanyingchuqiao_send.sh\n"), account)
            normalized.append(text.replace(account, "ACCOUNT"))
        self.assertTrue(all(text == normalized[0] for text in normalized[1:]))
        lib = self.LIB.read_text(encoding="utf-8")
        self.assertIn("export TG_MESSAGE='.元婴出窍'", lib)
        # MiniApp first; the group-command body is only its fallback.
        self.assertLess(lib.index("xiu_route_miniapp_command"), lib.index("export TG_MESSAGE="))

    def test_observer_sequence_wrappers_are_executable(self):
        observers = sorted((ROOT / "scripts").glob("run_yuanyingchuqiao_return_observer_tg*.sh"))
        self.assertTrue(observers)
        for observer in observers:
            text = observer.read_text(encoding="utf-8")
            match = re.search(r"^export TG_SEQUENCE_SEND_WRAPPER='([^']+)'$", text, re.MULTILINE)
            if match is None:
                self.fail(observer.name)
            wrapper = ROOT / match.group(1)
            self.assertTrue(wrapper.is_file(), wrapper)
            self.assertTrue(os.access(wrapper, os.X_OK), f"not executable: {wrapper}")

    def test_observer_templates_are_identical_after_account_normalization(self):
        normalized = []
        for account in ACCOUNTS:
            text = (ROOT / f"scripts/run_yuanyingchuqiao_return_observer_{account}.sh").read_text(encoding="utf-8")
            text = text.replace(account, "ACCOUNT").replace(USERNAMES[account], "USERNAME")
            text = text.replace("export XIU_ACCOUNT_SLUG='ACCOUNT'\n", "")
            text = text.replace("export TG_SESSION_FALLBACK_NAME='state/ACCOUNT'\n", "")
            normalized.append(text)
        self.assertTrue(all(text == normalized[0] for text in normalized[1:]))


if __name__ == "__main__":
    unittest.main()
