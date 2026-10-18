import unittest
from pathlib import Path


class ShendubiguanRootObserverWrapperTests(unittest.TestCase):
    def test_all_active_shendubiguan_observers_allow_root_topic_return_events(self) -> None:
        for script in sorted(Path("scripts").glob("run_shendubiguan_return_observer_tg*.sh")):
            with self.subTest(script=str(script)):
                text = script.read_text(encoding="utf-8")
                self.assertIn("export TG_RETURN_MARKER_TEXT='深度闭关总结|深度閉關總結|你并未处于深度闭关之中|你並未處於深度閉關之中", text)
                self.assertIn("export TG_RETURN_ALLOW_ROOT_TOPIC_EVENTS='1'", text)
                self.assertIn("export TG_TRIGGER_TARGET_USERNAME=", text)
                self.assertIn("export TG_OBSERVER_PERSISTENT='1'", text)
                # Trusted bots come from command_confirmation, not a shell copy.
                self.assertNotIn("TG_RETURN_TRUSTED_SENDER_IDS", text)


if __name__ == "__main__":
    unittest.main()
