"""Offline Xuangu replay: real message shapes, isolated Telegram substitute."""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_question_reply_monitor as fixtures
import xuangu_quiz as xuangu
from question_reply_monitor import CANONICAL_CHAT_ID, question_kind, question_max_age
from scripts import xiuxian_healthcheck as healthcheck


class XuanguAutomationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.QuestionReplyTests.asyncSetUp
    asyncTearDown = fixtures.QuestionReplyTests.asyncTearDown
    monitor = fixtures.QuestionReplyTests.monitor
    message = fixtures.QuestionReplyTests.message
    observe = fixtures.QuestionReplyTests.observe
    own_reply = fixtures.QuestionReplyTests.own_reply

    def result(self, ident=9000020, *, owner="demo_player_01", option="B", variant="考校", outcome="答对", **settings):
        # Header, wording and topic-root placement match authenticated message
        # 12296170. The owner/letter are substituted for isolated replay.
        text = (f"【玄骨{variant}·{outcome}】\n@{owner} 的答案 {option} 完全正确！\n"
                "玄骨上人冷哼一声：“算你这小辈还有点见识。”\n你获得了 5000 点修为作为奖赏！")
        if outcome == "答错":
            text = text.replace("完全正确", "并不正确")
        if outcome == "超时":
            # Timeout wording is from authenticated message 12294812.
            text = (f"【玄骨{variant}·超时】\n@{owner} 面对玄骨上人的提问，竟迟迟无法作答，被视为不堪造就。\n"
                    "玄骨上人略感失望，一道神念冲击让你损失了 1000 点修为。")
        message = self.message(ident, text, age=0, **settings)
        if message.chat_id != CANONICAL_CHAT_ID:
            message.reply_to.reply_to_msg_id = None
        return message

    async def submit(self, monitor, question=None):
        question = question or self.message()
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        return question, key

    async def test_requested_nanlonghou_link_does_not_trigger_xuangu(self):
        text = ("【天机异闻·南陇侯的交易】\n道友 @cncc01 经过深思熟虑，选择将侍妾【冰凤】与南陇侯交换！\n"
                "作为回报，南陇侯赐予了其一件至宝：【青竹蜂云剑】！")
        monitor = self.monitor()
        self.observe(monitor, self.message(1103237, text, chat=-1009400000005))
        self.assertIsNone(question_kind(text, xuangu.FLOW))
        self.assertEqual(monitor.rows, {})
        self.assertEqual(self.client.sends, [])

    async def test_historical_questions_use_their_own_answer_after_every_option_shuffle(self):
        cases = (
            ("乾蓝冰焰在原著中最初是封附在何种通天灵宝之外？", "虚天鼎", False),
            ("韩立在内殿对付玄骨时，最能克制魔修的手段是什么？", "辟邪神雷", False),
            ("韩立能把虚天鼎从乾蓝冰焰池中拉出的关键倚仗是什么？", "血玉蜘蛛", False),
            ("原著路线里，乾蓝冰焰是何时才适合从虚天鼎上开始缓慢炼化？", "元婴后", True),
        )
        answers = [case[1] for case in cases]
        monitor = self.monitor(bank=answers)
        for stem, correct, rare in cases:
            for slot in range(4):
                with self.subTest(question=stem, slot=slot):
                    options = [answer for answer in answers if answer != correct]
                    options.insert(slot, correct)
                    prefix = "那道魔念向 @demo_player_01 发问：" if rare else "神念直入脑海，一个苍老的声音向 @demo_player_01 提问："
                    text = prefix + f"\n\n“{stem}”\n\n" + "\n".join(f"{code}. {answer}" for code, answer in zip("ABCD", options))
                    self.assertEqual(monitor.answer(text), f".作答 {'ABCD'[slot]}")

    async def test_global_answer_words_do_not_answer_an_unrelated_or_negated_question(self):
        monitor = self.monitor()
        original = self.message().message
        self.assertIsNone(monitor.answer(original.replace("乾蓝冰焰最初封附在何种通天灵宝之外？", "哪件法宝不属于韩立？")))
        self.assertIsNone(monitor.answer(original.replace("B. 虚天鼎", "B. 并不是虚天鼎")))
        self.assertIsNone(monitor.answer(original.replace("B. 虚天鼎", "B. 虚天")))

    async def test_secondary_and_foreign_topic_questions_cannot_relocate_commands(self):
        monitor = self.monitor()
        questions = [self.message(chat=-1009400000005), self.message(105)]
        questions[1].reply_to.reply_to_msg_id = 12345
        for question in questions:
            self.observe(monitor, question)
            key = monitor.key(question.chat_id, question.id)
            await monitor.dispatch(key)
            self.assertEqual(monitor.rows[key]["status"], "readonly_source")
        self.assertEqual(self.client.sends, [])
        self.assertIsNone(monitor.health.data["business_issue"])

    async def test_shortened_window_and_restart_never_extend_original_deadline(self):
        monitor = self.monitor()
        question = self.message(age=20)
        question.message = question.message.replace("300秒", "30秒")
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        question.message = question.message.replace("30秒", "300秒")
        self.observe(monitor, question)
        restarted = self.monitor()
        self.now += 11
        await restarted.dispatch(key)
        self.assertEqual(self.client.sends, [])
        self.assertEqual(restarted.rows[key]["status"], "expired_unanswered")

    async def test_three_hundred_seconds_is_a_hard_cap_and_forwarded_questions_are_ignored(self):
        self.assertEqual(question_max_age("你有 900秒", xuangu.FLOW, 900), 300)
        self.assertEqual(question_max_age("你有 30秒", xuangu.FLOW, 900), 30)
        monitor = self.monitor()
        question = self.message()
        question.fwd_from = SimpleNamespace(date=question.date)
        self.observe(monitor, question)
        self.assertEqual(monitor.rows, {})
        self.assertEqual(self.client.sends, [])

    async def test_shorter_deadline_pushed_during_gate_wait_is_rechecked_before_send(self):
        monitor = self.monitor()
        question = self.message(age=20)
        self.observe(monitor, question)
        async def gate(action, **kwargs):
            # The exact question's edit arrives by push while the dispatch waits
            # for its send slot; the slot only re-checks local state.
            edited = SimpleNamespace(**vars(question))
            edited.message = question.message.replace("300秒", "10秒")
            monitor.observe(edited, edited.chat_id)
            return await action()
        key = monitor.key(question.chat_id, question.id)
        with patch("question_reply_monitor.send_with_global_gate", side_effect=gate):
            await monitor.dispatch(key)
        self.assertEqual(self.client.sends, [])
        self.assertEqual(monitor.rows[key]["status"], "expired_unanswered")

    async def test_manual_different_answer_survives_unrelated_chatter_and_deleted_question(self):
        monitor = self.monitor()
        question = self.message()
        self.observe(monitor, question)
        own = self.own_reply(question)
        own.message = ".作答   a"
        self.observe(monitor, own)
        chatter = self.own_reply(question, ident=9000011)
        chatter.message = "普通聊天不能覆盖已有答案"
        self.observe(monitor, chatter)
        del self.client.messages[(question.chat_id, question.id)]
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.assertEqual(self.client.sends, [])
        self.assertEqual(monitor.rows[key]["reply_text"], ".作答 A")
        self.assertEqual(monitor.rows[key]["status"], "awaiting_answer_result")
        self.assertFalse((self.root / "last_success.json").exists())

    async def test_delivery_waits_for_attributed_root_result_then_records_success(self):
        monitor = self.monitor(auto_update_answer_bank=True)
        question, key = await self.submit(monitor)
        self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".作答 B", question.id)])
        self.assertEqual(monitor.rows[key]["status"], "awaiting_answer_result")
        self.assertFalse((self.root / "last_success.json").exists())
        self.observe(monitor, self.result(chat=-1009400000005))
        state = json.loads((self.root / "last_success.json").read_text())
        self.assertEqual(state["status"], "answer_confirmed")
        self.assertEqual(state["receipt_chat_id"], -1009400000005)
        self.assertEqual(state["event_message_id"], question.id)
        self.assertTrue(state["automated_submission"])
        self.assertEqual(monitor.rows[key]["status"], "answer_confirmed")
        learned = json.loads(monitor.learned_path.read_text())
        self.assertEqual(learned["questions"][0]["answers"], ["虚天鼎"])

    async def test_wrong_owner_bot_forward_option_variant_or_reply_chain_cannot_confirm(self):
        monitor = self.monitor()
        _, key = await self.submit(monitor)
        candidates = [self.result(110, owner="demo_player_01_other"), self.result(111, sender=999),
                      self.result(112, option="A"), self.result(113, variant="窥鼎"), self.result(114), self.result(115)]
        candidates[4].fwd_from = SimpleNamespace(date=candidates[4].date)
        candidates[5].reply_to.reply_to_msg_id = 123456
        for result in candidates:
            self.observe(monitor, result)
        self.assertEqual(monitor.rows[key]["status"], "awaiting_answer_result")
        self.assertFalse((self.root / "last_success.json").exists())

    async def test_wrong_answer_and_server_timeout_are_failures_without_success_files(self):
        monitor = self.monitor()
        _, first = await self.submit(monitor)
        self.observe(monitor, self.result(outcome="答错"))
        self.assertEqual(monitor.rows[first]["status"], "answer_incorrect")
        self.now += 500
        _, second = await self.submit(monitor, self.message(200))
        self.now += 301
        self.observe(monitor, self.result(9000030, outcome="超时"))
        self.assertEqual(monitor.rows[second]["status"], "answer_timed_out")
        self.assertFalse((self.root / "last_success.json").exists())

    async def test_unknown_send_recovers_from_official_result_without_resending(self):
        monitor = self.monitor()
        self.client.send_error = TimeoutError("lost Telegram ack")
        question, key = await self.submit(monitor)
        restarted = self.monitor()
        self.observe(restarted, question)
        await restarted.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)
        self.assertEqual(restarted.rows[key]["status"], "send_outcome_unknown")
        self.observe(restarted, self.result())
        self.assertEqual(restarted.rows[key]["status"], "answer_confirmed")
        self.assertIsNone(restarted.health.data["business_issue"])

    async def test_business_receipt_before_telegram_timeout_preserves_confirmed_success(self):
        monitor = self.monitor()
        async def submitted(entity, text, **kwargs):
            self.observe(monitor, self.result())
            raise TimeoutError("late lost acknowledgement")
        with patch.object(self.client, "send_message", side_effect=submitted):
            _, key = await self.submit(monitor)
        self.assertEqual(monitor.rows[key]["status"], "answer_confirmed")
        self.assertEqual(json.loads((self.root / "last_success.json").read_text())["status"], "answer_confirmed")

    async def test_unconfirmed_result_remains_bad_across_heartbeat_and_restart(self):
        monitor = self.monitor()
        _, key = await self.submit(monitor)
        self.now += 361
        monitor.tick()
        restarted = self.monitor()
        async def scan(*, on_scan_complete):
            on_scan_complete()
        await restarted.health.run_cycle(self.client, scan)
        self.assertEqual(restarted.rows[key]["status"], "answer_result_unconfirmed")
        self.assertEqual(restarted.health.data["business_issue"]["code"], "answer_result_unconfirmed")
        self.assertFalse((self.root / "last_success.json").exists())

    async def test_old_exact_answer_receipt_reconciles_after_long_shutdown(self):
        monitor = self.monitor()
        _, key = await self.submit(monitor)
        result = self.result()
        result.reply_to.reply_to_msg_id = monitor.rows[key]["sent_message_id"]
        self.now += 2 * 86400
        restarted = self.monitor()
        restarted.tick()
        broadcast = self.result(9000021)
        broadcast.date = result.date
        self.observe(restarted, broadcast)
        unrelated = self.result(9000022)
        unrelated.date = result.date
        unrelated.reply_to.reply_to_msg_id = 123456
        self.observe(restarted, unrelated)
        self.assertEqual(restarted.rows[key]["status"], "answer_result_unconfirmed")
        self.observe(restarted, result)
        await restarted.dispatch(key)
        self.assertEqual(restarted.rows[key]["status"], "answer_confirmed")
        self.assertEqual(len(self.client.sends), 1)
        self.assertEqual(json.loads(restarted.state_path.read_text())["receipt_message_id"], result.id)

    async def test_ambiguous_broadcast_cannot_be_reassigned_after_another_result_resolves(self):
        monitor = self.monitor()
        question, first = await self.submit(monitor)
        _, second = await self.submit(monitor, self.message(105))
        broadcast = self.result()
        self.observe(monitor, broadcast)
        self.assertFalse((self.root / "last_success.json").exists())
        exact = self.result(9000021)
        exact.reply_to.reply_to_msg_id = question.id
        self.observe(monitor, exact)
        monitor.reconcile_answer_receipts()
        self.assertEqual(monitor.rows[first]["status"], "answer_confirmed")
        self.assertEqual(monitor.rows[second]["status"], "awaiting_answer_result")
        self.assertEqual(monitor.answer_receipts[monitor.key(broadcast.chat_id, broadcast.id)]["status"], "ambiguous")

    async def test_prior_manual_button_result_prevents_new_send_without_claiming_automation(self):
        monitor = self.monitor()
        self.observe(monitor, self.result())
        question = self.message(age=10)
        _, key = await self.submit(monitor, question)
        self.assertEqual(self.client.sends, [])
        self.assertEqual(monitor.rows[key]["status"], "answer_already_observed")
        self.assertFalse((self.root / "last_success.json").exists())

    async def test_stalled_history_does_not_block_a_new_xuangu_question_push(self):
        monitor = self.monitor()
        self.client.history_release.clear()
        task = asyncio.create_task(monitor.run())
        try:
            await asyncio.wait_for(self.client.history_started.wait(), 1)
            message = self.message()
            self.client.messages[(message.chat_id, message.id)] = message
            await self.client.handlers[0][0](SimpleNamespace(message=message))
            await asyncio.wait_for(self.client.sent.wait(), 1)
            self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".作答 B", message.id)])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_flat_learned_answers_cannot_poison_a_different_question(self):
        monitor = self.monitor(bank=())
        monitor.record_learned_question(self.message().message, "B", {})
        # Learning is disabled above; a legacy unscoped file must also not act
        # as an oracle for arbitrary questions merely sharing an option word.
        monitor.learned_path.write_text(json.dumps({"account": monitor.account, "flow": monitor.flow,
                                                   "answers": ["虚天鼎"]}))
        monitor.reload_answer_bank()
        unrelated = self.message().message.replace("乾蓝冰焰最初封附在何种通天灵宝之外？", "哪件法宝不属于韩立？")
        self.assertIsNone(monitor.answer(unrelated))

    async def test_health_requires_the_loaded_question_policy_not_just_a_live_process(self):
        monitor = self.monitor()
        async def scan(*, on_scan_complete):
            on_scan_complete()
        await monitor.health.run_cycle(self.client, scan)
        proc = f"user {os.getpid()} 0 0 0 0 ?? S 00:00 0:00 python auto_reply_topic_event.py"
        def classify():
            monitor.health._write()
            return healthcheck.classify_event_monitor(monitor.account, monitor.flow, "send", self.root, proc, True, {})
        self.assertEqual(classify().severity, "ok")
        monitor.health.data["answer_policy"]["official_receipt_required"] = False
        self.assertEqual(classify().status, "monitor_answer_policy_mismatch")

    async def test_retired_fixed_answer_jobs_cannot_return_via_registry_or_legacy_sender(self):
        from flow_registry import active_entries, retired_xuangu_launchagent_labels
        from auto_reply_topic_event import run_persistent_auto_reply
        for flow in ("zuoda_a", "zuoda_dingwai_qianlanbingyan"):
            with self.subTest(flow=flow):
                with self.assertRaisesRegex(ValueError, "retired"):
                    active_entries({"flows": [{"account": "tg9100000001", "flow": flow, "kinds": ["send"]}]})
                with patch.dict(os.environ, {"TG_FLOW_SLUG": flow}), self.assertRaisesRegex(ValueError, "Retired"):
                    await run_persistent_auto_reply()
        for label in retired_xuangu_launchagent_labels():
            with self.assertRaisesRegex(ValueError, "retired"):
                active_entries({"flows": [], "helpers": [{"label": label, "enabled": True}]})


if __name__ == "__main__":
    unittest.main()
