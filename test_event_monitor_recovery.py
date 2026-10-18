import asyncio
import contextlib
import datetime
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from event_monitor_runtime import EventMonitorHealth, MonitorIdentityMismatch, RUNTIME_VERSION, question_monitor_version
from scripts import xiuxian_healthcheck as healthcheck
import auto_reply_topic_event
import observe_return
import recipe_notice_monitor


class FakeListener:
    def __init__(self):
        self.connected = True
        self.future = asyncio.get_running_loop().create_future()
        self.get_me = AsyncMock(return_value=SimpleNamespace(id=123))

    @property
    def disconnected(self):
        return asyncio.shield(self.future)

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True
        if self.future.done():
            self.future = asyncio.get_running_loop().create_future()

    async def disconnect(self):
        self.connected = False
        if not self.future.done():
            self.future.set_result(None)


class MonitorRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = FakeListener()
        self.health = EventMonitorHealth(self.root, 'tg123', 'jiyin_choice', 'send',
                                         ['@example_game_group', -1009400000005], 10)
        self.gate = patch.dict(os.environ, {'TG_TELEGRAM_CONNECTION_GATE_PATH': str(self.root / 'connect.lock')})
        self.gate.start()

    async def asyncTearDown(self):
        self.health.close()
        await self.client.disconnect()
        self.gate.stop()
        self.temp.cleanup()

    async def scan(self, *, on_scan_complete):
        on_scan_complete()

    async def test_success_requires_listener_identity_and_completed_scan(self):
        await self.health.run_cycle(self.client, self.scan)
        state = json.loads(self.health.path.read_text())
        self.assertEqual(state['status'], 'healthy')
        self.assertTrue(state['listener_connected'])
        self.assertIsNotNone(state['last_success_ts'])
        self.client.get_me.assert_awaited_once()

    async def test_failed_scan_is_not_cleared_by_next_checking_heartbeat(self):
        async def fail(*, on_scan_complete):
            raise ConnectionResetError('private request token must not be stored')

        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ConnectionResetError):
            await self.health.run_cycle(self.client, fail)
        before = json.loads(self.health.path.read_text())
        self.assertEqual(before['status'], 'failed')
        self.assertNotIn('private request', self.health.path.read_text())
        self.assertIsNone(before['last_success_ts'])

        async def locked(*, on_scan_complete):
            pass

        await self.health.run_cycle(self.client, locked)
        after = json.loads(self.health.path.read_text())
        self.assertEqual(after['error_type'], 'ConnectionResetError')
        self.assertIsNone(after['last_success_ts'])

    async def test_real_recovery_clears_failure_after_complete_scan(self):
        self.health.failed('history_scan', TimeoutError())
        await self.health.run_cycle(self.client, self.scan)
        state = json.loads(self.health.path.read_text())
        self.assertEqual(state['status'], 'healthy')
        self.assertIsNone(state['error_type'])
        self.assertEqual(state['consecutive_failures'], 0)

    async def test_wrong_account_never_runs_reconciliation(self):
        self.client.get_me.return_value = SimpleNamespace(id=999)
        scan = AsyncMock()
        with self.assertRaises(MonitorIdentityMismatch):
            await self.health.run_cycle(self.client, scan)
        scan.assert_not_awaited()
        self.assertEqual(self.health.data['status'], 'failed')

    async def test_listener_loss_is_reported_even_while_no_scan_is_running(self):
        await self.health.run_cycle(self.client, self.scan)
        self.client.connected = False
        self.client.future.set_exception(ConnectionResetError())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(self.health.data['status'], 'failed')
        self.assertEqual(self.health.data['error_stage'], 'push_listener_disconnected')
        await self.health.run_cycle(self.client, self.scan)
        self.assertTrue(self.client.connected)
        self.assertEqual(self.health.data['status'], 'healthy')

    async def test_cancelled_scan_does_not_become_healthy(self):
        async def cancelled(*, on_scan_complete):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.health.run_cycle(self.client, cancelled)
        self.assertEqual(self.health.data['status'], 'failed')


class MonitorHealthCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temp.name) / 'accounts'
        self.flow_dir = self.state_root / 'tg123' / 'jiyin_choice'
        self.flow_dir.mkdir(parents=True)
        now = time.time() - 2
        self.payload = {
            'runtime_version': RUNTIME_VERSION, 'account': 'tg123', 'flow': 'jiyin_choice',
            'question_monitor_version': question_monitor_version('jiyin_choice'),
            'kind': 'send', 'pid': 123, 'ts': now, 'started_at_ts': now - 100,
            'last_success_ts': now, 'listener_verified_ts': now,
            'source_targets': ['@example_game_group', '-1009400000005'],
            'listener_connected': True, 'status': 'healthy', 'reconcile_seconds': 10,
            'error_type': None,
        }
        self.proc = 'user 123 0 0 python3 auto_reply_topic_event.py'
        self.patch_root = patch.object(healthcheck, 'STATE_ROOT', self.state_root)
        self.patch_root.start()

    def tearDown(self):
        self.patch_root.stop()
        self.temp.cleanup()

    def classify(self):
        (self.flow_dir / 'monitor_health.json').write_text(json.dumps(self.payload))
        return healthcheck.classify_kind('tg123', 'jiyin_choice', 'send', self.proc, True, {})

    def test_current_account_scan_clears_historical_traceback(self):
        log = self.flow_dir / 'send.launchd.log'
        log.write_text('Traceback (most recent call last):\nConnectionResetError: old\n')
        os.utime(log, (time.time() - 60, time.time() - 60))
        self.assertEqual(self.classify().severity, 'ok')

    def test_question_flows_require_loaded_push_handler(self):
        self.payload.pop('question_monitor_version')
        self.assertEqual(self.classify().status, 'monitor_question_handler_outdated')

    def test_jiyin_and_xuangu_require_their_own_current_handler(self):
        self.payload['question_monitor_version'] = question_monitor_version('xuangu_zuoda')
        self.assertEqual(self.classify().status, 'monitor_question_handler_outdated')
        self.assertEqual(question_monitor_version('xuangu_zuoda'), 'question-xuangu-v7')

    def test_new_programming_error_after_recovery_remains_bad(self):
        (self.flow_dir / 'send.launchd.log').write_text('Traceback (most recent call last):\nValueError: invalid\n')
        item = self.classify()
        self.assertEqual(item.severity, 'bad')
        self.assertEqual(item.note, 'ValueError')

    def test_stale_wrong_pid_identity_or_incomplete_sources_cannot_pass(self):
        changes = (
            {'last_success_ts': time.time() - 1000}, {'pid': 999}, {'account': 'tg999'},
            {'source_targets': ['@example_game_group']}, {'listener_connected': False},
            {'listener_verified_ts': time.time() - 1000}, {'ts': time.time() + 60},
            {'status': 'failed', 'error_type': 'ConnectionResetError', 'error_stage': 'history_scan'},
            {'status': 'checking', 'error_type': 'ConnectionResetError', 'error_stage': 'history_scan'},
        )
        original = dict(self.payload)
        for change in changes:
            with self.subTest(change=change):
                self.payload = {**original, **change}
                self.assertEqual(self.classify().severity, 'bad')

    def test_checking_does_not_extend_last_success_freshness(self):
        self.payload.update(status='checking', ts=time.time(), last_success_ts=time.time() - 1000)
        self.assertEqual(self.classify().severity, 'bad')

    def test_active_registry_requires_evidence_even_if_wrapper_is_running(self):
        entries = [{'account': 'tg123', 'flow': 'jiyin_choice', 'kinds': ['send'],
                    'enabled': True, 'monitor_health_required': True}]
        items = healthcheck.collect_active_items('user 123 bash send_jiyin_choice_tg123.sh', entries, {})
        self.assertEqual(items[0].severity, 'bad')
        self.assertEqual(items[0].status, 'monitor_health_missing')


class CompletedScanEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_push_only_wakes_for_its_own_fresh_return_or_owned_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / 'session'
            session.with_suffix('.session').touch()
            (Path(tmp) / 'status_probe.json').write_text(json.dumps({'sent_message_id': 77}))
            settings = {
                'XIU_ACCOUNT_SLUG': 'tg123', 'TG_API_ID': '123', 'TG_API_HASH': 'unused-test-value',
                'TG_SESSION_NAME': str(session), 'TG_TARGET': '@example_game_group',
                'TG_TOPIC_ID': '7310786', 'TG_FLOW_SLUG': 'shendubiguan', 'TG_STATE_DIR': tmp,
                'TG_RETURN_TRUSTED_SENDER_IDS': '456', 'TG_TRIGGER_TARGET_USERNAME': 'tester',
                'TG_RETURN_MARKER_TEXT': '深度闭关总结', 'TG_RETURN_ALLOW_ROOT_TOPIC_EVENTS': '1',
                'TG_TRIGGER_MAX_EVENT_AGE_SECONDS': '600',
                'TG_MONITOR_TARGETS': '@example_game_group|-1009400000005',
                'TG_MONITOR_ROOT_TARGETS': '-1009400000005',
            }
            client = FakeListener()
            client.get_entity = AsyncMock(side_effect=lambda target: SimpleNamespace(
                id=9400000004 if target == '@example_game_group' else 9400000005))
            handlers = []
            client.add_event_handler = lambda callback, _event: handlers.append(callback)

            async def exercise(_reconcile, signal, **_kwargs):
                async def check(text, expected, *, sender=456, age=0, reply_to=None, chat=-1009400000004):
                    signal.clear()
                    message = SimpleNamespace(id=200, message=text, sender_id=sender,
                                              date=datetime.datetime.fromtimestamp(time.time() - age),
                                              reply_to=(SimpleNamespace(reply_to_top_id=7310786,
                                                                        reply_to_msg_id=reply_to)
                                                        if reply_to is not None else None),
                                              reply_to_msg_id=reply_to)
                    await handlers[0](SimpleNamespace(message=message, chat_id=chat))
                    self.assertEqual(signal.is_set(), expected, text)

                await check('普通聊天 @tester', False)
                await check('深度闭关总结 @sample_peer_12_else', False)
                await check('深度闭关总结 @tester', False, sender=999)
                await check('深度闭关总结 @tester', False, age=601)
                await check('深度闭关总结 @tester', True)
                await check('深度闭关总结 @tester', True, chat=-1009400000005)
                await check('你并未处于深度闭关之中', True, reply_to=77)
                await check('你并未处于深度闭关之中', False, reply_to=99)

            with (patch.dict(os.environ, settings, clear=True),
                  patch.object(observe_return, 'load_dotenv'),
                  patch.object(observe_return, 'telegram_client', return_value=client),
                  patch.object(observe_return, 'run_observer_event_loop', side_effect=exercise)):
                await observe_return.run_persistent_observer()

    async def test_second_monitor_instance_skips_before_health_or_telegram(self):
        from state_store import flow_lock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'monitor_health.json').write_text('{"pid": 1}')
            (root / 'observer_health.json').write_text('{"pid": 1}')
            settings = {'TG_FLOW_SLUG': 'jiyin_choice', 'TG_STATE_DIR': tmp, 'XIU_ACCOUNT_SLUG': 'tg123',
                        'TG_API_ID': '123', 'TG_API_HASH': 'unused-test-value',
                        'TG_SESSION_NAME': str(root / 'session'), 'TG_TARGET': '@example_game_group'}
            with (patch.dict(os.environ, settings, clear=True),
                  patch.object(auto_reply_topic_event, 'load_dotenv'),
                  patch.object(auto_reply_topic_event, 'telegram_client') as reply_client,
                  patch.object(observe_return, 'load_dotenv'),
                  patch.object(observe_return, 'telegram_client') as observer_client,
                  patch.object(recipe_notice_monitor, 'load_dotenv'),
                  patch('transport.telegram_client') as recipe_client,
                  flow_lock(root / 'question_monitor.lock'), flow_lock(root / 'observer_monitor.lock'),
                  flow_lock(root / 'recipe_monitor.lock'), contextlib.redirect_stdout(io.StringIO())):
                self.assertIsNone(await auto_reply_topic_event.run_persistent_auto_reply())
                self.assertIsNone(await observe_return.run_persistent_observer())
                self.assertIsNone(await recipe_notice_monitor.run_persistent())
            reply_client.assert_not_called()
            observer_client.assert_not_called()
            recipe_client.assert_not_called()
            self.assertEqual(json.loads((root / 'monitor_health.json').read_text()), {'pid': 1})
            self.assertEqual(json.loads((root / 'observer_health.json').read_text()), {'pid': 1})

    async def test_auto_reply_persistent_scan_reuses_listener_client(self):
        from question_reply_monitor import QuestionReplyMonitor
        from telethon.tl.types import PeerChannel
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = AsyncMock()
            client.get_messages.return_value = []
            health = EventMonitorHealth(root, 'tg123', 'jiyin_choice', 'send',
                                         ['@example_game_group', '-1009400000005'], 10)
            self.addCleanup(health.close)
            sources = [('@example_game_group', PeerChannel(9400000004)),
                       ('-1009400000005', PeerChannel(9400000005))]
            monitor = QuestionReplyMonitor(client, health, sources, username='tester',
                trusted_sender_ids={456}, flow='jiyin_choice', state_dir=root,
                state_path=root / 'last_success.json', max_age=600, scan_limit=50,
                reconcile_seconds=10, reply_text='.收敛气息')
            completed = unittest.mock.Mock()
            async def once(action, **kwargs):
                return await action()
            with patch('question_reply_monitor.with_telegram_retry', side_effect=once), \
                    patch.object(auto_reply_topic_event, 'telegram_client') as factory:
                await monitor.scan(on_scan_complete=completed)
            completed.assert_called_once()
            self.assertEqual(client.get_messages.await_count, 2)
            factory.assert_not_called()
            client.disconnect.assert_not_awaited()

    async def test_return_observer_persistent_scan_reuses_listener_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = {
                'TG_API_ID': '123', 'TG_API_HASH': 'unused-test-value',
                'TG_SESSION_NAME': str(Path(tmp) / 'session'), 'TG_TARGET': '@example_game_group',
                'TG_TOPIC_ID': '7310786', 'TG_FLOW_SLUG': 'shendubiguan', 'TG_STATE_DIR': tmp,
                'TG_SEQUENCE_SEND_WRAPPER': '/unused-test-wrapper', 'TG_RETURN_TRUSTED_SENDER_IDS': '456',
                'TG_TRIGGER_TARGET_USERNAME': 'tester', 'TG_RETURN_MARKER_TEXT': 'return marker',
                'TG_LOG_IDLE': '0', 'TG_MONITOR_TARGETS': '@example_game_group|-1009400000005',
            }
            client = AsyncMock()
            client.get_entity.side_effect = lambda target: SimpleNamespace(
                id=9400000004 if target == '@example_game_group' else 9400000005)
            completed = unittest.mock.Mock()
            with (patch.dict(os.environ, settings, clear=True),
                  patch.object(observe_return, 'load_dotenv'),
                  patch.object(observe_return, 'telegram_client') as factory,
                  patch.object(observe_return, 'ensure_connected', new_callable=AsyncMock),
                  patch.object(observe_return, 'collect_recent_messages', new_callable=AsyncMock,
                               side_effect=[[], []]),
                  patch.object(observe_return, 'disconnect_telegram_client', new_callable=AsyncMock) as cleanup,
                  patch.object(observe_return.subprocess, 'Popen') as sender):
                await observe_return.main_async(
                    on_scan_complete=completed,
                    client_override=client,
                )
            completed.assert_called_once()
            factory.assert_not_called()
            cleanup.assert_not_awaited()
            sender.assert_not_called()

    async def test_return_observer_kick_outwaits_sender_with_its_own_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = {
                'TG_API_ID': '123', 'TG_API_HASH': 'unused-test-value',
                'TG_SESSION_NAME': str(Path(tmp) / 'observer.session'), 'TG_TARGET': '@example_game_group',
                'TG_TOPIC_ID': '7310786', 'TG_FLOW_SLUG': 'shendubiguan', 'TG_STATE_DIR': tmp,
                'TG_SCAN_LIMIT': '200', 'TG_RUN_MAX_SECONDS': '120', 'TG_KEPT': 'yes',
                'TG_SEQUENCE_SEND_WRAPPER': '/unused-test-wrapper', 'TG_RETURN_TRUSTED_SENDER_IDS': '456',
                'TG_TRIGGER_TARGET_USERNAME': 'tester', 'TG_RETURN_MARKER_TEXT': 'return marker',
                'TG_MONITOR_TARGETS': '@example_game_group',
            }
            event = SimpleNamespace(
                id=900, sender_id=456, message='@tester return marker',
                reply_to=SimpleNamespace(reply_to_msg_id=7310786, reply_to_top_id=None),
                date=datetime.datetime.now(datetime.timezone.utc))
            client = AsyncMock()
            client.get_entity.return_value = SimpleNamespace(id=9400000004)
            with (patch.dict(os.environ, settings, clear=True),
                  patch.object(observe_return, 'load_dotenv'),
                  patch.object(observe_return, 'ensure_connected', new_callable=AsyncMock),
                  patch.object(observe_return, 'collect_recent_messages', new_callable=AsyncMock,
                               return_value=[event]),
                  patch.object(observe_return.subprocess, 'Popen') as sender):
                sender.return_value.configure_mock(pid=4321, **{'wait.return_value': 0})
                self.assertEqual(await observe_return.main_async(client_override=client), 0)
            sender.assert_called_once()
            child_env = sender.call_args.kwargs['env']
            self.assertEqual(child_env['TG_KEPT'], 'yes')
            self.assertTrue(set(observe_return.OBSERVER_ONLY_ENV).isdisjoint(child_env))
            timeout = sender.return_value.wait.call_args.kwargs['timeout']
            self.assertGreaterEqual(timeout, 25 + 180)

    async def test_return_observer_routine_pass_reads_only_new_messages(self):
        from event_monitor_runtime import HISTORY_OVERLAP_IDS, HistoryWindow
        with tempfile.TemporaryDirectory() as tmp:
            settings = {
                'TG_API_ID': '123', 'TG_API_HASH': 'unused-test-value',
                'TG_SESSION_NAME': str(Path(tmp) / 'session'), 'TG_TARGET': '@example_game_group',
                'TG_TOPIC_ID': '7310786', 'TG_FLOW_SLUG': 'shendubiguan', 'TG_STATE_DIR': tmp,
                'TG_SEQUENCE_SEND_WRAPPER': '/unused-test-wrapper', 'TG_RETURN_TRUSTED_SENDER_IDS': '456',
                'TG_MONITOR_TARGETS': '@example_game_group|-1009400000005',
            }
            entities = {'@example_game_group': SimpleNamespace(id=9400000004),
                        -1009400000005: SimpleNamespace(id=9400000005)}
            chatter = {9400000004: [SimpleNamespace(id=500, message='chat')],
                       9400000005: [SimpleNamespace(id=700, message='chat')]}
            client = AsyncMock()
            client.get_messages.return_value = []
            history = HistoryWindow()
            with (patch.dict(os.environ, settings, clear=True),
                  patch.object(observe_return, 'load_dotenv'),
                  patch.object(observe_return, 'ensure_connected', new_callable=AsyncMock),
                  patch.object(observe_return, 'collect_recent_messages', new_callable=AsyncMock,
                               side_effect=lambda **kw: chatter[kw['chat_id'].id]) as full_window,
                  patch.object(observe_return.subprocess, 'Popen') as sender):
                for _ in range(2):
                    await observe_return.main_async(client_override=client, history=history, entities=entities)
            client.get_entity.assert_not_awaited()
            self.assertEqual(full_window.await_count, 2)  # first pass only
            self.assertEqual([call.kwargs['min_id'] for call in client.get_messages.await_args_list],
                             [500 - HISTORY_OVERLAP_IDS, 700 - HISTORY_OVERLAP_IDS])
            sender.assert_not_called()

    async def test_auto_reply_does_not_confirm_partial_source_scan(self):
        from question_reply_monitor import QuestionReplyMonitor
        from telethon.tl.types import PeerChannel
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = AsyncMock()
            client.get_messages.side_effect = [[], ConnectionResetError()]
            health = EventMonitorHealth(root, 'tg123', 'jiyin_choice', 'send',
                                         ['@example_game_group', '-1009400000005'], 10)
            self.addCleanup(health.close)
            monitor = QuestionReplyMonitor(client, health,
                [('@example_game_group', PeerChannel(9400000004)), ('-1009400000005', PeerChannel(9400000005))],
                username='tester', trusted_sender_ids={456}, flow='jiyin_choice', state_dir=root,
                state_path=root / 'last_success.json', max_age=600, scan_limit=50,
                reconcile_seconds=10, reply_text='.收敛气息')
            completed = unittest.mock.Mock()
            async def once(action, **kwargs):
                return await action()
            with patch('question_reply_monitor.with_telegram_retry', side_effect=once):
                with self.assertRaises(ConnectionResetError):
                    await monitor.scan(on_scan_complete=completed)
            completed.assert_not_called()
            client.send_message.assert_not_awaited()
            self.assertFalse((root / 'last_success.json').exists())

    async def test_return_observer_does_not_confirm_partial_source_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = {
                'TG_API_ID': '123', 'TG_API_HASH': 'unused-test-value',
                'TG_SESSION_NAME': str(Path(tmp) / 'session'), 'TG_TARGET': '@example_game_group',
                'TG_TOPIC_ID': '7310786', 'TG_FLOW_SLUG': 'shendubiguan', 'TG_STATE_DIR': tmp,
                'TG_SEQUENCE_SEND_WRAPPER': '/unused-test-wrapper', 'TG_RETURN_TRUSTED_SENDER_IDS': '456',
                'TG_MONITOR_TARGETS': '@example_game_group|-1009400000005',
            }
            client = AsyncMock()
            client.get_entity.side_effect = lambda target: SimpleNamespace(
                id=9400000004 if target == '@example_game_group' else 9400000005)
            completed = unittest.mock.Mock()
            with (patch.dict(os.environ, settings, clear=True),
                  patch.object(observe_return, 'load_dotenv'),
                  patch.object(observe_return, 'telegram_client', return_value=client),
                  patch.object(observe_return, 'ensure_connected', new_callable=AsyncMock),
                  patch.object(observe_return, 'collect_recent_messages', new_callable=AsyncMock,
                               side_effect=[[], ConnectionResetError()]),
                  patch.object(observe_return, 'disconnect_telegram_client', new_callable=AsyncMock) as cleanup,
                  patch.object(observe_return.subprocess, 'Popen') as sender):
                with self.assertRaises(ConnectionResetError):
                    await observe_return.main_async(on_scan_complete=completed)
            completed.assert_not_called()
            sender.assert_not_called()
            cleanup.assert_awaited_once_with(client)
            self.assertFalse((Path(tmp) / 'observer_last_seen.json').exists())


if __name__ == '__main__':
    unittest.main()
