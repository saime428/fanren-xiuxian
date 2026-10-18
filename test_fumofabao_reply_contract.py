import plistlib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from reply_parsing import AckPolicy, classify_reply_messages, decide_poll_action
from send_topic_message import resend_after_explicit_failure


class Message:
    def __init__(self, message_id, sender_id, text, *, reply_to_msg_id=None):
        self.id = message_id
        self.sender_id = sender_id
        self.message = text
        self.reply_to_msg_id = reply_to_msg_id
        self.reply_to = None
        self.date = None


class FumoFabaoReplyContractTests(unittest.TestCase):
    MESSAGE = ".抚摸法宝 玄天斩灵剑"
    TOPIC_ID = 7310786
    OWNER_ID = 9100000001
    SUCCESS_MARKERS = [
        "蹭了蹭你的掌心",
        "发出一声清脆的鸣叫",
        "在你手中微微颤动",
        "联系更加紧密",
        "傲娇地闪烁",
    ]
    FAILURE_MARKERS = ["没有这件拥有器灵的法宝", "名字输入错误"]

    def _classify(self, bot_text):
        sent = Message(100, self.OWNER_ID, self.MESSAGE, reply_to_msg_id=self.TOPIC_ID)
        reply = Message(101, 8917921351, bot_text, reply_to_msg_id=100)
        return classify_reply_messages(
            [sent, reply],
            topic_id=self.TOPIC_ID,
            own_sender_id=self.OWNER_ID,
            target_message=self.MESSAGE,
            sent_message_id=100,
            reset_triggers=[],
            cooldown_markers=["后再与它互动", "请在", "剩余"],
            policy=AckPolicy(
                reply_bot_id=8917921351,
                success_markers=self.SUCCESS_MARKERS,
                ack_markers=self.SUCCESS_MARKERS,
                failure_markers=self.FAILURE_MARKERS,
                require_success_or_cooldown=True,
            ),
            log=lambda *args, **kwargs: None,
        )

    def test_actual_824582_reply_is_success(self):
        outcome = self._classify("斩灵 蹭了蹭你的掌心，显得十分亲昵。\n(默契 +4, 经验 +16)")
        self.assertTrue(outcome.acked)
        self.assertIsNone(outcome.cooldown_retry_seconds)

    def test_all_observed_reward_wordings_are_success(self):
        for text in (
            "“斩灵”发出一声清脆的鸣叫，灵光流转。\n(默契 +4, 经验 +12)",
            "“斩灵”在你手中微微颤动，似乎很享受。\n(默契 +5, 经验 +14)",
            "你感觉到与玄天斩灵剑的联系更加紧密了，器灵传来了喜悦的情绪。\n(默契 +4, 经验 +11)",
            "“别摸啦，痒！” 斩灵 (冷酷) 傲娇地闪烁了一下。\n(默契 +2, 经验 +18)",
        ):
            with self.subTest(text=text):
                self.assertTrue(self._classify(text).acked)

    def test_countdown_is_not_success_and_sets_retry_delay(self):
        outcome = self._classify("器灵方才与你互动过，请在 1小时 59分钟 后再与它互动。")
        self.assertTrue(outcome.acked)  # reply arrived, but not a successful touch
        self.assertEqual(outcome.cooldown_retry_seconds, 7140)
        self.assertNotIn("蹭了蹭你的掌心", outcome.ack_reply_text)

    def test_actual_12185899_reply_is_terminal_failure_not_success(self):
        outcome = self._classify("你没有这件拥有器灵的法宝，或者名字输入错误。")
        self.assertTrue(outcome.acked)
        self.assertTrue(outcome.failed)
        self.assertFalse(outcome.succeeded)
        self.assertEqual("failed", outcome.terminal_status)
        self.assertEqual("failure_resend", decide_poll_action(outcome))

    def test_topic_root_success_is_not_accepted_without_direct_attribution(self):
        sent = Message(12265203, self.OWNER_ID, self.MESSAGE, reply_to_msg_id=self.TOPIC_ID)
        reply = Message(
            12265204,
            8980154525,
            "你感觉到与玄天斩灵剑的联系更加紧密了，器灵传来了喜悦的情绪。\n(默契 +3, 经验 +18)",
            reply_to_msg_id=self.TOPIC_ID,
        )
        outcome = classify_reply_messages(
            [sent, reply],
            topic_id=self.TOPIC_ID,
            own_sender_id=self.OWNER_ID,
            target_message=self.MESSAGE,
            sent_message_id=sent.id,
            reset_triggers=[],
            cooldown_markers=["后再与它互动", "请在", "剩余"],
            policy=AckPolicy(success_markers=self.SUCCESS_MARKERS,
                             ack_markers=self.SUCCESS_MARKERS,
                             failure_markers=self.FAILURE_MARKERS,
                             require_success_or_cooldown=True),
            log=lambda *args, **kwargs: None,
        )

        self.assertFalse(outcome.acked)
        self.assertFalse(outcome.succeeded)
        self.assertEqual([], outcome.matched_ids)

    def test_wrapper_enables_one_failure_resend(self):
        wrapper = Path("scripts/send_fumofabao_xuantianzhanlingjian_tg9100000001.sh").read_text(encoding="utf-8")
        self.assertIn("export TG_FAILURE_RESEND_MAX='1'", wrapper)

    def test_explicit_failure_resends_once_then_stops(self):
        send = AsyncMock(side_effect=[Message(200, self.OWNER_ID, self.MESSAGE), Message(201, self.OWNER_ID, self.MESSAGE)])
        first = self._classify("你没有这件拥有器灵的法宝，或者名字输入错误。")
        second = self._classify("你没有这件拥有器灵的法宝，或者名字输入错误。")

        first_result = self.async_run(resend_after_explicit_failure(first, send=send, resend_count=0, resend_max=1))
        second_result = self.async_run(resend_after_explicit_failure(second, send=send, resend_count=1, resend_max=1))

        self.assertIsNotNone(first_result)
        self.assertIsNone(second_result)
        self.assertEqual(1, send.await_count)

    @staticmethod
    def async_run(coro):
        import asyncio
        return asyncio.run(coro)

    def test_unrelated_bot_reply_is_not_accepted(self):
        outcome = self._classify("本命法宝状态正常。")
        self.assertFalse(outcome.acked)
        self.assertIsNone(outcome.cooldown_retry_seconds)

    def test_wrapper_does_not_treat_generic_artifact_words_as_success(self):
        wrapper = Path("scripts/send_fumofabao_xuantianzhanlingjian_tg9100000001.sh").read_text(encoding="utf-8")
        self.assertIn("联系更加紧密", wrapper)
        self.assertIn("傲娇地闪烁", wrapper)
        self.assertIn("export TG_COOLDOWN_COUNTS_AS_SUCCESS='0'", wrapper)
        self.assertIn("export TG_REQUIRE_SUCCESS_OR_COOLDOWN='1'", wrapper)


    def test_cooldown_is_always_distinct_from_success(self):
        outcome = self._classify("器灵方才与你互动过，请在 1小时 后再与它互动。")
        self.assertTrue(outcome.acked)
        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.terminal_status, "cooldown")

    def test_schedule_runs_every_two_hours(self):
        plist = Path(
            "scripts/ai.hermes.xiuxian.tg9100000001.fumofabao_xuantianzhanlingjian.send.plist"
        )
        payload = plistlib.loads(plist.read_bytes())
        self.assertEqual(7200, payload["StartInterval"])


if __name__ == "__main__":
    unittest.main()
