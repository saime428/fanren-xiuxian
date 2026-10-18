import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import watch_send_health as health


class WatchSendHealthBotCountdownTests(unittest.TestCase):
    def test_matching_future_bot_countdown_is_healthy(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status_probe.json"
            path.write_text(
                json.dumps(
                    {
                        "ts": now - 10,
                        "status": "countdown_pending", "reply_message_id": 100, "reply_sender_id": 8917921351,
                        "acked": True, "actor_id": 9100000001, "sent_message_id": 99,
                        "message": ".元婴状态", "topic_id": 7310786,
                        "flow_slug": "yuanyingchuqiao",
                        "target": "@example_game_group",
                        "not_before_ts": now + 3600,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(health, "STATUS_PROBE_PATH", path), patch.object(
                health, "FLOW_SLUG", "yuanyingchuqiao"
            ), patch.dict("os.environ", {"TG_TARGET": "@example_game_group", "XIU_ACCOUNT_SLUG": "tg9100000001"}, clear=False):
                found = health.fresh_flow_countdown(now)
        self.assertIsNotNone(found)
        self.assertEqual("countdown_pending", found["status"])

    def test_wrong_flow_or_expired_countdown_is_rejected(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status_probe.json"
            path.write_text(
                json.dumps(
                    {
                        "ts": now - 10,
                        "status": "countdown_pending", "reply_message_id": 100, "reply_sender_id": 8917921351,
                        "flow_slug": "shendubiguan",
                        "target": "@example_game_group",
                        "not_before_ts": now - 1,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(health, "STATUS_PROBE_PATH", path), patch.object(
                health, "FLOW_SLUG", "yuanyingchuqiao"
            ):
                self.assertIsNone(health.fresh_flow_countdown(now))


if __name__ == "__main__":
    unittest.main()
