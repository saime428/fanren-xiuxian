"""Account-matched official deadlines must survive scheduler interval resets."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from scripts import return_deadline_dispatch as dispatch


class PeriodicReceiptDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = self.root / 'active.json'
        self.entries = []
        self.helpers = []

    def add_flow(self, flow='fumofabao_qingzhufengyunjian_shenlei', account='tg9100000002',
                 due=1000, status='cooldown_pending', **entry_options):
        entry = {'account': account, 'flow': flow, 'enabled': True, 'kinds': ['send'], **entry_options}
        self.entries.append(entry)
        directory = self.root / 'accounts' / account / flow
        directory.mkdir(parents=True, exist_ok=True)
        result = {'status': status, 'acked': True, 'actor_id': int(account[2:]), 'flow_slug': flow,
                  'target': '@example_game_group', 'topic_id': 7310786,
                  'message': dispatch.PERIODIC_COMMANDS.get(flow, '.fixture'),
                  'sent_message_id': 100, 'ack_reply_message_id': 101,
                  'ack_reply_sender_id': 8788895566, 'next_due_ts': due,
                  'succeeded': status == 'acknowledged'}
        (directory / 'last_result.json').write_text(json.dumps(result))
        return directory, dispatch.launchagent_label(entry, 'send')

    def collect(self, now=1000):
        self.registry.write_text(json.dumps({'flows': self.entries, 'helpers': self.helpers}))
        return dispatch.collect_result_labels(self.root, registry_path=self.registry, now_ts=now)

    def test_all_periodic_commands_resume_both_cooldowns_and_success_cycles(self):
        expected = []
        for index, flow in enumerate(dispatch.PERIODIC_COMMANDS):
            for status in ('cooldown_pending', 'acknowledged', 'settled'):
                _, label = self.add_flow(flow, account=f'tg{1000 + len(expected)}', status=status)
                expected.append(label)
        self.assertEqual(self.collect(), sorted(expected))

    def test_no_early_wake_and_no_expiry_of_an_overdue_deadline(self):
        _, label = self.add_flow(due=2000)
        self.assertEqual(self.collect(1999), [])
        self.assertEqual(self.collect(2000), [label])
        self.assertEqual(self.collect(40000), [label])  # supervisor restart after several hours

    def test_pending_command_keeps_its_own_retry_window(self):
        directory, _ = self.add_flow()
        (directory / 'command_pending.json').write_text(json.dumps({'pending': True, 'next_retry_ts': 2000}))
        self.assertEqual(self.collect(), [])

    def test_paused_disabled_manual_and_event_flows_are_not_resumed(self):
        directory, _ = self.add_flow(account='tg1')
        (directory / 'pause_all').touch()
        self.add_flow(account='tg2', enabled=False)
        self.add_flow('doufa_demo_player_02', account='tg3')
        self.add_flow('xuangu_zuoda', account='tg4')
        self.add_flow('yuanyingchuqiao', account='tg5')
        self.assertEqual(self.collect(), [])

    def test_account_level_pause_stops_every_flow_of_that_account_only(self):
        directory, _ = self.add_flow(account='tg1')
        _, other = self.add_flow(account='tg2')
        (directory.parent / 'pause_all').touch()
        self.assertEqual(self.collect(), [other])
        # A pause file above a non-account state dir is not an account pause.
        (self.root / 'pause_all').touch()
        self.assertEqual(dispatch.is_paused(self.root / 'default', 'flow')[0], False)

    def test_interval_and_dedicated_cooldown_owners_are_not_double_scheduled(self):
        self.add_flow(interval_dispatch_seconds=7200)
        _, label = self.add_flow('fumofabao_xuantianzhanlingjian', account='tg9100000001')
        self.helpers = [{'label': label.removesuffix('.send') + '.cooldown_dispatch', 'enabled': True}]
        self.assertEqual(self.collect(), [])
        path = self.root / 'accounts/tg9100000001/fumofabao_xuantianzhanlingjian/last_result.json'
        row = json.loads(path.read_text()); row.update(status='acknowledged', succeeded=True)
        path.write_text(json.dumps(row))
        self.assertEqual(self.collect(), [label])

    def test_invalid_receipt_or_deadline_cannot_wake_a_sender(self):
        directory, _ = self.add_flow()
        path = directory / 'last_result.json'; original = json.loads(path.read_text())
        changes = [{'actor_id': 1}, {'flow_slug': 'other'}, {'message': '.other'},
                   {'target': '@other'}, {'topic_id': 1}, {'ack_reply_sender_id': 1},
                   {'ack_reply_message_id': 99}, {'acked': False}, {'failed': True},
                   {'reset_triggered_after_send': True}, {'status': 'failed'}]
        changes += [{'next_due_ts': value} for value in (True, 0, -1, None, 'bad', float('nan'), float('inf'))]
        for change in changes:
            with self.subTest(change=change):
                path.write_text(json.dumps({**original, **change}))
                self.assertEqual(self.collect(), [])

    def test_recent_kick_is_deduplicated_without_writing_business_state(self):
        directory, label = self.add_flow(due=900)
        path = directory / 'last_result.json'; before = path.read_bytes()
        (self.root / 'return_deadline_dispatch.json').write_text(json.dumps({'last_kick_by_label': {label: 950}}))
        self.assertEqual(self.collect(1000), [])
        self.assertEqual(self.collect(1070), [label])
        self.assertEqual(path.read_bytes(), before)

    def test_malformed_result_does_not_hide_other_due_accounts(self):
        bad, _ = self.add_flow(account='tg1')
        _, good = self.add_flow(account='tg2')
        (bad / 'last_result.json').write_text('bad json')
        self.assertEqual(self.collect(), [good])

    def test_main_uses_existing_nonforced_sender_and_records_successful_kick(self):
        _, label = self.add_flow()
        self.collect()
        with patch.dict(dispatch.os.environ, {'XIU_STATE_ROOT': str(self.root)}), \
             patch.object(dispatch, 'collect_due_labels', return_value=[]), \
             patch.object(dispatch, 'collect_unconfirmed_labels', return_value=[]), \
             patch.object(dispatch, 'collect_yuanshen_labels', return_value=[]), \
             patch.object(dispatch, 'collect_result_labels', return_value=[label]), \
             patch.object(dispatch, 'dispatch_intervals', return_value=set()), \
             patch.object(dispatch.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as kick:
            self.assertEqual(dispatch.main(), 0)
        self.assertEqual(kick.call_args.args[0], ['/bin/launchctl', 'kickstart', f'gui/{dispatch.os.getuid()}/{label}'])
        state = json.loads((self.root / 'return_deadline_dispatch.json').read_text())
        self.assertIn(label, state['last_kick_by_label'])


class ArtifactCooldownHelperTests(unittest.TestCase):
    def test_dispatchers_preserve_each_others_records_when_launches_overlap(self):
        self.check_overlapping_dispatchers(block_helper=False)

    def test_dispatchers_preserve_each_others_records_when_helper_write_overlaps(self):
        self.check_overlapping_dispatchers(block_helper=True)

    def check_overlapping_dispatchers(self, *, block_helper):
        from scripts import run_fumofabao_xuantianzhanlingjian_cooldown_dispatch as helper
        from state_store import persist_json_atomic

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as patches:
            root = Path(tmp)
            state_root = root / 'state'
            state_dir = state_root / 'accounts' / helper.ACCOUNT / helper.FLOW
            state_dir.mkdir(parents=True)
            ledger = state_root / 'return_deadline_dispatch.json'
            key = f'{helper.TARGET}|{helper.TOPIC_ID}|{helper.MESSAGE}'
            retry = state_dir / 'cooldown_retry.json'
            retry.write_text(json.dumps({key: {'not_before_ts': 1}}))
            ledger.write_text(json.dumps({'fixture': 'preserved', 'last_kick_by_label': {'older': 1}}))
            other_label = 'ai.hermes.xiuxian.tg1.wendao.send'
            blocked, release = Event(), Event()
            launches = []

            def run(command, **_kwargs):
                launches.append(command)
                if not block_helper and command[0] == '/bin/launchctl' and not blocked.is_set():
                    blocked.set()
                    if not release.wait(5):
                        raise TimeoutError('fixture release')
                return SimpleNamespace(returncode=0)

            def save(path, payload):
                blocked.set()
                if not release.wait(5):
                    raise TimeoutError('fixture release')
                persist_json_atomic(path, payload)

            patches.enter_context(patch.multiple(
                helper, ROOT=root, STATE_DIR=state_dir, RETRY_PATH=retry, DISPATCH_STATE_PATH=ledger))
            patches.enter_context(patch.dict(dispatch.os.environ, {'XIU_STATE_ROOT': str(state_root)}))
            patches.enter_context(patch('automation_dossiers.refresh_automation_dossiers'))
            patches.enter_context(patch.object(dispatch, 'PROJECT_ROOT', root))
            for name in ('collect_due_labels', 'collect_unconfirmed_labels', 'collect_yuanshen_labels',
                         'collect_tianji_retry_labels'):
                patches.enter_context(patch.object(dispatch, name, return_value=[]))
            patches.enter_context(patch.object(dispatch, 'collect_result_labels', return_value=[other_label]))
            patches.enter_context(patch.object(dispatch, 'dispatch_intervals', return_value=set()))
            patches.enter_context(patch.object(dispatch.subprocess, 'run', side_effect=run))
            patches.enter_context(patch.object(dispatch.time, 'time', return_value=10_000))
            if block_helper:
                patches.enter_context(patch.object(helper, 'persist_json_atomic', side_effect=save))

            first, second = (helper.main, dispatch.main) if block_helper else (dispatch.main, helper.main)
            with ThreadPoolExecutor(max_workers=1) as workers:
                pending = workers.submit(first)
                try:
                    self.assertTrue(blocked.wait(5), 'first dispatcher did not reach the interleaving point')
                    self.assertEqual(second(), 0)
                finally:
                    release.set()
                self.assertEqual(pending.result(timeout=5), 0)
            self.assertEqual(second(), 0)
            commands = [command[0] for command in launches]
            self.assertEqual(commands.count('/bin/bash'), 1)
            self.assertEqual(commands.count('/bin/launchctl'), 1)
            state = json.loads(ledger.read_text())
            self.assertEqual(state['fixture'], 'preserved')
            self.assertEqual(state['last_kick_by_label'],
                             {'older': 1, other_label: 10_000, helper.SEND_LABEL: 10_000})

    def test_paused_or_recently_kicked_sender_is_not_relaunched_every_minute(self):
        from scripts import run_fumofabao_xuantianzhanlingjian_cooldown_dispatch as helper
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / 'state/accounts' / helper.ACCOUNT / helper.FLOW
            state_dir.mkdir(parents=True)
            key = f'{helper.TARGET}|{helper.TOPIC_ID}|{helper.MESSAGE}'
            (state_dir / 'cooldown_retry.json').write_text(json.dumps({key: {'not_before_ts': 1}}))
            with patch.multiple(helper, ROOT=root, STATE_DIR=state_dir, RETRY_PATH=state_dir / 'cooldown_retry.json',
                                DISPATCH_STATE_PATH=root / 'state/return_deadline_dispatch.json'), \
                    patch.object(helper.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
                    patch.object(helper.time, 'time', return_value=10_000):
                (state_dir.parent / 'pause_all').touch()  # account-level pause
                self.assertEqual(helper.main(), 0)
                run.assert_not_called()
                (state_dir.parent / 'pause_all').unlink()
                self.assertEqual(helper.main(), 0)  # due: dispatches once
                self.assertEqual(helper.main(), 0)  # sender left the record (lock busy): deduped
                self.assertEqual(run.call_count, 1)
                kicks = json.loads((root / 'state/return_deadline_dispatch.json').read_text())['last_kick_by_label']
                self.assertEqual(kicks, {helper.SEND_LABEL: 10_000})
