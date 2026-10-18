"""Replay real Nanlonghou message shapes without a Telegram connection."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telethon import types

from event_monitor_runtime import EventMonitorHealth
from flow_registry import active_entries
from nanlonghou_trade import parse_trade_result, trade_message_kind, validate_trade_policy
from question_reply_monitor import CANONICAL_CHAT_ID, CANONICAL_TOPIC_ID, QuestionReplyMonitor
from test_question_reply_monitor import FakeClient


# Original public notice/result supplied by the user. The deleted current
# question is NOT reconstructed: QUESTION is a real older owned prompt saved
# by the existing tg9100000003 producer (event 11837186).
NOTICE = ("【天机异象 · 强横神念】\n"
          "一股来自天南的、霸道而沉稳的强大神念扫过此界...此乃【南陇侯】本尊！\n"
          "他的神念，似乎在修士 @cncc01 的洞府附近停留了片刻...")
QUESTION = ("@demo_player_03！你感到一股无法抗拒的威压降临洞府！南陇侯的身影竟直接出现在你面前，"
            "他的目光扫过你身旁的侍妾【冰魄仙子】...\n\n"
            "“道友好福气...老夫这有两样东西，你可择一而取。或者，你也可以拒绝...”\n\n"
            "你有 10分钟 内做出抉择：\n"
            "1. 回复本消息 .交换 法宝\n2. 回复本消息 .交换 功法\n3. 回复本消息 .拒绝交易")
RESULT = ("【天机异闻·南陇侯的交易】\n"
          "道友 @cncc01 经过深思熟虑，选择将侍妾【冰凤】与南陇侯交换！\n"
          "作为回报，南陇侯赐予了其一件至宝：【青竹蜂云剑】！")


class NanlonghouParserTests(unittest.TestCase):
    def test_supplied_links_are_notice_player_choice_and_result(self):
        self.assertEqual(trade_message_kind(NOTICE), "notice")
        self.assertIsNone(trade_message_kind(".交换 法宝"))
        self.assertEqual(trade_message_kind(RESULT), "trade_result")
        self.assertEqual(parse_trade_result(RESULT), {
            "username": "cncc01", "companion": "冰凤", "reward": "青竹蜂云剑", "command": ".交换 法宝"})
        self.assertEqual(trade_message_kind(QUESTION), "question")

    def test_result_requires_complete_server_template(self):
        self.assertIsNone(parse_trade_result("@cncc01 南陇侯 交换 冰凤 青竹蜂云剑"))
        self.assertIsNone(parse_trade_result(RESULT + "\n道友 @sample_peer_12_else"))

    def test_registry_disallows_two_enabled_exchange_choices_for_one_account(self):
        first = {"account": "tg123", "flow": "jiaohuan_fabao", "kinds": ["send"], "enabled": True}
        second = {**first, "flow": "jiaohuan_gongfa"}
        with self.assertRaises(ValueError):
            active_entries({"flows": [first, second]})
        second["enabled"] = False
        self.assertEqual(len(active_entries({"flows": [first, second]})), 1)
        validate_trade_policy({"flows": [first, second]}, "tg123", "jiaohuan_fabao", ".交换 法宝")
        with self.assertRaises(ValueError):
            validate_trade_policy({"flows": [first, second]}, "tg123", "jiaohuan_gongfa", ".交换 功法")
        with self.assertRaises(ValueError):
            validate_trade_policy({"flows": [first, second]}, "tg123", "jiaohuan_fabao", ".交换 功法")

    def test_only_demo_player_01_may_choose_gongfa(self):
        def entry(account, flow):
            return {"account": account, "flow": flow, "kinds": ["send"], "enabled": True}
        main = entry("tg9100000001", "jiaohuan_gongfa")
        validate_trade_policy({"flows": [main]}, main["account"], main["flow"], ".交换 功法")
        for invalid in (entry("tg9100000001", "jiaohuan_fabao"), entry("tg9100000006", "jiaohuan_gongfa")):
            with self.subTest(entry=invalid), self.assertRaises(ValueError):
                active_entries({"flows": [invalid]})


class NanlonghouMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = float(int(time.time()))
        self.client = FakeClient()
        self.main = types.Channel(id=9400000004, title="fixture", photo=types.ChatPhotoEmpty(),
                                  date=datetime.now(timezone.utc), megagroup=True)
        self.secondary = types.Channel(id=9400000005, title="fixture", photo=types.ChatPhotoEmpty(),
                                       date=datetime.now(timezone.utc), megagroup=True)
        self.healths = []
        self.env = patch.dict(os.environ, {"TG_MESSAGE_LOG_PATH": str(self.root / "messages.jsonl")})
        self.env.start()
        async def gate(action, **kwargs):
            return await action()
        self.gate = patch("question_reply_monitor.send_with_global_gate", side_effect=gate)
        self.gate.start()

    async def asyncTearDown(self):
        for health in self.healths:
            health.close()
        self.gate.stop()
        self.env.stop()
        self.tmp.cleanup()

    def monitor(self, flow="jiaohuan_fabao", *, max_age=600):
        health = EventMonitorHealth(self.root, "tg9100000003", flow, "send",
                                    ["@example_game_group", -1009400000005], 10)
        self.healths.append(health)
        async def get_me():
            return SimpleNamespace(id=9100000003, username="demo_player_03")
        self.client.get_me = get_me
        return QuestionReplyMonitor(
            self.client, health, [("@example_game_group", self.main), (-1009400000005, self.secondary)],
            username="demo_player_03", trusted_sender_ids={8917921351, 7965897083, 8980154525, 8388633812},
            flow=flow, state_dir=self.root, state_path=self.root / "last_success.json", max_age=max_age,
            scan_limit=300, reconcile_seconds=10,
            reply_text=".交换 法宝" if flow == "jiaohuan_fabao" else ".交换 功法", clock=lambda: self.now)

    def message(self, ident=12293614, text=QUESTION, *, sender=7965897083, chat=CANONICAL_CHAT_ID, age=1, parent=CANONICAL_TOPIC_ID):
        return SimpleNamespace(id=ident, message=text, sender_id=sender, chat_id=chat,
                               date=datetime.fromtimestamp(self.now-age, timezone.utc), entities=[], fwd_from=None,
                               reply_to=SimpleNamespace(reply_to_msg_id=parent,
                                                        reply_to_top_id=CANONICAL_TOPIC_ID if chat == CANONICAL_CHAT_ID else None))

    def observe(self, monitor, message):
        self.client.messages[(message.chat_id, message.id)] = message
        monitor.observe(message, message.chat_id)
        return monitor.key(message.chat_id, message.id)

    def receipt(self, ident=1103237, **kwargs):
        return self.message(ident, RESULT.replace("@cncc01", "@demo_player_03").replace("【冰凤】", "【冰魄仙子】"),
                            sender=8980154525, chat=-1009400000005, parent=None, age=0, **kwargs)

    async def test_notice_waits_for_nonadjacent_question_from_another_bot(self):
        monitor = self.monitor()
        notice = self.message(12293612, NOTICE.replace("@cncc01", "@demo_player_03"), sender=8917921351, age=4)
        notice_key = self.observe(monitor, notice)
        await monitor.dispatch(notice_key)
        self.assertEqual(self.client.sends, [])
        self.assertEqual(monitor.queue.qsize(), 0)
        self.observe(monitor, self.message(12293613, "与交易无关的聊天", sender=999))
        question = self.message()
        key = self.observe(monitor, question)
        await monitor.dispatch(key)
        self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".交换 法宝", question.id)])
        self.assertEqual(self.client.exact_reads, [(CANONICAL_CHAT_ID, question.id)])
        self.assertEqual(monitor.rows[notice_key]["status"], "question_received")
        self.assertEqual(monitor.rows[key]["status"], "awaiting_trade_result")
        self.assertFalse((self.root / "last_success.json").exists())

    async def test_real_other_player_inputs_never_trigger_our_accounts(self):
        monitor = self.monitor()
        for message in (self.message(12293612, NOTICE, sender=8917921351),
                        self.message(12293621, ".交换 法宝", sender=8013818582, parent=12293614),
                        self.message(1103237, RESULT, sender=8980154525, chat=-1009400000005, parent=None)):
            self.observe(monitor, message)
        self.assertEqual(monitor.rows, {})
        self.assertEqual(monitor.trade_receipts, {})
        self.assertEqual(self.client.sends, [])

    async def test_exact_case_insensitive_owner_and_no_prefix_match(self):
        monitor = self.monitor()
        self.assertTrue(monitor.eligible(self.message(text=QUESTION.replace("@demo_player_03", "@demo_player_03")), CANONICAL_CHAT_ID))
        self.assertFalse(monitor.eligible(self.message(text=QUESTION.replace("@demo_player_03", "@demo_player_03_other")), CANONICAL_CHAT_ID))

    async def test_ten_minute_limit_also_caps_the_old_fifteen_minute_setting(self):
        monitor = self.monitor(max_age=900)
        self.assertTrue(monitor.eligible(self.message(age=590), CANONICAL_CHAT_ID))
        self.assertFalse(monitor.eligible(self.message(age=600), CANONICAL_CHAT_ID))
        self.assertFalse(monitor.eligible(self.message(age=31, text=QUESTION.replace("10分钟", "30秒")), CANONICAL_CHAT_ID))

    async def test_gongfa_uses_its_configured_offered_option(self):
        monitor = self.monitor("jiaohuan_gongfa")
        key = self.observe(monitor, self.message())
        await monitor.dispatch(key)
        self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".交换 功法", 12293614)])

    async def test_an_unavailable_option_is_not_guessed_or_sent(self):
        monitor = self.monitor()
        key = self.observe(monitor, self.message(text=QUESTION.replace("1. 回复本消息 .交换 法宝\n", "")))
        await monitor.dispatch(key)
        self.assertEqual(self.client.sends, [])
        self.assertEqual(monitor.rows[key]["status"], "answer_unavailable")

    async def test_secondary_question_is_not_relocated_to_main_topic(self):
        monitor = self.monitor()
        key = self.observe(monitor, self.message(chat=-1009400000005, parent=None))
        await monitor.dispatch(key)
        self.assertEqual(monitor.rows[key]["status"], "readonly_source")
        self.assertEqual(self.client.sends, [])

    async def test_gate_delay_cannot_send_an_expired_question(self):
        monitor = self.monitor()
        key = self.observe(monitor, self.message(age=590))
        async def delayed(action, **kwargs):
            self.now += 11
            return await action()
        with patch("question_reply_monitor.send_with_global_gate", side_effect=delayed):
            await monitor.dispatch(key)
        self.assertEqual(self.client.sends, [])

    async def test_send_timeout_survives_restart_without_retrying_before_confirmation_window(self):
        monitor = self.monitor()
        question = self.message()
        key = self.observe(monitor, question)
        self.client.send_error = TimeoutError()
        await monitor.dispatch(key)
        restarted = self.monitor()
        self.observe(restarted, question)
        await restarted.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)
        self.assertEqual(restarted.rows[key]["status"], "send_outcome_unknown")

    async def test_old_exact_receipt_reconciles_after_long_shutdown_without_resending(self):
        monitor = self.monitor()
        key = self.observe(monitor, self.message())
        await monitor.dispatch(key)
        result = self.receipt()
        result.chat_id = CANONICAL_CHAT_ID
        result.reply_to.reply_to_msg_id = monitor.rows[key]["sent_message_id"]
        self.now += 2 * 86400
        restarted = self.monitor()
        restarted.tick()
        self.assertEqual(restarted.rows[key]["status"], "trade_result_unconfirmed")
        self.observe(restarted, result)
        await restarted.dispatch(key)
        self.assertEqual(restarted.rows[key]["status"], "trade_confirmed")
        self.assertEqual(len(self.client.sends), 1)
        self.assertEqual(json.loads(restarted.state_path.read_text())["receipt_message_id"], result.id)

    async def test_receipt_replying_to_another_action_does_not_confirm_or_suppress_our_trade(self):
        monitor = self.monitor()
        result = self.receipt()
        result.chat_id = CANONICAL_CHAT_ID
        result.reply_to.reply_to_msg_id = 123456
        self.observe(monitor, result)
        key = self.observe(monitor, self.message())
        await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)
        self.now += 1
        result.id += 1
        result.date = datetime.fromtimestamp(self.now, timezone.utc)
        self.observe(monitor, result)
        self.assertEqual(monitor.rows[key]["status"], "awaiting_trade_result")
        self.assertFalse(monitor.state_path.exists())

    async def test_cross_group_result_requires_owner_companion_choice_and_unique_pending_trade(self):
        monitor = self.monitor()
        key = self.observe(monitor, self.message())
        await monitor.dispatch(key)
        self.now += 1
        result = self.receipt()
        self.observe(monitor, result)
        self.assertEqual(monitor.rows[key]["status"], "trade_confirmed")
        state = json.loads((self.root / "last_success.json").read_text())
        self.assertEqual(state["trade_reward"], "青竹蜂云剑")
        self.assertEqual(state["receipt_message_id"], 1103237)
        self.assertEqual(state["receipt_chat_id"], -1009400000005)
        self.observe(monitor, result)
        self.observe(monitor, self.message())
        await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)

    async def test_receipt_alone_or_for_different_companion_never_claims_our_action_succeeded(self):
        monitor = self.monitor()
        self.observe(monitor, self.receipt())
        self.assertFalse((self.root / "last_success.json").exists())
        key = self.observe(monitor, self.message())
        await monitor.dispatch(key)
        self.assertEqual(monitor.rows[key]["status"], "trade_already_observed")
        self.assertEqual(self.client.sends, [])
        self.assertFalse((self.root / "last_success.json").exists())
        self.now += 2
        key = self.observe(monitor, self.message(12293624, age=0))
        await monitor.dispatch(key)
        self.now += 1
        wrong = self.receipt(1103238)
        wrong.message = wrong.message.replace("【冰魄仙子】", "【另一侍妾】")
        self.observe(monitor, wrong)
        self.assertEqual(monitor.rows[key]["status"], "awaiting_trade_result")

    async def test_forwarded_question_and_result_are_not_original_business_evidence(self):
        monitor = self.monitor()
        question = self.message()
        question.fwd_from = SimpleNamespace(date=question.date)
        self.observe(monitor, question)
        result = self.receipt()
        result.fwd_from = SimpleNamespace(date=result.date)
        self.observe(monitor, result)
        self.assertEqual(monitor.rows, {})
        self.assertEqual(monitor.trade_receipts, {})

    async def test_untrusted_wrong_owner_and_wrong_choice_receipts_cannot_confirm_trade(self):
        monitor = self.monitor()
        key = self.observe(monitor, self.message())
        await monitor.dispatch(key)
        self.now += 1
        for index, variant in enumerate(("untrusted", "other_owner", "other_choice", "foreign_chat")):
            result = self.receipt(1103240 + index)
            if variant == "untrusted": result.sender_id = 999
            if variant == "other_owner": result.message = result.message.replace("@demo_player_03", "@demo_player_03_extra")
            if variant == "other_choice": result.message = result.message.replace("一件至宝", "一部功法")
            if variant == "foreign_chat": result.chat_id = -1009400000007
            self.observe(monitor, result)
        self.assertEqual(monitor.rows[key]["status"], "awaiting_trade_result")
        self.assertFalse((self.root / "last_success.json").exists())

    async def test_stalled_history_scan_does_not_delay_a_new_trade_question(self):
        monitor = self.monitor()
        self.client.history_release.clear()
        task = asyncio.create_task(monitor.run())
        try:
            await asyncio.wait_for(self.client.history_started.wait(), 1)
            question = self.message()
            self.client.messages[(question.chat_id, question.id)] = question
            await self.client.handlers[0][0](SimpleNamespace(message=question))
            await asyncio.wait_for(self.client.sent.wait(), 1)
            self.assertFalse(self.client.history_release.is_set())
            self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".交换 法宝", question.id)])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_legacy_sender_cannot_bypass_trade_journal(self):
        import auto_reply_topic_event as legacy
        with (patch.dict(os.environ, {"TG_FLOW_SLUG": "jiaohuan_fabao"}, clear=True),
              patch.object(legacy, "load_dotenv"), patch.object(legacy, "telegram_client") as client,
              self.assertRaises(ValueError)):
            await legacy.run_once()
        client.assert_not_called()

    async def test_health_verifies_loaded_trade_command_deadline_and_receipt_requirement(self):
        from scripts import xiuxian_healthcheck as healthcheck
        monitor = self.monitor()
        async def scan(*, on_scan_complete):
            on_scan_complete()
        await monitor.health.run_cycle(self.client, scan)
        proc = f"user {os.getpid()} 0 0 python3 auto_reply_topic_event.py"
        def classify():
            return healthcheck.classify_event_monitor("tg9100000003", "jiaohuan_fabao", "send", self.root, proc, True, {})
        self.assertEqual(classify().severity, "ok")
        correct = dict(monitor.health.data["trade_policy"])
        for invalid in ({"reply_text": ".交换 功法"}, {"max_event_age_seconds": 900}, {"official_receipt_required": False}):
            monitor.health.data["trade_policy"] = {**correct, **invalid}
            monitor.health._write()
            self.assertEqual(classify().status, "monitor_trade_policy_mismatch")

    async def test_ambiguous_broadcast_is_not_assigned_by_message_proximity(self):
        monitor = self.monitor()
        keys = []
        for ident in (12293614, 12293620):
            key = self.observe(monitor, self.message(ident))
            await monitor.dispatch(key)
            keys.append(key)
        self.now += 1
        self.observe(monitor, self.receipt())
        self.assertTrue(all(monitor.rows[key]["status"] == "awaiting_trade_result" for key in keys))
        self.assertFalse((self.root / "last_success.json").exists())

    async def test_unconfirmed_business_result_survives_healthy_heartbeat_and_restart(self):
        monitor = self.monitor()
        key = self.observe(monitor, self.message())
        await monitor.dispatch(key)
        self.now += 700
        restarted = self.monitor()
        restarted.tick()
        self.assertEqual(restarted.rows[key]["status"], "trade_result_unconfirmed")
        self.assertEqual(restarted.health.data["business_issue"]["code"], "trade_result_unconfirmed")
        self.assertFalse((self.root / "last_success.json").exists())
        delayed = self.receipt()
        delayed.date = datetime.fromtimestamp(self.now - 695, timezone.utc)
        self.observe(restarted, delayed)
        self.assertEqual(restarted.rows[key]["status"], "trade_confirmed")
        self.assertIsNone(restarted.health.data["business_issue"])
        self.assertEqual(len(self.client.sends), 1)

    async def test_existing_manual_refusal_prevents_exchange_even_if_question_was_deleted(self):
        monitor = self.monitor()
        question = self.message()
        key = self.observe(monitor, question)
        own = self.message(12293621, ".拒绝交易", sender=9100000003, parent=question.id, age=0)
        self.observe(monitor, own)
        self.client.messages.pop((question.chat_id, question.id))
        await monitor.dispatch(key)
        self.assertEqual(self.client.sends, [])
        self.assertEqual(monitor.rows[key]["status"], "already_replied")
        self.assertEqual(monitor.rows[key]["reply_text"], ".拒绝交易")
        self.assertFalse((self.root / "last_success.json").exists())

    async def test_business_receipt_before_telegram_ack_is_not_downgraded_by_send_timeout(self):
        monitor = self.monitor()
        key = self.observe(monitor, self.message())
        async def send(entity, text, *, reply_to):
            self.client.sends.append((CANONICAL_CHAT_ID, text, reply_to))
            self.now += 1
            self.observe(monitor, self.receipt())
            raise TimeoutError()
        self.client.send_message = send
        await monitor.dispatch(key)
        self.assertEqual(monitor.rows[key]["status"], "trade_confirmed")
        self.assertIsNone(monitor.health.data["business_issue"])
        self.assertEqual(len(self.client.sends), 1)


if __name__ == "__main__":
    unittest.main()
