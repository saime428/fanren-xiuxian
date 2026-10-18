"""Offline broker/worker contracts. Real callback evidence is recorded separately."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from qyz_verification import MemoryTurnstileBroker, VerificationService, PreparedNativeTurnstileBrowser
from qyz_reference.world_boss_turnstile import TurnstileRequestError
from qyz_reference.world_boss_browser import AutomaticTurnstileWorker, NativeTurnstileBrowser, BrowserVerificationError


def create(broker, account='tg9100000001'):
    return broker.create_request(account=account, identity='主魂', challenge_id='fixture_challenge_'+account,
                                 event_fingerprint='a'*64, message_id=123,
                                 origin='https://asc.aiopenai.app')['request_id']


class BrokerTests(unittest.TestCase):
    def test_callback_is_memory_only_single_use_and_accepted_only_after_begin(self):
        broker = MemoryTurnstileBroker()
        with patch.object(Path, 'open', side_effect=AssertionError('file I/O')), \
             patch.object(Path, 'write_text', side_effect=AssertionError('file I/O')):
            key = create(broker)
            broker.record_result(key, accepted=True)
            self.assertEqual(broker.get_request(key)['status'], 'pending')
            broker.submit_token(key, 'fixture_callback_secret')
            self.assertNotIn('fixture_callback_secret', json.dumps(broker.list_requests()))
            self.assertEqual(broker.take_token(key), 'fixture_callback_secret')
            self.assertIsNone(broker.take_token(key))
            self.assertEqual(broker.get_request(key)['status'], 'consumed')
            broker.record_result(key, accepted=True, http_status=200)
            broker.cancel(key)
            self.assertEqual(broker.get_request(key)['status'], 'accepted')
            self.assertFalse(broker._tokens)

    def test_expiry_removes_token_and_late_callback_cannot_revive_request(self):
        now = [100.0]
        broker = MemoryTurnstileBroker(clock=lambda:now[0],request_ttl_seconds=30)
        key = create(broker)
        broker.submit_token(key,'fixture_secret')
        now[0] += 31
        self.assertIsNone(broker.take_token(key))
        self.assertFalse(broker._tokens)
        self.assertEqual(broker.get_request(key)['status'],'expired')
        with self.assertRaises(TurnstileRequestError):
            broker.submit_token(key,'fixture_late')
        broker.record_browser_event(key,'token_generated')
        self.assertEqual(broker.get_request(key)['status'],'expired')

    def test_account_view_cannot_consume_or_overwrite_another_accounts_request(self):
        broker = MemoryTurnstileBroker()
        key = create(broker,'tg9100000006')
        broker.submit_token(key,'fixture_other_account')
        actor = broker.for_account('tg9100000001')
        for call in [lambda:actor.take_token(key),lambda:actor.get_request(key),
                     lambda:actor.cancel(key),lambda:actor.record_result(key,accepted=True),
                     lambda:actor.record_browser_event(key,'widget_ready'),
                     lambda:actor.submit_token(key,'fixture_wrong_account')]:
            with self.assertRaisesRegex(TurnstileRequestError,'account_mismatch'):
                call()
        self.assertEqual(broker.take_token(key),'fixture_other_account')

    def test_account_cancel_all_leaves_other_account_pending(self):
        broker = MemoryTurnstileBroker()
        own = create(broker,'tg9100000001')
        other = create(broker,'tg9100000006')
        broker.for_account('tg9100000001').cancel_all()
        own_row = broker.get_request(own)
        other_row = broker.get_request(other)
        self.assertIsNotNone(own_row)
        self.assertIsNotNone(other_row)
        self.assertEqual((own_row or {})['status'],'cancelled')
        self.assertEqual((other_row or {})['status'],'pending')

    def test_untrusted_origin_and_unbounded_browser_errors_are_rejected(self):
        broker = MemoryTurnstileBroker()
        with self.assertRaisesRegex(TurnstileRequestError,'origin_invalid'):
            broker.create_request(account='fixture',identity='主魂',challenge_id='fixture',
                                  event_fingerprint='a'*64,message_id=1,origin='https://example.org')
        key = create(broker)
        with self.assertRaisesRegex(TurnstileRequestError,'event_invalid'):
            broker.record_browser_event(key,'widget_error','fixture_secret')

    def test_original_worker_serializes_six_independent_requests(self):
        broker = MemoryTurnstileBroker()
        keys = [create(broker,'tg100000'+str(i)) for i in range(6)]
        callbacks = []
        def verify(origin, *, on_event, still_pending):
            self.assertEqual(origin,'https://asc.aiopenai.app')
            self.assertTrue(still_pending())
            on_event('widget_ready')
            on_event('token_generated')
            token = 'fixture_native_'+str(len(callbacks))
            callbacks.append(token)
            return token
        browser = SimpleNamespace(verify=verify,close=lambda:None)
        worker = AutomaticTurnstileWorker(broker,browser)
        for _ in keys:
            self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        self.assertEqual([broker.take_token(key) for key in keys],callbacks)
        self.assertFalse(broker._tokens)

    def test_cancelled_browser_request_discards_late_callback(self):
        broker = MemoryTurnstileBroker()
        key = create(broker)
        def verify(origin, *, on_event, still_pending):
            broker.cancel(key)
            return 'fixture_late_callback'
        worker = AutomaticTurnstileWorker(broker,SimpleNamespace(verify=verify,close=lambda:None))
        worker.run_once()
        self.assertFalse(broker._tokens)
        self.assertEqual(broker.get_request(key)['status'],'cancelled')

    def test_macos_launch_uses_native_display_and_real_chrome(self):
        with tempfile.TemporaryDirectory() as folder:
            browser = NativeTurnstileBrowser(folder)
            browser.command = Mock(return_value={})
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=None)
            response.read.return_value = b'[{"type":"page","webSocketDebuggerUrl":"ws://127.0.0.1:123/fixture"}]'
            process = Mock()
            process.poll.return_value = None
            with patch('qyz_reference.world_boss_browser.sys.platform','darwin'), \
                 patch('qyz_reference.world_boss_browser.find_chrome',return_value='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'), \
                 patch('qyz_reference.world_boss_browser.shutil.which',side_effect=AssertionError('macOS must not require Xvfb')), \
                 patch('qyz_reference.world_boss_browser.urllib.request.urlopen',return_value=response), \
                 patch('qyz_reference.world_boss_browser.subprocess.Popen',return_value=process) as launch, \
                 patch('websocket.create_connection',return_value=Mock()):
                browser.start()
            args = launch.call_args.args[0]
            self.assertEqual(args[0],'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
            self.assertIn('about:blank',args)
            self.assertFalse(any('headless' in value or 'xvfb' in value for value in args))
            browser.close()

    def test_browser_only_accepts_the_official_origin(self):
        browser = NativeTurnstileBrowser('/not-used')
        with self.assertRaisesRegex(BrowserVerificationError,'origin_not_allowed'):
            browser.verify('https://example.org')


class VerificationServiceTests(unittest.TestCase):
    def test_preparation_loads_only_public_page_without_account_data_or_widget(self):
        browser = PreparedNativeTurnstileBrowser('/not-used')
        browser.start = Mock()
        browser.command = Mock()
        browser.evaluate = Mock(return_value=True)
        browser.prepare()
        browser.command.assert_called_once_with('Page.navigate', {
            'url': 'https://asc.aiopenai.app/miniapp/xianxia-world-boss'})
        browser.evaluate.assert_called_once_with('Boolean(window.turnstile && window.__QYZ_TURNSTILE_CONFIG__)')
        browser.prepare()
        self.assertEqual(browser.command.call_count, 1)

    def test_failed_optional_preparation_falls_back_to_original_verification(self):
        broker = MemoryTurnstileBroker()
        key = create(broker)
        browser = SimpleNamespace(prepare=Mock(side_effect=OSError('fixture')),
                                  verify=Mock(return_value='fixture-token'), close=Mock())
        service = VerificationService(Path('/not-used'), browser=browser, broker=broker)
        service.prepare_event('a' * 64, valid_for_seconds=60)
        self.assertTrue(service._run_once())
        self.assertEqual(broker.get_request(key)['status'], 'submitted')
        self.assertEqual(service._preparation['error'], 'public_page_prepare_failed')

    def test_valid_entry_prepares_public_page_without_generating_a_token(self):
        now = [100.0]
        browser = SimpleNamespace(prepare=Mock(), verify=Mock(), close=Mock(), chrome=None)
        broker = MemoryTurnstileBroker(clock=lambda: now[0])
        service = VerificationService(Path('/not-used'), browser=browser, broker=broker,
                                      monotonic=lambda: now[0])
        service.prepare_event('a' * 64, valid_for_seconds=60)
        self.assertFalse(service._run_once())
        browser.prepare.assert_called_once_with()
        browser.verify.assert_not_called()
        self.assertFalse(broker._tokens)
        now[0] += 30
        self.assertFalse(service._run_once())
        browser.close.assert_not_called()
        now[0] += 61
        self.assertFalse(service._run_once())
        browser.close.assert_called_once_with()

    def test_verification_uses_main_then_declared_order_after_short_coalescing(self):
        now = [100.0]
        order = ('tg9100000001', 'tg9100000002', 'tg9100000003',
                 'tg9100000004', 'tg9100000005', 'tg9100000006')
        broker = MemoryTurnstileBroker(clock=lambda: now[0])
        browser = SimpleNamespace(verify=lambda *_args, **_kwargs: 'fixture-token', close=lambda: None)
        service = VerificationService(Path('/not-used'), browser=browser, broker=broker,
                                      account_order=order, monotonic=lambda: now[0])
        # The first network response must not permanently win first slot.
        create(broker, order[1])
        self.assertFalse(service._run_once())
        now[0] += .4
        for account in reversed(order):
            if account != order[1]:
                create(broker, account)
        observed = []
        for _ in order:
            self.assertTrue(service._run_once())
            submitted = [row for row in broker.list_requests() if row['status'] == 'submitted']
            self.assertEqual(len(submitted), 1)
            row = submitted[0]
            observed.append(row['account'])
            broker.take_token(row['request_id'])
            broker.record_result(row['request_id'], accepted=True)
        self.assertEqual(tuple(observed), order)
        self.assertFalse(broker._tokens)

    def test_missing_main_only_delays_available_requests_for_one_second(self):
        now = [100.0]
        broker = MemoryTurnstileBroker(clock=lambda: now[0])
        browser = SimpleNamespace(verify=lambda *_args, **_kwargs: 'fixture-token', close=lambda: None)
        service = VerificationService(Path('/not-used'), browser=browser, broker=broker,
                                      account_order=('tg9100000001', 'tg9100000002'),
                                      monotonic=lambda: now[0])
        key = create(broker, 'tg9100000002')
        self.assertFalse(service._run_once())
        now[0] += 1.01
        self.assertTrue(service._run_once())
        self.assertEqual(broker.get_request(key)['status'], 'submitted')

    def test_reference_service_has_no_signed_browser_drag_extension(self):
        browser = SimpleNamespace(verify=lambda *_args, **_kwargs: "", close=lambda: None, chrome=None)
        service = VerificationService(Path('/not-used'), browser=browser)
        self.assertFalse(hasattr(service, 'verify_drag'))
        self.assertFalse(hasattr(service, 'drag_verifier'))


class VerifierLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_verifier_loop_survives_an_unexpected_worker_exception(self):
        service = VerificationService.__new__(VerificationService)
        browser = SimpleNamespace(closed=0)
        browser.close = lambda: setattr(browser, "closed", browser.closed + 1)
        service.browser, service.stopping, service.task = browser, False, None
        service._humanization = SimpleNamespace(interval=lambda ceiling: 0)
        calls = []

        def run_once():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            service.stopping = True

        service._run_once = run_once
        service.start()
        await service.task
        self.assertEqual(len(calls), 2)
        self.assertEqual(browser.closed, 2)  # once after the failure, once on exit
