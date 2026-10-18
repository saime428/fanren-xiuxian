import unittest

from reply_parsing import ReplyOutcome, decide_poll_action, parse_cooldown_retry_delay_seconds


class StrongVerificationTests(unittest.TestCase):
    def test_cooldown_parser_still_works(self):
        self.assertEqual(
            parse_cooldown_retry_delay_seconds("【野外历练】\n山中灵机未复，请在 1分钟39秒 后再来。", ["后再来", "後再來"]),
            99,
        )

    def test_decide_acknowledged(self):
        self.assertEqual(decide_poll_action(ReplyOutcome(acked=True)), "acknowledged")

    def test_decide_reset_takes_priority(self):
        self.assertEqual(decide_poll_action(ReplyOutcome(acked=True, reset_triggered=True)), "reset_resend")

    def test_decide_cooldown_beats_ack(self):
        self.assertEqual(decide_poll_action(ReplyOutcome(acked=True, cooldown_retry_seconds=125)), "cooldown")

    def test_decide_zero_second_cooldown_still_counts(self):
        self.assertEqual(decide_poll_action(ReplyOutcome(acked=True, cooldown_retry_seconds=0)), "cooldown")

    def test_decide_keep_waiting_without_reply(self):
        self.assertEqual(decide_poll_action(ReplyOutcome()), "keep_waiting")


if __name__ == "__main__":
    unittest.main()
