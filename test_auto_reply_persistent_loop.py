import unittest

from auto_reply_topic_event import normalized_reconcile_seconds, parse_trusted_sender_ids


class AutoReplyPersistentLoopTests(unittest.TestCase):
    def test_auto_reply_requires_verified_sender_ids(self) -> None:
        self.assertEqual({123, 456}, parse_trusted_sender_ids("123,456"))
        with self.assertRaises(SystemExit):
            parse_trusted_sender_ids("")

    def test_short_deadline_flow_can_reconcile_every_ten_seconds(self) -> None:
        self.assertEqual(10.0, normalized_reconcile_seconds("10"))


if __name__ == "__main__":
    unittest.main()
