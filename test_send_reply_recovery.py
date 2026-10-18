import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import send_topic_message as sender
from reply_parsing import ReplyOutcome


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def time(self):
        return 10000.0 + self.elapsed

    def monotonic(self):
        return self.elapsed

    async def sleep(self, delay):
        self.elapsed += delay


class SendReplyRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.env_patch = patch.dict(os.environ, {
            'TG_API_ID': '123', 'TG_TARGET': '@example_game_group', 'TG_TOPIC_ID': '7310786',
            'TG_MESSAGE': '.抚摸法宝 玄天斩灵剑', 'TG_FLOW_SLUG': 'fumofabao',
            'TG_STATE_DIR': str(self.root), 'TG_MIN_REPEAT_SECONDS': '0',
            'TG_POST_SEND_WAIT_SECONDS': '0', 'TG_POST_SEND_MAX_WAIT_SECONDS': '120',
            'TG_POST_SEND_POLL_SECONDS': '5', 'TG_DISABLE_IMMEDIATE_RESEND': '1',
            'TG_FAILURE_RESEND_MAX': '1', 'TG_COOLDOWN_COUNTS_AS_SUCCESS': '0',
            'XIU_ATTEMPT_LEDGER': '0',
        }, clear=True)
        self.env_patch.start()
        self.cfg = sender.build_config()
        self.run = SimpleNamespace(
            cfg=self.cfg, client=object(), me_id=9100000001,
            send=AsyncMock(side_effect=[SimpleNamespace(id=100), SimpleNamespace(id=200)]),
            gather=AsyncMock(), inline_wait_budget_seconds=lambda: 0,
            maybe_wait_for_cooldown_retry=AsyncMock(return_value=True),
        )

    async def asyncTearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    def reply(self, sent_id, *, failed=False, cooldown=None, ts=None):
        return ReplyOutcome(
            matched_ids=[sent_id], acked=True, failed=failed,
            succeeded=not failed and cooldown is None,
            terminal_status='failed' if failed else ('success' if cooldown is None else ''),
            ack_reply_message_id=sent_id + 1, acked_at_ts=self.clock.time() if ts is None else ts,
            ack_reply_sender_id=8917921351,
            ack_reply_text='没有这件拥有器灵的法宝' if failed else '器灵传来了喜悦的情绪',
            cooldown_retry_seconds=cooldown,
        )

    async def cycle(self):
        with (
            patch.object(sender, 'time', self.clock),
            patch.object(sender, 'should_stop_before_send', return_value=False),
            patch.object(sender, 'collect_recent_messages', AsyncMock(return_value=[])),
            patch.object(sender.asyncio, 'sleep', side_effect=self.clock.sleep),
        ):
            await sender._run_send_cycle(self.run)

    async def test_late_explicit_failure_is_not_success_or_a_new_success_guard(self):
        self.cfg.min_repeat_seconds = 7200
        self.cfg.post_send_max_wait_seconds = 0
        self.cfg.require_ack_reply = True
        self.cfg.last_attempt_path.write_text(json.dumps({
            'sent_message_id': 10, 'status': 'sent_no_ack', 'ts': 9990, 'actual_send_ts': 9990,
        }))

        async def gather(sent_id, **_kwargs):
            return self.reply(10, failed=True, ts=9995) if sent_id == 10 else ReplyOutcome()

        self.run.gather.side_effect = gather
        await self.cycle()
        self.run.send.assert_awaited_once()
        self.assertFalse(self.cfg.last_success_path.exists())
        self.assertEqual(json.loads(self.cfg.last_attempt_path.read_text())['status'], 'reply_retry_pending')
        self.assertTrue(json.loads((self.root / 'command_pending.json').read_text())['pending'])

    async def test_pre_send_explicit_failure_does_not_start_success_guard(self):
        self.cfg.min_repeat_seconds = 7200
        self.cfg.post_send_max_wait_seconds = 0

        async def gather(sent_id, **_kwargs):
            return self.reply(10, failed=True, ts=9995) if sent_id is None else ReplyOutcome()

        self.run.gather.side_effect = gather
        await self.cycle()
        self.run.send.assert_awaited_once()
        self.assertFalse(self.cfg.last_success_path.exists())

    async def test_failed_recovery_preserves_previous_genuine_success(self):
        self.cfg.min_repeat_seconds = 7200
        self.cfg.post_send_max_wait_seconds = 0
        previous = {'status': 'acknowledged', 'ts': 1, 'sent_message_id': 2}
        self.cfg.last_success_path.write_text(json.dumps(previous))
        self.run.gather.side_effect = lambda sent_id, **_kwargs: (
            self.reply(10, failed=True, ts=9995) if sent_id is None else ReplyOutcome())
        await self.cycle()
        self.assertEqual(json.loads(self.cfg.last_success_path.read_text()), previous)
        self.run.send.assert_awaited_once()

    async def test_real_recovered_success_still_prevents_duplicate_send(self):
        self.cfg.min_repeat_seconds = 7200
        self.run.gather.return_value = self.reply(10, ts=9995)
        await self.cycle()
        self.run.send.assert_not_awaited()
        success = json.loads(self.cfg.last_success_path.read_text())
        self.assertEqual(success['status'], 'acknowledged')
        self.assertEqual(success['ack_reply_message_id'], 11)

    async def test_recovered_cooldown_respects_no_success_policy(self):
        self.cfg.post_send_max_wait_seconds = 0
        for pending in (False, True):
            with self.subTest(pending=pending):
                for path in (self.cfg.last_attempt_path, self.cfg.last_success_path):
                    path.unlink(missing_ok=True)
                if pending:
                    self.cfg.last_attempt_path.write_text(json.dumps({'sent_message_id': 10, 'status': 'sent_no_ack'}))
                self.run.send = AsyncMock(return_value=SimpleNamespace(id=100))
                self.run.gather.side_effect = lambda sent_id, **_kwargs: (
                    self.reply(10, cooldown=0, ts=9995)
                    if sent_id == (10 if pending else None) else ReplyOutcome())
                await self.cycle()
                self.assertFalse(self.cfg.last_success_path.exists())

    async def test_interval_dispatch_uses_sender_cooldown_then_success_deadlines(self):
        from scripts.interval_flow_dispatch import dispatch_intervals

        account, flow = 'tg9100000001', self.cfg.flow_slug
        state_root = self.root / 'state'
        directory = state_root / 'accounts' / account / flow
        directory.mkdir(parents=True)
        self.cfg.state_dir = directory
        self.cfg.last_attempt_path = directory / 'last_attempt.json'
        self.cfg.last_success_path = directory / 'last_success.json'
        self.cfg.cooldown_state_path = directory / 'cooldown_retry.json'
        self.cfg.min_repeat_seconds = 7200
        wrapper = self.root / 'scripts' / f'send_{flow}_{account}.sh'
        wrapper.parent.mkdir()
        wrapper.write_text('#!/bin/bash\nexit 0\n')
        registry = {'flows': [{'account': account, 'flow': flow, 'enabled': True,
                              'kinds': ['send'], 'interval_dispatch_seconds': 7200}]}
        spawn = Mock(return_value=SimpleNamespace(pid=123))
        def tick(now):
            dispatch_intervals(self.root, state_root, registry, now=now, spawn=spawn)

        tick(10000)
        self.run.gather.side_effect = lambda sent_id, **kw: (
            self.reply(100, cooldown=2265, ts=10000) if sent_id == 100 else ReplyOutcome())
        await self.cycle()
        self.assertFalse(json.loads((directory / 'command_pending.json').read_text())['pending'])
        self.assertFalse(self.cfg.last_success_path.exists())
        tick(12267)
        self.assertEqual(spawn.call_count, 1)
        tick(12268)
        self.assertEqual(spawn.call_count, 2)

        self.clock.elapsed = 2268
        self.run.gather.side_effect = lambda sent_id, **kw: (
            self.reply(100, cooldown=2265, ts=10000) if sent_id is None
            else self.reply(200) if sent_id == 200 else ReplyOutcome())
        await self.cycle()
        self.assertEqual(self.run.send.await_count, 2)
        self.assertTrue(json.loads(self.cfg.last_success_path.read_text())['succeeded'])
        tick(12298)
        tick(19467)
        self.assertEqual(spawn.call_count, 2)
        tick(19468)
        self.assertEqual(spawn.call_count, 3)

    async def test_failure_resend_waits_for_its_delayed_success(self):
        async def gather(sent_id, **_kwargs):
            if sent_id == 100:
                return self.reply(100, failed=True, ts=10000)
            if sent_id == 200 and self.clock.elapsed >= 15:
                return self.reply(200)
            return ReplyOutcome()

        self.run.gather.side_effect = gather
        await self.cycle()
        self.assertGreaterEqual(self.clock.elapsed, 15)
        self.assertEqual(self.run.send.await_count, 2)
        success = json.loads(self.cfg.last_success_path.read_text())
        self.assertEqual(success['sent_message_id'], 200)
        self.assertEqual(success['ack_reply_message_id'], 201)

    async def test_failure_resend_gets_entire_window_when_reply_is_missing(self):
        self.run.gather.side_effect = lambda sent_id, **_kwargs: (
            self.reply(100, failed=True, ts=10000) if sent_id == 100 else ReplyOutcome())
        await self.cycle()
        self.assertEqual(self.clock.elapsed, 120)
        self.assertEqual(self.run.send.await_count, 2)
        state = json.loads(self.cfg.last_attempt_path.read_text())
        self.assertEqual(state['status'], 'reply_retry_pending')
        self.assertFalse(state['acked'])
        self.assertEqual(state['sent_message_id'], 200)
        journal = json.loads((self.root / 'command_pending.json').read_text())
        self.assertEqual(journal['attempts'][0]['message_id'], 100)
        self.assertTrue(journal['attempts'][0]['resolved'])
        self.assertEqual(journal['attempts'][1]['message_id'], 200)
        self.assertTrue(journal['pending'])
        self.assertFalse(self.cfg.last_success_path.exists())

    async def test_only_new_failure_can_exhaust_resend_limit(self):
        async def gather(sent_id, **_kwargs):
            if sent_id == 100:
                return self.reply(100, failed=True, ts=10000)
            if sent_id == 200 and self.clock.elapsed >= 10:
                return self.reply(200, failed=True)
            return ReplyOutcome()

        self.run.gather.side_effect = gather
        await self.cycle()
        self.assertEqual(self.clock.elapsed, 10)
        self.assertEqual(self.run.send.await_count, 2)
        state = json.loads(self.cfg.last_attempt_path.read_text())
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['ack_reply_message_id'], 201)
        self.assertFalse(self.cfg.last_success_path.exists())

    async def test_late_original_success_after_unacknowledged_resend_is_preserved(self):
        self.cfg.disable_immediate_resend = False
        self.cfg.post_send_max_wait_seconds = 10

        async def gather(sent_id, **_kwargs):
            if sent_id == 100 and self.run.send.await_count == 2:
                return self.reply(100)
            return ReplyOutcome()

        self.run.gather.side_effect = gather
        await self.cycle()
        self.assertEqual(self.run.send.await_count, 1)
        self.clock.elapsed = 121
        await self.cycle()
        self.assertEqual(self.run.send.await_count, 2)
        self.assertEqual(self.clock.elapsed, 121)
        success = json.loads(self.cfg.last_success_path.read_text())
        self.assertEqual(success['ack_reply_message_id'], 101)
        self.assertEqual(success['sent_message_id'], 100)
        self.assertEqual(success['command_message_ids'], [100, 200])


if __name__ == '__main__':
    unittest.main()
