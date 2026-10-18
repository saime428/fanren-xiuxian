"""Offline regressions for official receipts and retries across invocations."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import send_topic_message as sender
from command_confirmation import PendingCommand
from reply_parsing import AckPolicy, ReplyOutcome, classify_reply_messages
from scripts.return_deadline_dispatch import collect_unconfirmed_labels


class ReplyIdentityTests(unittest.TestCase):
    def message(self, ident, who, text, parent):
        return SimpleNamespace(id=ident, sender_id=who, message=text, fwd_from=None,
            date=datetime.fromtimestamp(1000 + ident, timezone.utc),
            reply_to=SimpleNamespace(reply_to_msg_id=parent, reply_to_top_id=7310786))

    def classify(self, reply):
        return classify_reply_messages([self.message(10, 42, '.问道', 7310786), reply],
            topic_id=7310786, own_sender_id=42, target_message='.问道', sent_message_id=10,
            reset_triggers=[], cooldown_markers=['请在'],
            policy=AckPolicy(success_markers=['问道成功'], ack_any_reply=True), log=lambda *args: None)

    def test_player_quoting_a_success_and_forwarded_bot_message_do_not_confirm(self):
        self.assertFalse(self.classify(self.message(11, 999, '问道成功', 10)).acked)
        reply = self.message(11, 8917921351, '问道成功', 10)
        reply.fwd_from = object()
        self.assertFalse(self.classify(reply).acked)

    def test_arbitrary_official_reply_does_not_count_as_success(self):
        self.assertFalse(self.classify(self.message(11, 8917921351, '你好', 10)).acked)

    def test_exact_official_success_and_cooldown_remain_distinct(self):
        self.assertTrue(self.classify(self.message(11, 8917921351, '问道成功', 10)).succeeded)
        cooldown = self.classify(self.message(12, 8917921351, '请在 20秒 后再来', 10))
        self.assertTrue(cooldown.acked)
        self.assertFalse(cooldown.succeeded)
        self.assertEqual(cooldown.cooldown_retry_seconds, 20)


class PersistentCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 10000.0
        self.sequence = 0
        self.env = patch.dict(os.environ, {'TG_API_ID': '123', 'TG_TARGET': '@example_game_group', 'TG_TOPIC_ID': '7310786',
            'TG_MESSAGE': '.问道', 'TG_FLOW_SLUG': 'wendao', 'TG_STATE_DIR': str(self.root),
            'TG_MIN_REPEAT_SECONDS': '7200', 'TG_POST_SEND_WAIT_SECONDS': '0',
            'TG_POST_SEND_MAX_WAIT_SECONDS': '0', 'XIU_ATTEMPT_LEDGER': '0'}, clear=True)
        self.env.start()
        self.cfg = sender.build_config()
        async def send(_label):
            self.sequence += 1
            return SimpleNamespace(id=self.sequence * 10)
        self.run = SimpleNamespace(cfg=self.cfg, client=object(), me_id=42, send=AsyncMock(side_effect=send),
            gather=AsyncMock(return_value=ReplyOutcome()), maybe_wait_for_cooldown_retry=AsyncMock(return_value=False))

    async def asyncTearDown(self):
        self.env.stop()
        self.temp.cleanup()

    async def cycle(self):
        with patch.object(sender.time, 'time', side_effect=lambda: self.now), patch.object(
                sender, 'collect_recent_messages', AsyncMock(return_value=[])):
            await sender._run_send_cycle(self.run)

    async def test_no_reply_retries_more_than_twice_and_late_first_reply_ends_chain(self):
        for _ in range(3):
            await self.cycle()
            self.assertFalse(self.cfg.last_success_path.exists())
            self.now += 121
        self.assertEqual(self.sequence, 3)
        async def gather(mid, **kwargs):
            if mid == 10 and self.sequence == 4:
                return ReplyOutcome(acked=True, succeeded=True, matched_ids=[10], command_message_id=10,
                    ack_reply_message_id=99, ack_reply_sender_id=8917921351, ack_reply_text='问道成功', acked_at_ts=self.now)
            return ReplyOutcome()
        self.run.gather.side_effect = gather
        await self.cycle()
        self.assertEqual(self.sequence, 4)
        self.assertEqual(json.loads(self.cfg.last_success_path.read_text())['sent_message_id'], 10)
        self.assertFalse(json.loads((self.root / 'command_pending.json').read_text())['pending'])
        await self.cycle()
        self.assertEqual(self.sequence, 4)

    async def test_success_interval_never_suppresses_unconfirmed_retry_and_restart_waits_interval(self):
        await self.cycle()
        await self.cycle()
        self.assertEqual(self.sequence, 1)
        self.now += 121
        await self.cycle()
        self.assertEqual(self.sequence, 2)
        self.assertEqual(json.loads((self.root / 'command_pending.json').read_text())['attempts'][0]['message_id'], 10)

    async def test_remaining_time_uses_the_official_timestamp_across_scheduled_runs(self):
        await self.cycle()
        receipt_ts = self.now + 2
        cooldown = ReplyOutcome(acked=True, matched_ids=[10], command_message_id=10,
            ack_reply_message_id=11, ack_reply_sender_id=8917921351, acked_at_ts=receipt_ts,
            ack_reply_text='冷却剩余 14分钟48秒。', cooldown_reply_text='冷却剩余 14分钟48秒。',
            cooldown_retry_seconds=888)
        self.run.gather.side_effect = lambda mid, **kwargs: cooldown if mid in (None, 10) else ReplyOutcome()
        self.now += 61
        await self.cycle()
        due = receipt_ts + 888 + self.cfg.cooldown_retry_buffer_seconds
        for instant in (self.now + 60, due - 1):
            self.now = instant
            await self.cycle()
            stored = json.loads(self.cfg.cooldown_state_path.read_text())[self.cfg.state_key]
            self.assertEqual(stored['not_before_ts'], due)
            self.assertEqual(self.sequence, 1)
            self.assertFalse(self.cfg.last_success_path.exists())
        self.now = due
        await self.cycle()
        self.assertEqual(self.sequence, 2)

    async def test_hundred_old_copies_do_not_starve_retry_and_recent_old_receipt_still_confirms(self):
        journal = PendingCommand(self.root, flow='wendao', command='.问道', target='@example_game_group',
                                 topic_id=7310786, actor_id=42, clock=lambda: self.now)
        journal.data.update(pending=True, first_attempt_ts=self.now - 2000, next_retry_ts=self.now - 1,
                            attempts=[{'message_id': mid, 'sent_at_ts': self.now - 2000 + mid}
                                      for mid in range(10, 1001, 10)])
        journal.save()
        self.run = sender.SendRun(self.cfg, object(), object(), 42, AckPolicy(success_markers=['问道成功']))
        self.run.send = AsyncMock(return_value=SimpleNamespace(id=1010))
        with patch.object(sender, 'fetch_message_by_id', AsyncMock(return_value=None)) as lookup, \
                patch.object(sender, 'collect_direct_replies_to_message', AsyncMock(return_value=[])) as history:
            await self.cycle()
            self.run.send.assert_awaited_once()
            self.assertLessEqual(lookup.await_count, 4)
            self.assertLessEqual(history.await_count, 4)
            pending = json.loads((self.root / 'command_pending.json').read_text())
            self.assertEqual(len(pending['attempts']), 101)
            self.assertTrue(pending['pending'])
            self.assertTrue(pending['reply_scans_by_message'])
            self.now += 121
            reply = SimpleNamespace(id=1100, sender_id=8917921351, message='问道成功', fwd_from=None,
                date=datetime.fromtimestamp(self.now, timezone.utc),
                reply_to=SimpleNamespace(reply_to_msg_id=50, reply_to_top_id=7310786))
            with patch.object(sender.time, 'time', side_effect=lambda: self.now), \
                    patch.object(sender, 'collect_recent_messages', AsyncMock(return_value=[reply])):
                await sender._run_send_cycle(self.run)
        self.run.send.assert_awaited_once()
        self.assertEqual(json.loads(self.cfg.last_success_path.read_text())['sent_message_id'], 50)
        self.assertFalse(json.loads((self.root / 'command_pending.json').read_text())['pending'])

    async def test_send_exception_is_durable_and_retries_without_marking_success(self):
        self.run.send.side_effect = TimeoutError()
        await self.cycle()
        self.assertEqual(json.loads(self.cfg.last_attempt_path.read_text())['status'], 'send_outcome_unknown')
        self.run.send.side_effect = None
        self.run.send.return_value = SimpleNamespace(id=20)
        self.now += 121
        await self.cycle()
        self.assertEqual(self.run.send.await_count, 2)
        self.assertFalse(self.cfg.last_success_path.exists())

    async def test_read_only_reconciliation_does_not_send_when_reply_is_still_missing(self):
        await self.cycle()
        self.now += 121
        with patch.dict(os.environ, {'TG_RECONCILE_ONLY': '1'}):
            await self.cycle()
        self.assertEqual(self.sequence, 1)
        self.assertTrue(json.loads((self.root / 'command_pending.json').read_text())['pending'])
        self.assertFalse(self.cfg.last_success_path.exists())

    async def test_incomplete_legacy_result_is_repaired_only_from_an_exact_official_readback(self):
        legacy = {'status': 'acknowledged', 'acked': True, 'succeeded': True,
                  'message': '.问道', 'sent_message_id': 10, 'ts': self.now - 1}
        (self.root / 'last_result.json').write_text(json.dumps(legacy))
        self.cfg.last_success_path.write_text(json.dumps(legacy))
        with patch.dict(os.environ, {'TG_RECONCILE_ONLY': '1'}):
            await self.cycle()
        self.assertEqual(json.loads(self.cfg.last_success_path.read_text()), legacy)
        async def gather(mid, **kwargs):
            return ReplyOutcome(acked=True, succeeded=True, command_message_id=10,
                ack_reply_message_id=11, ack_reply_sender_id=8917921351, acked_at_ts=self.now) if mid == 10 else ReplyOutcome()
        self.run.gather.side_effect = gather
        with patch.dict(os.environ, {'TG_RECONCILE_ONLY': '1'}):
            await self.cycle()
        repaired = json.loads(self.cfg.last_success_path.read_text())
        self.assertEqual(repaired['actor_id'], 42)
        self.assertEqual(repaired['ack_reply_message_id'], 11)
        self.assertEqual(self.sequence, 0)

    async def test_dispatcher_only_resumes_registered_enabled_pending_command(self):
        await self.cycle()
        dest = self.root / 'state/accounts/tg42/wendao'
        dest.mkdir(parents=True)
        (dest / 'command_pending.json').write_bytes((self.root / 'command_pending.json').read_bytes())
        registry = self.root / 'registry.json'
        registry.write_text(json.dumps({'flows': [{'account': 'tg42', 'flow': 'wendao', 'enabled': True, 'kinds': ['send']}]}))
        self.assertEqual(collect_unconfirmed_labels(self.root / 'state', registry_path=registry, now_ts=self.now + 121),
                         ['ai.hermes.xiuxian.tg42.wendao.send'])
        registry.write_text(json.dumps({'flows': [{'account': 'tg42', 'flow': 'wendao', 'enabled': False, 'kinds': ['send']}]}))
        self.assertEqual(collect_unconfirmed_labels(self.root / 'state', registry_path=registry, now_ts=self.now + 121), [])

    async def test_cooldown_cannot_write_success_even_when_legacy_flag_is_enabled(self):
        with patch.dict(os.environ, {'TG_COOLDOWN_COUNTS_AS_SUCCESS': '1'}):
            self.assertFalse(sender.persist_reply_state(self.cfg, {'status': 'cooldown_pending', 'acked': True,
                'ack_reply_message_id': 11, 'ts': self.now}))
        self.assertFalse(self.cfg.last_success_path.exists())


class PendingIdentityTests(unittest.TestCase):
    def test_reply_window_starts_after_gate_and_delivery_not_when_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            now = 1000
            journal = PendingCommand(directory, flow='wendao', command='.问道', target='@example_game_group',
                                     topic_id=7310786, actor_id=1, clock=lambda: now)
            journal.begin(120)
            now = 1600
            journal.dispatched(120)
            now = 1610
            journal.sent(SimpleNamespace(id=10))
            self.assertEqual(journal.data['next_retry_ts'], 1730)
            self.assertEqual(journal.data['attempts'][-1]['dispatched_at_ts'], 1600)

    def test_copy_lookup_rotation_survives_reload_without_discarding_any_id(self):
        with tempfile.TemporaryDirectory() as directory:
            now = 1000
            kwargs = dict(flow='wendao', command='.问道', target='@example_game_group', topic_id=7310786, actor_id=1)
            journal = PendingCommand(directory, **kwargs, clock=lambda: now)
            journal.data.update(pending=True, attempts=[{'message_id': mid, 'sent_at_ts': 900} for mid in range(1, 11)])
            journal.save()
            covered = set()
            for _ in range(10):
                journal = PendingCommand(directory, **kwargs, clock=lambda: now)
                selected = journal.reply_scan_ids()
                self.assertLessEqual(len(selected), 2)
                covered.update(selected)
                now += 61
            self.assertEqual(covered, set(range(1, 11)))
            self.assertEqual(journal.message_ids, list(range(1, 11)))

    def test_a_different_account_cannot_resume_the_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = PendingCommand(directory, flow='wendao', command='.问道', target='@example_game_group', topic_id=7310786, actor_id=1)
            journal.begin(120)
            with self.assertRaises(ValueError):
                PendingCommand(directory, flow='wendao', command='.问道', target='@example_game_group', topic_id=7310786, actor_id=2)


class DeletedCommandRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_receipt_readback_does_not_depend_on_forum_get_replies(self):
        own = SimpleNamespace(id=10, sender_id=42, message=".问道", fwd_from=None,
            date=datetime.fromtimestamp(1000, timezone.utc),
            reply_to=SimpleNamespace(reply_to_msg_id=7310786, reply_to_top_id=7310786))
        reply = SimpleNamespace(id=11, sender_id=8917921351, message="问道成功", fwd_from=None,
            date=datetime.fromtimestamp(1001, timezone.utc),
            reply_to=SimpleNamespace(reply_to_msg_id=10, reply_to_top_id=7310786))
        with patch.object(sender, "collect_direct_replies_to_message", AsyncMock()) as replies_api, \
                patch.object(sender, "fetch_message_by_id", AsyncMock(return_value=own)):
            result = await sender._classify_with_fetches(object(), recent=[reply], chat_id="@example_game_group",
                topic_id=7310786, own_sender_id=42, target_message=".问道", sent_message_id=10,
                scan_limit=50, reset_triggers=[], cooldown_markers=[], policy=AckPolicy(success_markers=["问道成功"]))
        self.assertTrue(result.succeeded)
        replies_api.assert_not_awaited()

    async def test_actor_bound_journal_can_match_a_receipt_after_command_cleanup(self):
        reply = SimpleNamespace(id=11, sender_id=8917921351, message="问道成功", fwd_from=None,
            date=datetime.fromtimestamp(1001, timezone.utc),
            reply_to=SimpleNamespace(reply_to_msg_id=10, reply_to_top_id=7310786))
        with patch.object(sender, "collect_direct_replies_to_message", AsyncMock(return_value=[reply])), \
                patch.object(sender, "fetch_message_by_id", AsyncMock(return_value=None)):
            kwargs = dict(recent=[], chat_id="@example_game_group", topic_id=7310786, own_sender_id=42,
                target_message=".问道", sent_message_id=10, scan_limit=50, reset_triggers=[], cooldown_markers=[],
                policy=AckPolicy(success_markers=["问道成功"]))
            unknown = await sender._classify_with_fetches(object(), **kwargs)
            self.assertFalse(unknown.acked)
            verified = await sender._classify_with_fetches(object(), **kwargs,
                sent_record={"message_id": 10, "sent_at_ts": 1000})
            self.assertTrue(verified.succeeded)

    async def test_replies_api_failure_uses_bounded_chronological_history(self):
        import transport
        async def unavailable(*args, **kwargs):
            raise ValueError("fixture getReplies unavailable")
            yield
        client = SimpleNamespace(iter_messages=unavailable, get_messages=AsyncMock(return_value=[SimpleNamespace(id=11)]))
        result = await transport.collect_direct_replies_to_message(client, "@example_game_group", 10, 40)
        self.assertEqual(result[0].id, 11)
        client.get_messages.assert_awaited_once_with("@example_game_group", min_id=10, reverse=True, limit=40)
