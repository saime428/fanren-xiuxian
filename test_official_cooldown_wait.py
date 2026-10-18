"""Official remaining time takes precedence over no-reply retries.

The 14-minute-48-second text is from official reply 12304454. All sends and
clock changes below are isolated fixtures, never replays to Telegram.
"""
import unittest
from unittest.mock import patch

import test_nanlonghou_trade as trades
import test_question_reply_monitor as fixtures
from event_command_receipts import fixed_event_receipt
from reply_parsing import AckPolicy, classify_reply_messages, parse_cooldown_retry_delay_seconds


REQUESTED_COOLDOWN = "凡间方才承受神谕，需再等待 14分钟48秒。"


class RemainingTimeParserTests(unittest.TestCase):
    def test_explicit_remaining_time_overrides_a_narrow_configured_marker_list(self):
        cases = [
            (REQUESTED_COOLDOWN, 888),
            ("凡間方才承受神諭，需再等待 14分鐘48秒。", 888),
            ("本次历练时长 8小时，剩余冷却时间：14分钟48秒。", 888),
            ("本次歷練時長 8小時，剩餘時間：00:14:48。", 888),
            ("冷却还剩 2小时58分钟50秒。", 10730),
            ("冷卻還需 1分鐘。", 60),
            ("剩余时间：1天2小时3分钟4秒。", 93784),
            ("剩余时间：0秒。", 0),
        ]
        for text, seconds in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_cooldown_retry_delay_seconds(text, ["请在"]), seconds)
                self.assertEqual(fixed_event_receipt(text, "shenji_budao", ".神迹 布道"),
                                 {"status": "cooldown", "command": ".神迹 布道", "retry_seconds": seconds})

    def test_non_timer_counts_and_question_windows_are_not_cooldowns(self):
        for text in ("剩余 5 次，作答窗口为 300秒。", "你有 300秒 的时间作答。", "修为增加 888 点。"):
            with self.subTest(text=text):
                self.assertIsNone(parse_cooldown_retry_delay_seconds(text, ["请在"]))
                self.assertIsNone(fixed_event_receipt(text, "shenji_budao", ".神迹 布道"))


class EventCooldownWaitTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.QuestionReplyTests.asyncSetUp
    asyncTearDown = fixtures.QuestionReplyTests.asyncTearDown
    monitor = fixtures.QuestionReplyTests.monitor
    message = fixtures.QuestionReplyTests.message
    observe = fixtures.QuestionReplyTests.observe
    jiyin_question = fixtures.QuestionReplyTests.jiyin_question

    def disaster(self):
        return self.message(12300713, "【小世界·天降浩劫】 @demo_player_01 请速速查看 .小世界 安抚信徒！", age=0)

    def cooldown(self, monitor, key, *, text=REQUESTED_COOLDOWN, ident=12304454):
        result = self.message(ident, text, age=0)
        result.reply_to.reply_to_msg_id = monitor.rows[key]["sent_message_id"]
        return result

    async def test_official_cooldown_is_not_shortened_by_send_timeout_or_restart(self):
        monitor = self.monitor("shenji_budao", reply_text=".神迹 布道")
        question = self.disaster()
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.now += 61
        result = self.cooldown(monitor, key)
        expected_due = result.date.timestamp() + 888 + 1

        async def timed_out_send(entity, text, *, reply_to):
            self.client.sends.append((question.chat_id, text, reply_to))
            self.observe(monitor, result)
            raise TimeoutError("RPC result lost after the official cooldown arrived")

        monitor.tick()
        with patch.object(self.client, "send_message", side_effect=timed_out_send):
            await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 2)
        self.assertGreaterEqual(monitor.rows[key]["retry_at_ts"], expected_due)
        self.now += 60
        restarted = self.monitor("shenji_budao", reply_text=".神迹 布道")
        restarted.tick()
        await restarted.dispatch(key)
        self.assertEqual(len(self.client.sends), 2)
        self.assertGreaterEqual(restarted.health.data["pending_command"]["retry_at_ts"], expected_due)
        self.assertFalse(restarted.state_path.exists())

        self.now = expected_due + 1
        restarted.tick()
        await restarted.dispatch(key)
        self.assertEqual(len(self.client.sends), 3)

    async def test_old_short_retry_timestamp_cannot_bypass_a_persisted_server_cooldown(self):
        monitor = self.monitor("shenji_budao", reply_text=".神迹 布道")
        question = self.disaster()
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.observe(monitor, self.cooldown(monitor, key))
        expected_due = self.now + 889
        # A previous writer could overwrite retry_at_ts in its exception path.
        # The retained official deadline still forbids an early send.
        monitor.rows[key].update(status="reply_retry_pending", retry_at_ts=self.now)
        monitor.persist()
        self.client.messages.clear()
        restarted = self.monitor("shenji_budao", reply_text=".神迹 布道")
        await restarted.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)
        self.assertGreaterEqual(restarted.health.data["pending_command"]["retry_at_ts"], expected_due)

    async def test_all_question_branches_wait_and_replay_does_not_extend_remaining_time(self):
        original_root = self.root
        flows = ("shenji_budao", "jiyin_choice", "xuangu_zuoda", "jiaohuan_gongfa")
        for index, flow in enumerate(flows):
            with self.subTest(flow=flow):
                self.root = original_root / flow
                self.root.mkdir()
                options = {"reply_text": ".交换 功法"} if flow == "jiaohuan_gongfa" else {}
                monitor = self.monitor(flow, **options)
                question = (self.disaster() if flow == "shenji_budao" else self.jiyin_question()
                            if flow == "jiyin_choice" else self.message(12304400 + index,
                                trades.QUESTION.replace("@demo_player_03", "@demo_player_01"))
                            if flow == "jiaohuan_gongfa" else self.message(12304400 + index))
                self.observe(monitor, question)
                key = monitor.key(question.chat_id, question.id)
                before = len(self.client.sends)
                await monitor.dispatch(key)
                result = self.cooldown(monitor, key, text="剩余时间：2分钟。", ident=12304454 + index)
                expected_due = self.now + 121
                self.observe(monitor, result)
                self.now += 61
                restarted = self.monitor(flow, **options)
                self.observe(restarted, result)
                restarted.tick()
                await restarted.dispatch(key)
                self.assertEqual(len(self.client.sends), before + 1)
                self.assertEqual(restarted.rows[key]["server_retry_at_ts"], expected_due)
                self.assertFalse(restarted.state_path.exists())
                self.now = expected_due + 1
                restarted.tick()
                await restarted.dispatch(key)
                self.assertEqual(len(self.client.sends), before + 2)
                self.now += 400
        self.root = original_root

    async def test_ordinary_sender_treats_a_timed_refusal_as_cooldown(self):
        command = self.message(100, ".问道", sender=9100000001)
        reply = self.message(101, "暂时无法问道，冷却还剩 14分钟48秒。", age=0)
        reply.reply_to.reply_to_msg_id = command.id
        result = classify_reply_messages([command, reply], topic_id=7310786,
            own_sender_id=9100000001, target_message=".问道", sent_message_id=100,
            reset_triggers=[], cooldown_markers=["请在"],
            policy=AckPolicy(failure_markers=["无法"]), log=lambda *args: None)
        self.assertTrue(result.acked)
        self.assertFalse(result.succeeded)
        self.assertFalse(result.failed)
        self.assertEqual(result.cooldown_retry_seconds, 888)

    async def test_empty_marker_list_does_not_turn_an_unrelated_duration_into_cooldown(self):
        command = self.message(100, ".问道", sender=9100000001)
        reply = self.message(101, "这段典故发生在 1小时 之前。", age=0)
        reply.reply_to.reply_to_msg_id = command.id
        result = classify_reply_messages([command, reply], topic_id=7310786,
            own_sender_id=9100000001, target_message=".问道", sent_message_id=100,
            reset_triggers=[], cooldown_markers=[], policy=AckPolicy(), log=lambda *args: None)
        self.assertFalse(result.acked)
        self.assertIsNone(result.cooldown_retry_seconds)

    async def test_foreign_cooldowns_cannot_defer_an_answer_and_success_remains_terminal(self):
        monitor = self.monitor()
        question = self.message()
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        for index, change in enumerate((
                {"sender_id": 111}, {"message": "@another_player 剩余时间：2分钟。"},
                {"fwd_from": object()}, {"chat_id": -1009400000005})):
            result = self.cooldown(monitor, key, text="剩余时间：2分钟。", ident=12304454 + index)
            for field, value in change.items():
                setattr(result, field, value)
            self.observe(monitor, result)
            self.assertNotIn("server_retry_at_ts", monitor.rows[key])
        valid = self.cooldown(monitor, key, text="剩余时间：2分钟。")
        self.observe(monitor, valid)
        self.assertEqual(monitor.rows[key]["server_retry_at_ts"], self.now + 121)
        success = self.message(12304480, "【玄骨考校·答对】\n@demo_player_01 的答案 B 完全正确！", age=0)
        success.reply_to.reply_to_msg_id = monitor.rows[key]["sent_message_id"]
        self.observe(monitor, success)
        self.assertEqual(monitor.rows[key]["status"], "answer_confirmed")
        self.assertTrue(monitor.state_path.exists())
        self.observe(monitor, self.cooldown(monitor, key, text="剩余时间：2分钟。", ident=12304481))
        self.now += 122
        monitor.tick()
        await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)
        self.assertEqual(monitor.rows[key]["status"], "answer_confirmed")

    async def test_cooldown_never_extends_an_expired_question(self):
        monitor = self.monitor()
        question = self.message()
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.observe(monitor, self.cooldown(monitor, key))
        self.now += 900
        restarted = self.monitor()
        restarted.tick()
        await restarted.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)
        self.assertEqual(restarted.rows[key]["status"], "answer_result_unconfirmed")
        self.assertFalse(restarted.state_path.exists())


if __name__ == "__main__":
    unittest.main()
