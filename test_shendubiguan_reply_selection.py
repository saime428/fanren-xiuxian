import unittest

from reply_parsing import classify_action_reply, classify_status_reply
from send_shendubiguan import select_classified_reply


class ShendubiguanReplySelectionTests(unittest.TestCase):
    def test_status_probe_ignores_unrelated_topic_root_chatter(self):
        replies = [
            {"id": 101, "text": "你已在深度闭关之中。", "ts": 1.0},
            {"id": 102, "text": ".宗门点卯", "ts": 2.0},
        ]
        selected, status, _ = select_classified_reply(
            replies,
            classifier=lambda text: classify_status_reply(text, expected_username="demo_player_01"),
        )
        self.assertEqual(101, selected["id"])
        self.assertEqual("already_in_retreat", status)

    def test_action_wait_ignores_unrelated_topic_root_chatter(self):
        replies = [
            {"id": 201, "text": "你已进入深度闭关状态，神魂将自行吐纳 8 小时。", "ts": 1.0},
            {"id": 202, "text": ".问道", "ts": 2.0},
        ]
        selected, status, duration = select_classified_reply(
            replies,
            classifier=classify_action_reply,
        )
        self.assertEqual(201, selected["id"])
        self.assertEqual("started", status)
        self.assertEqual(8 * 60 * 60, duration)


if __name__ == "__main__":
    unittest.main()
