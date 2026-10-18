import unittest
from pathlib import Path


class YuanyingObserverImmediateKickWrapperTests(unittest.TestCase):
    def test_observer_immediate_kick_bypasses_status_probe_before_cache_wait(self) -> None:
        for script in [
            Path("scripts/lib/run_yuanyingchuqiao_send.sh"),  # shared by every account wrapper
        ]:
            with self.subTest(script=str(script)):
                text = script.read_text(encoding="utf-8")
                guard = 'if [[ "${TG_OBSERVER_IMMEDIATE_KICK:-0}" == "1" ]]; then'
                cache_wait = 'wait_seconds="$(./.venv/bin/python3 command_confirmation.py --cooldown-remaining'
                probe_call = './.venv/bin/python3 probe_yuanyingchuqiao_status.py'
                send_call = 'exec ./.venv/bin/python3 send_topic_message.py'
                self.assertIn(guard, text)
                self.assertIn(cache_wait, text)
                self.assertIn(probe_call, text)
                self.assertIn(send_call, text)
                self.assertLess(text.index(guard), text.index(cache_wait))
                immediate_block = text[text.index(guard): text.index(cache_wait)]
                self.assertIn("bypassing status probe/cache", immediate_block)
                self.assertIn("export TG_MIN_REPEAT_SECONDS='0'", immediate_block)
                self.assertIn("export TG_PRE_REPEAT_PROBE_SECONDS='0'", immediate_block)
                self.assertIn(send_call, immediate_block)
                self.assertNotIn(probe_call, immediate_block)


if __name__ == "__main__":
    unittest.main()
