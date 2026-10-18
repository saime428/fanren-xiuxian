import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flow_registry import active_entries, launchagent_labels
from scripts.interval_flow_dispatch import dispatch_intervals, configured_jobs, DISPATCHER_LABEL

ACCOUNT, FLOW = 'tg9100000002', 'fumofabao_qingzhufengyunjian_shenlei'
LABEL = f'ai.hermes.xiuxian.{ACCOUNT}.{FLOW}.send'

class IntervalDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.wrapper = self.root / 'scripts' / f'send_{FLOW}_{ACCOUNT}.sh'
        self.wrapper.parent.mkdir()
        self.wrapper.write_text('#!/bin/bash\nexit 0\n')
        self.registry = {'flows': [{'account': ACCOUNT, 'flow': FLOW, 'enabled': True,
                                   'kinds': ['send'], 'interval_dispatch_seconds': 7200}]}
        self.spawn = Mock(return_value=SimpleNamespace(pid=123))

    def tick(self, now, **options):
        return dispatch_intervals(self.root, self.state, self.registry, now=now, spawn=self.spawn, **options)

    def record(self):
        return self.state / 'accounts' / ACCOUNT / FLOW / 'interval_dispatch.json'

    def receipt(self, *, due, sent_id=100, cooldown=True, **changes):
        row = {'status': 'cooldown_pending' if cooldown else 'acknowledged',
               'acked': True, 'succeeded': not cooldown, 'actor_id': int(ACCOUNT[2:]),
               'flow_slug': FLOW, 'target': '@example_game_group', 'topic_id': 7310786,
               'message': '.抚摸法宝 青竹蜂云剑（神雷版）', 'sent_message_id': sent_id,
               'ack_reply_message_id': sent_id + 1, 'ack_reply_sender_id': 8788895566,
               'next_due_ts': due, **changes}
        path = self.record().with_name('last_result.json')
        path.write_text(json.dumps(row))
        return path

    def test_official_cooldown_wakes_sender_before_two_hour_fallback(self):
        self.tick(1000)
        receipt = self.receipt(due=3268)  # 37m45s plus the sender's 3s buffer
        original = receipt.read_bytes()
        self.tick(1030)
        self.assertEqual(json.loads(self.record().read_text())['next_due_ts'], 3268)
        self.tick(3267)
        self.assertEqual(self.spawn.call_count, 1)
        self.tick(3268)
        self.assertEqual(self.spawn.call_count, 2)
        self.assertEqual(receipt.read_bytes(), original)
        # Re-reading the same overdue receipt must not dispatch every tick.
        self.tick(3298)
        self.assertEqual(self.spawn.call_count, 2)

    def test_success_after_cooldown_starts_a_fresh_two_hours(self):
        self.tick(1000)
        self.receipt(due=3268)
        self.tick(3268)
        self.receipt(due=10500, sent_id=200, cooldown=False)
        self.tick(3330)
        for now in (8200, 10468, 10499):
            self.tick(now)
        self.assertEqual(self.spawn.call_count, 2)
        self.tick(10500)
        self.assertEqual(self.spawn.call_count, 3)

    def test_overdue_cooldown_is_recovered_after_dispatcher_downtime(self):
        self.tick(1000)
        self.receipt(due=3268)
        self.tick(7000)
        self.assertEqual(self.spawn.call_count, 2)
        self.tick(7030)
        self.assertEqual(self.spawn.call_count, 2)

    def test_untrusted_receipts_cannot_change_interval(self):
        self.tick(1000)
        for change in ({'actor_id': 9100000006}, {'flow_slug': 'other'},
                       {'topic_id': 1}, {'target': '@other'}, {'acked': False},
                       {'ack_reply_sender_id': 1}, {'ack_reply_message_id': 99}):
            with self.subTest(change=change):
                self.receipt(due=1100, **change)
                self.tick(1200)
                self.assertEqual(self.spawn.call_count, 1)
                self.assertEqual(json.loads(self.record().read_text())['next_due_ts'], 8200)

    def test_invalid_official_deadlines_fail_closed(self):
        self.tick(1000)
        for due in (float('nan'), float('inf'), -1, 0, True, 'bad'):
            with self.subTest(due=due):
                self.receipt(due=due)
                self.tick(9000)
                self.assertEqual(self.spawn.call_count, 1)

    def test_missing_official_deadline_is_rejected_as_invalid(self):
        from scripts.interval_flow_dispatch import reconcile_receipt_deadline
        self.tick(1000)
        for due in (None, 'bad'):
            with self.subTest(due=due):
                self.receipt(due=due)
                with self.assertRaisesRegex(ValueError, 'invalid official result deadline'):
                    reconcile_receipt_deadline(self.record().parent, self.record(), {}, account=ACCOUNT, flow=FLOW)

    def test_failed_cooldown_spawn_retries_on_next_tick(self):
        self.tick(1000)
        self.receipt(due=3268)
        self.spawn.side_effect = OSError('fixture')
        self.tick(3268)
        self.spawn.side_effect = None
        self.tick(3298)
        self.assertEqual(self.spawn.call_count, 3)

    def test_periodic_and_retry_share_wrapper_without_resetting_interval(self):
        self.assertEqual(self.tick(1000), {LABEL})
        self.tick(1100)
        self.assertEqual(self.spawn.call_count, 1)
        self.tick(1200, retry_labels=[LABEL])
        self.assertEqual(self.spawn.call_count, 2)
        self.assertEqual(json.loads(self.record().read_text())['next_due_ts'], 8200)
        self.tick(8200)
        self.assertEqual(self.spawn.call_count, 3)
        args, kwargs = self.spawn.call_args
        self.assertEqual(args[0], ['/bin/bash', str(self.wrapper)])
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(len(kwargs['pass_fds']), 1)

    def test_real_child_retains_lock_and_prevents_retry_overlap(self):
        self.wrapper.write_text('#!/bin/bash\nsleep 0.5\n')
        children = []
        def spawn(*args, **kwargs):
            child = subprocess.Popen(*args, **kwargs); children.append(child); return child
        with patch.object(self, 'spawn', side_effect=spawn):
            self.tick(1000)
            self.tick(1001, retry_labels=[LABEL])
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].wait(timeout=5), 0)
        self.tick(1002, retry_labels=[LABEL])
        self.spawn.assert_called_once()

    def test_failed_spawn_restores_deadline_for_later_retry(self):
        self.spawn.side_effect = OSError('fixture')
        self.tick(1000)
        self.assertEqual(json.loads(self.record().read_text()), {})
        self.spawn.side_effect = None
        self.tick(1001)
        self.assertEqual(self.spawn.call_count, 2)

    def test_disabled_and_paused_never_spawn(self):
        self.registry['flows'][0]['enabled'] = False
        self.assertEqual(self.tick(1000), set())
        self.registry['flows'][0]['enabled'] = True
        with patch('scripts.interval_flow_dispatch.is_paused', return_value=(True, 'fixture')):
            self.tick(1001)
        self.spawn.assert_not_called()

    def test_bad_state_fails_only_this_job_closed(self):
        self.record().parent.mkdir(parents=True)
        for value in ['bad json', json.dumps({'account': 'tg1', 'flow': FLOW}),
                      json.dumps({'account': ACCOUNT, 'flow': FLOW, 'next_due_ts': float('nan')})]:
            self.record().write_text(value)
            self.assertEqual(self.tick(1000), {LABEL})
        self.spawn.assert_not_called()

    def test_registry_maps_health_to_actual_dispatcher_and_rejects_bad_intervals(self):
        self.assertEqual(launchagent_labels(active_entries(self.registry)), [DISPATCHER_LABEL])
        for interval in [True, 0, 30, '7200']:
            self.registry['flows'][0]['interval_dispatch_seconds'] = interval
            with self.assertRaises(ValueError):configured_jobs(self.registry)

class DispatcherIntegrationTests(unittest.TestCase):
    def test_interval_tick_runs_without_pending_and_handles_its_own_retry(self):
        from scripts import return_deadline_dispatch as dispatcher
        for pending in ([], [LABEL]):
            with tempfile.TemporaryDirectory() as tmp, \
                 patch.dict(os.environ, {'XIU_STATE_ROOT': tmp}), \
                 patch.object(dispatcher, 'collect_due_labels', return_value=[]), \
                 patch.object(dispatcher, 'collect_yuanshen_labels', return_value=[]), \
                 patch.object(dispatcher, 'collect_unconfirmed_labels', return_value=pending), \
                 patch.object(dispatcher, 'dispatch_intervals', return_value={LABEL}) as intervals, \
                 patch.object(dispatcher.subprocess, 'run') as launchctl:
                self.assertEqual(dispatcher.main(), 0)
                intervals.assert_called_once()
                self.assertEqual(intervals.call_args.kwargs['retry_labels'], pending)
                launchctl.assert_not_called()
