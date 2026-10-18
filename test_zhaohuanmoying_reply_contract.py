import unittest
from pathlib import Path
import plistlib
from datetime import datetime, timezone

from reply_parsing import AckPolicy, classify_reply_messages, decide_poll_action


class Msg:
    def __init__(self, mid, sender_id, text, reply_to_msg_id=None):
        self.id = mid
        self.sender_id = sender_id
        self.message = text
        self.reply_to_msg_id = reply_to_msg_id
        self.reply_to = None
        self.date = datetime.now(timezone.utc)


class ZhaohuanMoyingReplyContractTests(unittest.TestCase):
    SUCCESS = ["召唤成功，镇压成功", "魔影被你成功击溃"]
    ACK = ["召唤成功，镇压失败", "魔影过于强大", "修为暴跌"]
    COOLDOWN = ["魔域裂隙尚未平复", "后再行召唤"]

    def classify(self, reply_text):
        own = Msg(100, 9100000002, ".召唤魔影")
        reply = Msg(101, 8735907987, reply_text, reply_to_msg_id=100)
        return classify_reply_messages(
            [own, reply],
            topic_id=7310786,
            own_sender_id=9100000002,
            target_message=".召唤魔影",
            sent_message_id=100,
            reset_triggers=[],
            cooldown_markers=self.COOLDOWN,
            policy=AckPolicy(
                reply_bot_id=8735907987,
                success_markers=self.SUCCESS,
                ack_markers=self.ACK,
            ),
            log=lambda *args: None,
        )

    def test_server_failure_settlement_is_terminal_ack_not_resend(self):
        outcome = self.classify("召唤成功，镇压失败！\n魔影过于强大，你遭其反噬，修为暴跌了 7237 点！")
        self.assertTrue(outcome.acked)
        self.assertEqual("acknowledged", decide_poll_action(outcome))

    def test_magic_rift_countdown_is_cooldown_not_resend(self):
        outcome = self.classify("魔域裂隙尚未平复，请在 7小时59分钟19秒 后再行召唤。")
        self.assertTrue(outcome.acked)
        self.assertIsNotNone(outcome.cooldown_retry_seconds)
        self.assertEqual("cooldown", decide_poll_action(outcome))

    def test_success_settlement_remains_success(self):
        outcome = self.classify("召唤成功，镇压成功！\n魔影被你成功击溃，留下一道凶兽戾魄。")
        self.assertTrue(outcome.acked)
        self.assertEqual("acknowledged", decide_poll_action(outcome))

    def test_scheduler_polls_like_state_driven_flows(self):
        payload = plistlib.loads(
            Path("scripts/ai.hermes.xiuxian.tg9100000002.zhaohuanmoying.send.plist").read_bytes()
        )
        self.assertEqual(300, payload["StartInterval"])

    def test_wrapper_skips_future_cooldown_without_sleeping(self):
        text = Path("scripts/send_zhaohuanmoying_tg9100000002.sh").read_text(encoding="utf-8")
        self.assertIn("Cooldown retry pending", text)
        self.assertIn("skip this run", text)
        self.assertNotIn('sleep "${wait_seconds}"', text)


if __name__ == "__main__":
    unittest.main()
