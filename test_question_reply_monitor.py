"""Replay split notices, short-lived pushes and failures without Telegram sends."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from telethon import types, utils

from event_monitor_runtime import EventMonitorHealth
from question_reply_monitor import (
    CANONICAL_CHAT_ID, CANONICAL_TOPIC_ID, QuestionReplyMonitor,
    message_owned_by, question_kind,
)
from scripts import xiuxian_healthcheck as healthcheck


class FakeClient:
    def __init__(self):
        self.messages = {}
        self.hidden_from_recent = set()
        self.sends = []
        self.exact_reads = []
        self.handlers = []
        self.history_started = asyncio.Event()
        self.history_release = asyncio.Event()
        self.history_release.set()
        self.sent = asyncio.Event()
        self.send_error = None
        self.disconnected = asyncio.get_running_loop().create_future()

    def is_connected(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=9100000001)

    async def get_messages(self, entity, *, ids=None, limit=None, min_id=0, reverse=False, search=None, offset_date=None, wait_time=None):
        chat = utils.get_peer_id(entity)
        if ids is not None:
            self.exact_reads.append((chat, ids))
            return self.messages.get((chat, ids))
        self.history_started.set()
        await self.history_release.wait()
        return sorted((message for (key, mid), message in self.messages.items()
                       if key == chat and mid > min_id and (min_id or search or mid not in self.hidden_from_recent)
                       and (not search or search.casefold() in message.message.casefold())
                       and (offset_date is None or message.date < offset_date)),
                      key=lambda message: message.id, reverse=not reverse)

    async def iter_messages(self, entity, *, reply_to, limit):
        for (chat, _), message in self.messages.items():
            if chat == utils.get_peer_id(entity) and getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None) == reply_to:
                yield message

    async def send_message(self, entity, text, *, reply_to):
        self.sends.append((utils.get_peer_id(entity), text, reply_to))
        if self.send_error:
            raise self.send_error
        self.sent.set()
        return SimpleNamespace(id=9000000 + len(self.sends))

    def add_event_handler(self, handler, builder):
        self.handlers.append((handler, builder))

    def remove_event_handler(self, handler, builder):
        self.handlers.remove((handler, builder))


class QuestionReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Telegram message dates have whole-second precision.
        self.now = float(int(time.time()))
        self.client = FakeClient()
        self.main = types.Channel(id=9400000004, title="fixture", photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc), megagroup=True)
        self.secondary = types.Channel(id=9400000005, title="fixture", photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc), megagroup=True)
        self.healths = []
        self.env = patch.dict(os.environ, {"TG_MESSAGE_LOG_PATH": str(self.root / "messages.jsonl")})
        self.env.start()
        async def gate(action, **_kwargs):
            return await action()
        self.gate = patch("question_reply_monitor.send_with_global_gate", side_effect=gate)
        self.gate.start()

    async def asyncTearDown(self):
        for health in self.healths:
            health.close()
        self.gate.stop()
        self.env.stop()
        self.tmp.cleanup()

    def monitor(self, flow="xuangu_zuoda", *, bank=("虚天鼎", "辟邪神雷", "元婴后"), **settings):
        account = settings.pop("account", "tg9100000001")
        health = EventMonitorHealth(self.root, account, flow, "send", ["@example_game_group", -1009400000005], 10)
        self.healths.append(health)
        return QuestionReplyMonitor(
            self.client, health, [("@example_game_group", self.main), (-1009400000005, self.secondary)],
            username=settings.pop("username", "demo_player_01"), trusted_sender_ids={8917921351, 8388633812, 8861328042, 7965897083}, flow=flow,
            state_dir=self.root, state_path=self.root / "last_success.json", max_age=10800 if flow == "jiyin_choice" else 300,
            scan_limit=100, reconcile_seconds=10, reply_text=settings.pop("reply_text", ".献上魂魄"), answer_bank=bank, clock=lambda: self.now,
            **settings,
        )

    def message(self, ident=104, text=None, *, chat=CANONICAL_CHAT_ID, sender=8388633812, age=1, entities=()):
        return SimpleNamespace(
            id=ident, sender_id=sender, chat_id=chat, entities=list(entities), fwd_from=None,
            date=datetime.fromtimestamp(self.now - age, timezone.utc),
            reply_to=SimpleNamespace(reply_to_msg_id=CANONICAL_TOPIC_ID, reply_to_top_id=None),
            message=text or ("神念直入脑海，一个苍老的声音向 @demo_player_01 提问：\n"
                             "乾蓝冰焰最初封附在何种通天灵宝之外？\n"
                             "A. 掌天瓶\nB. 虚天鼎\nC. 太乙青山\nD. 大五行幻世轮\n"
                             "你有 300秒，可以回复本消息使用 .作答 <选项>。"),
        )

    def observe(self, monitor, message):
        self.client.messages[(message.chat_id, message.id)] = message
        monitor.observe(message, message.chat_id)

    def jiyin_question(self, ident=12293165, *, username="demo_player_01", window="180 分钟", **settings):
        # Text structure and the two distinct Bot IDs come from the September
        # 12 message pair. Substitute only the recipient for isolated replay.
        return self.message(ident,
            f"@{username}！你感到一股无法抗拒的意志锁定了你的神魂！\n"
            "一个沙哑的声音在你脑海中响起：“小辈，让老夫看看你的成色...”\n\n"
            f"你必须在 {window} 内做出抉择：\n"
            "1. 回复本消息 .献上魂魄 (高风险，高回报)\n"
            "2. 回复本消息 .收敛气息 (低风险，低回报)",
            sender=settings.pop("sender", 7965897083), **settings)

    async def test_nonadjacent_official_question_is_answered_and_recorded_for_exact_owner(self):
        monitor = self.monitor()
        notice = self.message(100, "【天机异象 · 玄骨考校】 @demo_player_01", sender=8917921351, age=10)
        self.observe(monitor, notice)
        self.observe(monitor, self.message(101, "你感觉到与青竹蜂云剑的联系更加紧密。", sender=8917921351))
        self.observe(monitor, self.message(102, ".元婴状态", sender=999))
        question = self.message(104)
        self.observe(monitor, question)
        await monitor.dispatch(monitor.key(CANONICAL_CHAT_ID, 104))
        self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".作答 B", 104)])
        self.assertEqual(self.client.exact_reads, [(CANONICAL_CHAT_ID, 104)])
        self.assertEqual(monitor.rows[monitor.key(CANONICAL_CHAT_ID, 100)]["status"], "question_received")
        state = json.loads((self.root / "last_attempt.json").read_text())
        self.assertEqual(state["event_message_id"], 104)
        self.assertEqual(state["status"], "awaiting_answer_result")
        self.assertFalse((self.root / "last_success.json").exists())
        fact = json.loads((self.root / "messages.jsonl").read_text().splitlines()[-1])
        self.assertEqual(fact["event_message_id"], 104)
        self.assertEqual(fact["identity_id"], 9100000001)

    async def test_stalled_history_scan_does_not_delay_jiyin_push_reply(self):
        monitor = self.monitor("jiyin_choice")
        self.client.history_release.clear()
        task = asyncio.create_task(monitor.run())
        try:
            await asyncio.wait_for(self.client.history_started.wait(), 1)
            message = self.jiyin_question(500)
            self.client.messages[(message.chat_id, message.id)] = message
            await self.client.handlers[0][0](SimpleNamespace(message=message))
            await asyncio.wait_for(self.client.sent.wait(), 1)
            self.assertFalse(self.client.history_release.is_set())
            self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".献上魂魄", 500)])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_unknown_send_is_retained_across_restart_and_healthy_scans(self):
        monitor = self.monitor()
        question = self.message()
        self.observe(monitor, question)
        self.client.send_error = TimeoutError("fixture lost acknowledgement")
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.assertEqual(monitor.rows[key]["status"], "send_outcome_unknown")
        self.assertFalse((self.root / "last_success.json").exists())
        restarted = self.monitor()
        self.observe(restarted, question)
        await restarted.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)
        async def scan(*, on_scan_complete):
            on_scan_complete()
        await restarted.health.run_cycle(self.client, scan)
        proc = f"user {os.getpid()} 0 0 0 0 ?? S 00:00 0:00 python auto_reply_topic_event.py"
        result = healthcheck.classify_event_monitor("tg9100000001", "xuangu_zuoda", "send", self.root, proc, True, {})
        self.assertEqual(result.severity, "warn")
        self.assertEqual(result.status, "awaiting_official_reply")
        self.assertIn("unconfirmed", result.note)
        state = json.loads((self.root / "monitor_health.json").read_text())
        state["pending_command"]["retry_at_ts"] = self.now - 301
        (self.root / "monitor_health.json").write_text(json.dumps(state))
        stalled = healthcheck.classify_event_monitor("tg9100000001", "xuangu_zuoda", "send", self.root, proc, True, {})
        self.assertEqual(stalled.status, "command_retry_stalled")
        self.assertEqual(stalled.severity, "bad")

    async def test_no_answer_is_bad_and_a_later_scan_cannot_clear_it(self):
        monitor = self.monitor(bank=("unknown fixture answer",))
        question = self.message()
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.observe(monitor, question)
        self.assertEqual(monitor.rows[key]["status"], "answer_unavailable")
        self.assertEqual(monitor.health.data["business_issue"]["code"], "answer_unavailable")
        self.assertEqual(self.client.sends, [])

    async def test_ambiguous_answer_bank_never_guesses_the_first_option(self):
        import xuangu_quiz
        monitor = self.monitor(bank=("虚天鼎", "掌天瓶"))
        key = xuangu_quiz.parse_question(self.message().message)["key"]
        monitor.learned_questions[key] = ["虚天鼎", "掌天瓶"]
        self.assertIsNone(monitor.answer(self.message().message))

    async def test_deadline_is_rechecked_inside_send_gate(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question(500, window="20 秒")
        self.observe(monitor, question)
        async def delayed(action, **_kwargs):
            self.now += 21
            return await action()
        with patch("question_reply_monitor.send_with_global_gate", side_effect=delayed):
            await monitor.dispatch(monitor.key(question.chat_id, question.id))
        self.assertEqual(self.client.sends, [])
        self.assertFalse((self.root / "messages.jsonl").exists())

    async def test_jiyin_split_notice_waits_for_actual_question_from_other_trusted_bot(self):
        monitor = self.monitor("jiyin_choice")
        notice = self.message(12293161, "【天机异象 · 魔君降临】 极阴祖师注视 @demo_player_01",
                              sender=8861328042, age=64)
        self.observe(monitor, notice)
        self.assertEqual(question_kind(notice.message, monitor.flow), "notice")
        self.assertEqual(monitor.queue.qsize(), 0)
        await monitor.dispatch(monitor.key(notice.chat_id, notice.id))
        self.assertEqual(self.client.sends, [])
        self.observe(monitor, self.message(12293162, "其他玩家的普通消息", sender=999))
        question = self.jiyin_question(age=60)
        self.observe(monitor, question)
        await monitor.dispatch(monitor.key(question.chat_id, question.id))
        self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".献上魂魄", 12293165)])
        self.assertEqual(monitor.rows[monitor.key(notice.chat_id, notice.id)]["status"], "question_received")

    async def test_jiyin_question_does_not_require_the_announcement_or_npc_name(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question(age=3600)
        self.assertNotIn("极阴祖师", question.message)
        self.observe(monitor, question)
        await monitor.dispatch(monitor.key(question.chat_id, question.id))
        self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".献上魂魄", question.id)])

    async def test_jiyin_uses_the_question_window_instead_of_twenty_seconds(self):
        monitor = self.monitor("jiyin_choice")
        self.assertTrue(monitor.eligible(self.jiyin_question(age=10790), CANONICAL_CHAT_ID))
        self.assertFalse(monitor.eligible(self.jiyin_question(age=10800), CANONICAL_CHAT_ID))
        self.assertFalse(monitor.eligible(self.jiyin_question(age=31, window="30 秒"), CANONICAL_CHAT_ID))
        self.assertTrue(monitor.eligible(self.jiyin_question(age=59, window="1 分鐘"), CANONICAL_CHAT_ID))
        self.assertFalse(monitor.eligible(self.jiyin_question(age=60, window="1 分鐘"), CANONICAL_CHAT_ID))
        self.assertTrue(monitor.eligible(self.jiyin_question(age=10790, window="3 小时"), CANONICAL_CHAT_ID))

    async def test_jiyin_other_players_and_untrusted_bots_are_not_answered(self):
        monitor = self.monitor("jiyin_choice")
        for question in (self.jiyin_question(username="sample_peer_12_else"),
                         self.jiyin_question(username="demo_player_01_other"),
                         self.jiyin_question(sender=999)):
            self.observe(monitor, question)
        self.assertEqual(monitor.rows, {})
        self.assertEqual(self.client.sends, [])

    async def test_jiyin_respects_low_risk_account_policy(self):
        monitor = self.monitor("jiyin_choice", account="tg9100000003", username="demo_player_03", reply_text=".收敛气息")
        question = self.jiyin_question(username="demo_player_03")
        self.observe(monitor, question)
        await monitor.dispatch(monitor.key(question.chat_id, question.id))
        self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".收敛气息", question.id)])

    async def test_jiyin_deadline_is_not_extended_by_an_edit_or_restart(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question(window="30 秒")
        self.observe(monitor, question)
        self.now += 10
        question.message = question.message.replace("30 秒", "180 分钟")
        self.observe(monitor, question)
        restarted = self.monitor("jiyin_choice")
        self.now += 21
        await restarted.dispatch(monitor.key(question.chat_id, question.id))
        self.assertEqual(self.client.sends, [])
        self.assertEqual(restarted.rows[monitor.key(question.chat_id, question.id)]["status"], "expired_unanswered")

    async def test_jiyin_shorter_window_is_rechecked_on_exact_read(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question(age=25)
        self.observe(monitor, question)
        question.message = question.message.replace("180 分钟", "20 秒")
        await monitor.dispatch(monitor.key(question.chat_id, question.id))
        self.assertEqual(self.client.sends, [])

    async def test_jiyin_notice_edited_into_question_gets_correct_kind_and_reply(self):
        monitor = self.monitor("jiyin_choice")
        notice = self.message(500, "【天机异象 · 魔君降临】 极阴祖师注视 @demo_player_01", sender=7965897083)
        self.observe(monitor, notice)
        question = self.jiyin_question(500)
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.assertEqual(monitor.rows[key]["kind"], "question")
        self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".献上魂魄", question.id)])

    async def test_jiyin_secondary_or_other_topic_question_is_observed_without_relocation(self):
        monitor = self.monitor("jiyin_choice")
        secondary = self.jiyin_question(chat=-1009400000005)
        wrong_topic = self.jiyin_question(12293166)
        wrong_topic.reply_to.reply_to_msg_id = 12345
        for question in (secondary, wrong_topic):
            self.observe(monitor, question)
            key = monitor.key(question.chat_id, question.id)
            await monitor.dispatch(key)
            self.assertEqual(monitor.rows[key]["status"], "readonly_source")
        self.assertEqual(self.client.sends, [])
        self.assertFalse((self.root / "last_success.json").exists())

        secondary.message = "【极阴祖师·题面失效】 @demo_player_01 未能及时作答"
        monitor.observe(secondary, secondary.chat_id)
        self.assertEqual(monitor.rows[monitor.key(secondary.chat_id, secondary.id)]["status"], "readonly_source")
        self.assertIsNone(monitor.health.data["business_issue"])

    async def test_jiyin_existing_manual_choice_prevents_a_second_opposite_choice(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question()
        own = self.own_reply(question)
        own.message = ".收敛气息"
        self.observe(monitor, own)
        unrelated = self.own_reply(question, ident=9000011)
        unrelated.message = "后续普通聊天，不能覆盖已有的选择"
        self.observe(monitor, unrelated)
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.assertEqual(self.client.sends, [])
        self.assertEqual(monitor.rows[key]["status"], "awaiting_command_result")
        self.assertEqual(monitor.rows[key]["reply_text"], ".收敛气息")

    async def test_jiyin_unknown_send_remains_unknown_after_restart_without_retransmit(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question()
        self.observe(monitor, question)
        self.client.send_error = TimeoutError()
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        restarted = self.monitor("jiyin_choice")
        self.observe(restarted, question)
        await restarted.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)
        self.assertEqual(restarted.rows[key]["status"], "send_outcome_unknown")

    async def test_secondary_source_cannot_be_suppressed_by_larger_main_chat_ids(self):
        (self.root / "last_success.json").write_text(json.dumps({"handled_event_ids_by_chat": {"9400000004": 12000000}}))
        monitor = self.monitor()
        question = self.message(42, chat=-1009400000005)
        self.observe(monitor, question)
        await monitor.dispatch(monitor.key(question.chat_id, question.id))
        self.assertEqual(self.client.sends, [])
        self.assertEqual(monitor.rows[monitor.key(question.chat_id, 42)]["status"], "readonly_source")
        self.assertEqual(monitor.cursors, {"9400000004": 12000000})

    async def test_duplicate_push_and_reconciliation_do_not_send_twice(self):
        monitor = self.monitor()
        question = self.message()
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.observe(monitor, question)
        await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)

    async def test_late_expiry_edit_marks_a_sent_but_rejected_answer_bad(self):
        monitor = self.monitor()
        question = self.message()
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.now += 310
        expired = self.message(question.id, "【玄骨考校·题面失效】 @demo_player_01 未能及时作答", age=311)
        monitor.observe(expired, expired.chat_id)
        self.assertEqual(monitor.rows[key]["status"], "reply_rejected")

    async def test_untrusted_wrong_account_expired_and_foreign_chat_inputs_are_rejected(self):
        monitor = self.monitor()
        candidates = [self.message(sender=999), self.message(text=self.message().message.replace("@demo_player_01", "@demo_player_01_other")),
                      self.message(age=301), self.message(age=-31), self.message(chat=-1009400000007)]
        for candidate in candidates:
            self.observe(monitor, candidate)
        self.assertEqual(monitor.queue.qsize(), 0)
        self.assertEqual(monitor.rows, {})

    async def test_explicit_other_owner_cannot_inherit_notice_attribution(self):
        monitor = self.monitor()
        self.observe(monitor, self.message(100, "【天机异象 · 玄骨考校】 @demo_player_01", sender=8917921351))
        question = self.message(text=self.message().message.replace("@demo_player_01", "@sample_peer_12_else"))
        question.reply_to.reply_to_msg_id = 100
        self.observe(monitor, question)
        self.assertEqual(monitor.queue.qsize(), 0)

    async def test_explicit_telegram_user_id_supports_display_name_without_guessing_alias(self):
        message = self.message(text="【天机异象 · 魔君降临】 极阴祖师注视 @修士", entities=[SimpleNamespace(url="tg://user?id=9100000001")])
        self.assertTrue(message_owned_by(message, "demo_player_01", 9100000001))
        self.assertFalse(message_owned_by(message, "demo_player_01", 9100000006))
        self.assertIsNone(question_kind("【极阴的欣赏】 极阴祖师赏赐 @demo_player_01", "jiyin_choice"))

    def own_reply(self, question, ident=9000010, *, sender=9100000001):
        message = self.message(ident, ".作答 B", sender=sender, age=0)
        message.reply_to.reply_to_msg_id = question.id
        message.reply_to.reply_to_top_id = CANONICAL_TOPIC_ID
        return message

    async def test_timeout_recovers_only_from_own_exact_reply_without_resending(self):
        monitor = self.monitor()
        question = self.message()
        self.observe(monitor, question)
        self.client.send_error = TimeoutError()
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        other = self.own_reply(question, sender=999)
        monitor.observe(other, other.chat_id)
        await monitor.reconcile_unknown()
        self.assertEqual(monitor.rows[key]["status"], "send_outcome_unknown")
        own = self.own_reply(question)
        monitor.observe(own, own.chat_id)
        await monitor.reconcile_unknown()
        self.assertEqual(monitor.rows[key]["status"], "awaiting_answer_result")
        self.assertEqual(len(self.client.sends), 1)
        self.assertIsNone(monitor.health.data["business_issue"])

    async def test_secondary_question_and_root_reply_cannot_create_an_outbound_action(self):
        monitor = self.monitor()
        question = self.message(chat=-1009400000005)
        self.observe(monitor, question)
        self.client.send_error = TimeoutError()
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        own = self.own_reply(question)
        own.reply_to.reply_to_msg_id = CANONICAL_TOPIC_ID
        monitor.observe(own, own.chat_id)
        await monitor.reconcile_unknown()
        self.assertEqual(monitor.rows[key]["status"], "readonly_source")
        self.assertEqual(self.client.sends, [])
        self.assertFalse((self.root / "last_success.json").exists())

    async def test_history_finds_existing_own_answer_before_queuing_send(self):
        monitor = self.monitor()
        question = self.message()
        own = self.own_reply(question)
        self.client.messages = {(question.chat_id, question.id): question, (own.chat_id, own.id): own}
        completed = Mock()
        await monitor.scan(on_scan_complete=completed)
        await monitor.dispatch(monitor.key(question.chat_id, question.id))
        completed.assert_called_once()
        self.assertEqual(self.client.sends, [])
        self.assertEqual(monitor.rows[monitor.key(question.chat_id, question.id)]["status"], "awaiting_answer_result")

    async def test_post_send_write_failure_keeps_exact_id_for_recovery(self):
        monitor = self.monitor()
        question = self.message()
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        with patch("question_reply_monitor.MessageFactStore.append", side_effect=OSError("fixture disk error")):
            await monitor.dispatch(key)
        self.assertEqual(monitor.rows[key]["status"], "send_outcome_unknown")
        self.assertEqual(monitor.rows[key]["sent_message_id"], 9000001)
        own = self.own_reply(question, ident=9000001)
        self.client.messages[(own.chat_id, own.id)] = own
        await monitor.reconcile_unknown()
        self.assertEqual(monitor.rows[key]["status"], "awaiting_answer_result")
        self.assertEqual(len(self.client.sends), 1)

    async def test_new_notice_does_not_clear_an_unanswered_question_failure(self):
        monitor = self.monitor(bank=())
        question = self.message()
        self.observe(monitor, question)
        await monitor.dispatch(monitor.key(question.chat_id, question.id))
        self.now += 2
        self.observe(monitor, self.message(110, "【天机异象 · 玄骨考校】 @demo_player_01"))
        self.assertEqual(monitor.health.data["business_issue"]["code"], "answer_unavailable")

    async def test_pending_journal_entries_survive_retention_and_resume_after_restart(self):
        monitor = self.monitor()
        for index in range(45):
            self.observe(monitor, self.message(100 + index))
        self.assertEqual(len(monitor.rows), 45)
        restarted = self.monitor()
        restarted.tick()
        self.assertEqual(restarted.queue.qsize(), 45)
        self.assertEqual(len(restarted.rows), 45)

    async def test_late_owned_question_resolves_missing_notice(self):
        monitor = self.monitor()
        self.observe(monitor, self.message(100, "【天机异象 · 玄骨考校】 @demo_player_01", age=40))
        monitor.tick()
        self.assertEqual(monitor.rows[monitor.key(CANONICAL_CHAT_ID, 100)]["status"], "question_missing")
        self.observe(monitor, self.message())
        await monitor.dispatch(monitor.key(CANONICAL_CHAT_ID, 104))
        self.assertEqual(monitor.rows[monitor.key(CANONICAL_CHAT_ID, 100)]["status"], "question_received")
        self.assertIsNone(monitor.health.data["business_issue"])

    async def test_safe_answer_learning_follows_exact_official_reply_chain(self):
        monitor = self.monitor(bank=(), auto_update_answer_bank=True,
                               result_marker_groups=[["玄骨", "答案", "完全正"]])
        question = self.message(age=20)
        self.observe(monitor, question)
        await monitor.dispatch(monitor.key(question.chat_id, question.id))
        answer = self.own_reply(question)
        result = self.message(9000020, "【玄骨考校·答对】\n@demo_player_01 的答案 B 完全正确！", age=0)
        result.reply_to.reply_to_msg_id = answer.id
        self.client.messages[(answer.chat_id, answer.id)] = answer
        await monitor.learn_result(result, result.chat_id)
        self.assertEqual(monitor.answer(question.message), ".作答 B")
        learned = json.loads(monitor.learned_path.read_text())
        self.assertEqual(learned["last_source"]["question_message_id"], question.id)
        self.assertEqual(learned["account"], "tg9100000001")
        self.assertFalse((self.root / "xuangu_answer_bank.json").exists())
        await monitor.dispatch(monitor.key(question.chat_id, question.id))
        self.assertEqual(self.client.sends, [])
        self.assertFalse((self.root / "last_success.json").exists())

    async def test_one_failing_row_or_learning_lookup_does_not_abort_the_scan(self):
        monitor = self.monitor(bank=(), auto_update_answer_bank=True,
                               result_marker_groups=[["玄骨", "答案", "完全正"]])
        for ident in (104, 105):
            self.observe(monitor, self.message(ident))
            monitor.rows[monitor.key(CANONICAL_CHAT_ID, ident)].update(
                status="send_outcome_unknown", sent_message_id=9000000 + ident)
        unanswerable = self.message(9000020, "【玄骨考校·答对】\n@demo_player_01 的答案 B 完全正确！", age=0)
        unanswerable.reply_to.reply_to_msg_id = 4242  # a parent nobody can read
        self.client.messages[(CANONICAL_CHAT_ID, unanswerable.id)] = unanswerable
        exact_reads = []
        read = self.client.get_messages
        async def flaky(entity, **kwargs):
            if kwargs.get("ids") is not None:
                exact_reads.append(kwargs["ids"])
                if kwargs["ids"] in (9000104, 4242):
                    raise TimeoutError()
            return await read(entity, **kwargs)
        self.client.get_messages = flaky
        completed = Mock()
        await monitor.scan(on_scan_complete=completed)
        completed.assert_called_once()
        self.assertIn(9000105, exact_reads)  # the next row is still reconciled
        self.assertIn(4242, exact_reads)  # and the failing learning lookup was reached
        self.assertEqual(monitor.rows[monitor.key(CANONICAL_CHAT_ID, 104)]["receipt_lookup_error_type"], "TimeoutError")

    async def test_answer_learning_fetches_each_parent_once(self):
        monitor = self.monitor(bank=(), auto_update_answer_bank=True,
                               result_marker_groups=[["玄骨", "答案", "完全正"]])
        result = self.message(9000020, "【玄骨考校·答对】\n@demo_player_01 的答案 B 完全正确！", age=0)
        result.reply_to.reply_to_msg_id = 4242  # deleted parent: never learnable
        for _ in range(3):
            await monitor.learn_result(result, result.chat_id)
        self.assertEqual(self.client.exact_reads, [(CANONICAL_CHAT_ID, 4242)])

    async def test_answer_learning_rejects_untrusted_and_unattributed_nearby_results(self):
        monitor = self.monitor(bank=(), auto_update_answer_bank=True,
                               result_marker_groups=[["玄骨", "答案", "完全正"]])
        question = self.message()
        self.observe(monitor, question)
        untrusted = self.message(105, "【玄骨考校·答对】\n@demo_player_01 的答案 B 完全正确！", sender=999)
        untrusted.reply_to.reply_to_msg_id = question.id
        await monitor.learn_result(untrusted, untrusted.chat_id)
        adjacent = self.message(105, "【玄骨考校·答对】\n@demo_player_01 的答案 B 完全正确！")
        await monitor.learn_result(adjacent, adjacent.chat_id)
        self.assertFalse(monitor.learned_path.exists())
        self.assertIsNone(monitor.answer(question.message))

    async def test_background_scan_reloads_updated_configured_answer_bank(self):
        bank_path = self.root / "seed.json"
        monitor = self.monitor(bank=(), answer_bank_path=bank_path)
        question = self.message()
        self.observe(monitor, question)
        await monitor.dispatch(monitor.key(question.chat_id, question.id))
        bank_path.write_text(json.dumps({"answers": ["虚天鼎"]}))
        await monitor.scan(on_scan_complete=lambda: None)
        await monitor.dispatch(monitor.key(question.chat_id, question.id))
        self.assertEqual(self.client.sends, [(CANONICAL_CHAT_ID, ".作答 B", question.id)])

    async def test_dry_run_never_sends_or_records_success(self):
        monitor = self.monitor(dry_run=True)
        question = self.message()
        self.observe(monitor, question)
        await monitor.dispatch(monitor.key(question.chat_id, question.id))
        self.assertEqual(self.client.sends, [])
        self.assertFalse((self.root / "last_success.json").exists())

    async def test_tick_does_not_rewrite_state_for_an_already_unconfirmed_row(self):
        monitor = self.monitor("jiyin_choice")
        monitor.rows["k"] = {"status": "command_result_unconfirmed", "date_ts": self.now - 20_000, "text": "", "retry_at_ts": 0}
        with patch.object(monitor, "persist") as persist:
            monitor.tick()
            monitor.tick()
        self.assertEqual(persist.call_count, 0)
        self.assertEqual(monitor.rows["k"]["status"], "command_result_unconfirmed")

    async def test_account_pause_refusal_is_not_an_unknown_send_outcome(self):
        from account_lifecycle import AccountPausedError
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question()
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        self.client.send_error = AccountPausedError("account_shattered_paused:tg9100000001")
        await monitor.dispatch(key)
        row = monitor.rows[key]
        self.assertEqual((row["status"], row["error_type"]), ("retryable_error", "AccountPausedError"))
        self.assertGreaterEqual(row["retry_at_ts"], self.now + 600)

    async def test_jiyin_retry_survives_restart_and_stops_on_first_copys_official_receipt(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question()
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        first_id = monitor.rows[key]["sent_message_id"]
        self.assertFalse(monitor.state_path.exists())
        self.now += 61
        monitor = self.monitor("jiyin_choice")
        monitor.tick()
        await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 2)
        self.assertEqual(monitor.rows[key]["sent_message_ids"], [first_id, first_id + 1])
        result = self.message(first_id + 10, "@demo_player_01 获得了极阴的欣赏，赏赐修为增加 12 点。", age=0)
        result.reply_to.reply_to_msg_id = first_id
        self.observe(monitor, result)
        self.assertEqual(monitor.rows[key]["status"], "command_confirmed")
        self.now += 61
        monitor.tick()
        await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 2)
        confirmed = json.loads(monitor.state_path.read_text())
        self.assertEqual(confirmed["receipt_message_id"], result.id)
        self.assertEqual(confirmed["sent_message_id"], first_id)
        self.assertEqual(confirmed["receipt_reply_to_message_id"], first_id)
        self.assertEqual(confirmed["sent_message_ids"], [first_id, first_id + 1])

    async def test_fixed_command_explicit_rejection_never_writes_success(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question()
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        result = self.message(9000010, "你没有待处理的极阴选择。", age=0)
        result.reply_to.reply_to_msg_id = monitor.rows[key]["sent_message_id"]
        self.observe(monitor, result)
        self.assertEqual(monitor.rows[key]["status"], "command_rejected")
        self.now += 61
        monitor.tick()
        await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)
        self.assertFalse(monitor.state_path.exists())

    async def test_expired_question_is_not_replayed_and_keeps_missing_result(self):
        monitor = self.monitor()
        question = self.message()
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.now += 400
        monitor.tick()
        await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)
        self.assertEqual(monitor.rows[key]["status"], "answer_result_unconfirmed")
        self.assertFalse(monitor.state_path.exists())

    async def test_shenji_sent_is_not_success_and_retries_past_discovery_window(self):
        monitor = self.monitor("shenji_budao", reply_text=".神迹 布道")
        question = self.message(12300730, "【小世界·天降浩劫】 @demo_player_01 请速速查看 .小世界 安抚信徒！")
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.assertFalse(monitor.state_path.exists())
        self.now += 1000
        self.client.messages.pop((question.chat_id, question.id), None)
        monitor = self.monitor("shenji_budao", reply_text=".神迹 布道")
        monitor.tick()
        await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 2)
        result = self.message(9000010, "@demo_player_01 布道成功，信仰增加 100 点。", age=0)
        result.reply_to.reply_to_msg_id = monitor.rows[key]["sent_message_ids"][0]
        self.observe(monitor, result)
        self.assertEqual(monitor.rows[key]["status"], "command_confirmed")

    async def test_live_shenji_cooldown_blocks_retry_then_exact_reward_confirms(self):
        monitor = self.monitor("shenji_budao", reply_text=".神迹 布道")
        question = self.message(12300730, "【小世界·天降浩劫】 @demo_player_01 请速速查看 .小世界 安抚信徒！")
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        cooldown = self.message(9000010, "凡间方才承受神谕，需再等待 20分钟15秒。", age=0)
        cooldown.reply_to.reply_to_msg_id = monitor.rows[key]["sent_message_id"]
        self.observe(monitor, cooldown)
        self.assertEqual(monitor.rows[key]["retry_at_ts"], self.now + 1216)
        self.now += 600
        monitor.tick()
        await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)
        self.assertFalse(monitor.state_path.exists())
        success = self.message(9000011, "【神音浩荡】\n你消耗 8000 点修为，在天穹之上显化法相，传颂大道！\n凡人狂热膜拜，信仰提升至 100，稳定提升至 100！", age=0)
        success.reply_to.reply_to_msg_id = monitor.rows[key]["sent_message_id"]
        self.observe(monitor, success)
        self.assertEqual(monitor.rows[key]["status"], "command_confirmed")
        self.assertEqual(json.loads(monitor.state_path.read_text())["receipt_message_id"], success.id)

    async def test_old_reply_lookup_timeout_keeps_retry_and_lock_age_current(self):
        monitor = self.monitor("shenji_budao", reply_text=".神迹 布道")
        question = self.message(12300730, "【小世界·天降浩劫】 @demo_player_01 请速速查看 .小世界 安抚信徒！")
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.now += 601
        monitor.tick()
        leases = []
        send = self.client.send_message
        async def send_reading_lease(*args, **kwargs):
            leases.append(json.loads((self.root / "send.lock").read_text()))
            return await send(*args, **kwargs)
        self.client.send_message = send_reading_lease
        with patch("question_reply_monitor.collect_direct_replies_to_message", side_effect=TimeoutError()):
            await monitor.dispatch(key)
            await monitor.health.run_cycle(self.client, monitor.scan)
        self.assertEqual(len(self.client.sends), 2)
        self.assertFalse(monitor.state_path.exists())
        self.assertEqual(monitor.health.data["status"], "healthy")
        self.assertIsNotNone(monitor.health.data["pending_command"])
        self.assertEqual(leases[-1]["pid"], os.getpid())
        self.assertEqual(leases[-1]["started_at_ts"], self.now)
        # The released lease is removed (flow_lock), so no watchdog can age it.
        self.assertFalse((self.root / "send.lock").exists())

    async def test_growing_retry_journal_keeps_history_scan_bounded_and_rotates_copies(self):
        monitor = self.monitor("shenji_budao", reply_text=".神迹 布道")
        question = self.message(12300730, "【小世界·天降浩劫】 @demo_player_01 请速速查看 .小世界 安抚信徒！")
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        row = monitor.rows[key]
        row["sent_message_ids"] = list(range(9000001, 9000011))
        with patch("question_reply_monitor.collect_direct_replies_to_message", return_value=[]) as lookup:
            await monitor.scan(on_scan_complete=lambda: None)
            self.assertEqual(lookup.await_count, 2)
            self.now += 61
            await monitor.scan(on_scan_complete=lambda: None)
            self.assertEqual(lookup.await_count, 4)
            self.assertEqual(len({call.args[2] for call in lookup.await_args_list}), 4)
        restored = self.monitor("shenji_budao", reply_text=".神迹 布道")
        self.assertEqual(len(restored.rows[key]["sent_message_ids"]), 10)
        self.assertEqual(len(restored.rows[key]["reply_scans_by_message"]), 4)
        self.assertFalse(monitor.state_path.exists())

    async def test_expired_event_still_accepts_a_late_exact_official_result_after_restart(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question(500, window="20 秒")
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        sent_id = monitor.rows[key]["sent_message_id"]
        self.now += 90
        monitor.tick()
        self.assertEqual(monitor.rows[key]["status"], "command_result_unconfirmed")
        result = self.message(9000010, "@demo_player_01 你的献礼获得了极阴的欣赏，赏赐修为 +100。", age=0)
        result.reply_to.reply_to_msg_id = sent_id
        self.client.messages[(CANONICAL_CHAT_ID, result.id)] = result
        self.client.hidden_from_recent.add(result.id)
        restarted = self.monitor("jiyin_choice")
        await restarted.scan(on_scan_complete=lambda: None)
        self.assertEqual(restarted.rows[key]["status"], "command_confirmed")
        self.assertEqual(len(self.client.sends), 1)

    async def test_restart_recovers_real_jiyin_broadcast_from_secondary_group(self):
        # Authenticated secondary message 1105013 follows command 12302545 by
        # five seconds; no "长时间未做抉择" timeout wording is present.
        monitor = self.monitor("jiyin_choice", username="demo_player_03", account="tg9100000003", reply_text=".收敛气息")
        question = self.jiyin_question(12302541, username="demo_player_03")
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        # Retain an actor-bound pre-restart send without asking FakeClient's
        # default main-account identity to perform another account's action.
        row = monitor.rows[key]
        row.update(status="awaiting_command_result", submitted_at_ts=self.now,
                   sent_at_ts=self.now, sent_message_id=12302545, sent_message_ids=[12302545],
                   reply_text=".收敛气息", send_reply_to=question.id)
        monitor.persist()
        self.now += 5
        result = self.message(1105013, "【侥幸逃脱】\n@demo_player_03 将气息收敛到极致，如同一颗顽石。极阴祖师的神念一扫而过，并未发现任何异常。",
                              chat=-1009400000005, sender=8861328042, age=0)
        result.reply_to.reply_to_msg_id = None
        self.client.messages[(result.chat_id, result.id)] = result
        self.client.hidden_from_recent.add(result.id)
        self.now += 4 * 3600
        restarted = self.monitor("jiyin_choice", username="demo_player_03", account="tg9100000003", reply_text=".收敛气息")
        restarted.tick()
        await restarted.scan(on_scan_complete=lambda: None)
        self.assertEqual(restarted.rows[key]["status"], "command_confirmed")
        self.assertEqual(json.loads(restarted.state_path.read_text())["receipt_message_id"], 1105013)
        self.assertEqual(self.client.sends, [])

    async def test_send_slot_covers_only_the_recheck_and_the_send(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question(500)
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        self.now += 61
        monitor.tick()  # the retry path also reads copies, history and the question
        inside_slot = False
        reads = {"get_messages": self.client.get_messages, "iter_messages": self.client.iter_messages}
        def guarded(name):
            def read(*args, **kwargs):
                self.assertFalse(inside_slot, f"{name} while holding a global send slot")
                return reads[name](*args, **kwargs)
            return read
        self.client.get_messages = guarded("get_messages")
        self.client.iter_messages = guarded("iter_messages")
        async def gate(action, **_kwargs):
            nonlocal inside_slot
            inside_slot = True
            try:
                return await action()
            finally:
                inside_slot = False
        with patch("question_reply_monitor.send_with_global_gate", side_effect=gate):
            await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 2)

    async def test_reconciliation_reads_only_new_history_between_full_windows(self):
        from event_monitor_runtime import FULL_HISTORY_SECONDS, HISTORY_OVERLAP_IDS
        monitor = self.monitor("jiyin_choice")
        secondary = utils.get_peer_id(self.secondary)
        for chat, ident in ((CANONICAL_CHAT_ID, 500), (secondary, 700)):
            chatter = self.message(ident, "普通聊天", chat=chat)
            self.client.messages[(chat, ident)] = chatter
        reads, identity_checks = [], []
        history, get_me = self.client.get_messages, self.client.get_me
        async def record_history(entity, **kwargs):
            if kwargs.get("ids") is None and kwargs.get("limit") == monitor.scan_limit:
                reads.append((utils.get_peer_id(entity), kwargs.get("min_id", 0)))
            return await history(entity, **kwargs)
        async def record_get_me():
            identity_checks.append(1)
            return await get_me()
        self.client.get_messages, self.client.get_me = record_history, record_get_me
        cycle = lambda: monitor.health.run_cycle(self.client, monitor.scan)
        await cycle()
        await cycle()
        incremental = [(CANONICAL_CHAT_ID, 500 - HISTORY_OVERLAP_IDS), (secondary, 700 - HISTORY_OVERLAP_IDS)]
        full = [(CANONICAL_CHAT_ID, 0), (secondary, 0)]
        self.assertEqual(reads, full + incremental)
        self.assertEqual(len(identity_checks), 1)  # one GetUsers per verified connection
        monitor.health.history.full_scan_ts -= FULL_HISTORY_SECONDS
        await cycle()
        self.assertEqual(reads[4:], full)
        # A failed pass re-verifies the listener and re-reads the full window.
        self.client.get_messages = Mock(side_effect=ConnectionResetError())
        async def once(action, **_kwargs):
            return await action()
        with self.assertRaises(ConnectionResetError), patch("question_reply_monitor.with_telegram_retry",
                                                            side_effect=once):
            await cycle()
        self.client.get_messages = record_history
        await cycle()
        self.assertEqual(reads[6:], full)
        self.assertEqual(len(identity_checks), 2)

    async def test_pause_retains_pending_command_without_sending(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question(500)
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        (self.root / "pause_all").touch()
        await monitor.dispatch(key)
        self.assertEqual(self.client.sends, [])
        self.assertEqual(monitor.rows[key]["status"], "pending")

    async def test_delayed_reply_to_old_copy_is_recovered_outside_recent_history(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question(500)
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        sent_id = monitor.rows[key]["sent_message_id"]
        result = self.message(9000010, "@demo_player_01 你的献礼获得了极阴的欣赏，赏赐修为 +100。", age=0)
        result.reply_to.reply_to_msg_id = sent_id
        self.client.messages[(CANONICAL_CHAT_ID, result.id)] = result
        self.client.hidden_from_recent.add(result.id)
        self.now += 61
        restarted = self.monitor("jiyin_choice")
        restarted.tick()
        await restarted.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)
        self.assertEqual(restarted.rows[key]["status"], "command_confirmed")
        self.assertTrue(restarted.state_path.exists())

    async def test_same_cooldown_reply_cannot_keep_postponing_event_retry(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question(500)
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        result = self.message(9000010, "请在 2分钟 后再来。", age=0)
        result.reply_to.reply_to_msg_id = monitor.rows[key]["sent_message_id"]
        self.observe(monitor, result)
        self.now += 60
        monitor.tick()
        await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 1)
        self.now += 62
        monitor.tick()
        await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 2)
        self.assertFalse(monitor.state_path.exists())

    async def test_unclassified_official_reply_keeps_retrying_until_a_business_result(self):
        monitor = self.monitor("jiyin_choice")
        question = self.jiyin_question(500)
        self.observe(monitor, question)
        key = monitor.key(question.chat_id, question.id)
        await monitor.dispatch(key)
        result = self.message(9000010, "尚在核對指令，請稍候。", age=0)
        result.reply_to.reply_to_msg_id = monitor.rows[key]["sent_message_id"]
        self.observe(monitor, result)
        self.assertEqual(monitor.rows[key]["status"], "command_reply_unclassified")
        self.assertFalse(monitor.state_path.exists())
        self.now += 61
        monitor.tick()
        await monitor.dispatch(key)
        self.assertEqual(len(self.client.sends), 2)



if __name__ == "__main__":
    unittest.main()
