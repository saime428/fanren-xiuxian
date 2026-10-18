import re
import unittest
from pathlib import Path

from command_confirmation import OFFICIAL_REPLY_SENDER_IDS

ROOT = Path(__file__).parent


class ReturnObserverConfigTests(unittest.TestCase):
    def test_return_observers_trust_the_canonical_bot_set_not_a_shell_copy(self) -> None:
        # Hand-copied lists drifted to 9 or 10 of the 12 official bots. Both
        # listeners take command_confirmation.OFFICIAL_REPLY_SENDER_IDS
        # themselves; a wrapper may not carry (and narrow) its own copy.
        wrappers = sorted((ROOT / "scripts").glob("run_*return_observer_*.sh"))
        self.assertGreaterEqual(len(wrappers), 12)
        for wrapper in wrappers:
            with self.subTest(wrapper=wrapper.name):
                self.assertNotIn("TRUSTED_SENDER_IDS", wrapper.read_text(encoding="utf-8"))
        observer = (ROOT / "observe_return.py").read_text(encoding="utf-8")
        self.assertIn("trusted_sender_ids |= set(OFFICIAL_REPLY_SENDER_IDS)", observer)
        second_soul = (ROOT / "yuanshen_return.py").read_text(encoding="utf-8")
        self.assertRegex(second_soul, r'event\["sender_id"\] in OFFICIAL_REPLY_SENDER_IDS')
        self.assertEqual(12, len(OFFICIAL_REPLY_SENDER_IDS))

    def test_all_yuanying_return_observers_are_persistent_push_listeners(self) -> None:
        wrappers = sorted((ROOT / "scripts").glob("run_yuanyingchuqiao_return_observer_*.sh"))
        self.assertGreaterEqual(len(wrappers), 1)
        for wrapper in wrappers:
            with self.subTest(wrapper=wrapper.name):
                text = wrapper.read_text(encoding="utf-8")
                self.assertIn("export TG_OBSERVER_PERSISTENT='1'", text)
                self.assertIn("export TG_OBSERVER_RECONCILE_SECONDS='60'", text)
                self.assertNotIn("TG_OBSERVER_POLL_SECONDS", text)
                self.assertIsNotNone(re.search(r"^export TG_SEQUENCE_SEND_WRAPPER='\./scripts/", text, re.M))


if __name__ == "__main__":
    unittest.main()
