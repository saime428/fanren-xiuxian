"""Deadline isolation and official empty-cast evidence; no live requests."""
import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import run_miniapp_fishing_cycle as cycle
import miniapp_fishing_integration as protocol
from fishing_catch import extract_catch
from miniapp_shared.summaries import _catch_text
from test_miniapp_fishing_integration import _fake_bridge


class AccountIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_http_on_one_account_cannot_block_another_reel_timer(self):
        entered, fast_finished = threading.Event(), threading.Event()
        loops, threads = {}, {}
        parent = asyncio.get_running_loop()

        async def account(name, options):
            loops[name] = asyncio.get_running_loop()
            threads[name] = threading.get_ident()
            if name == 'slow':
                entered.set()
                # Same-loop gather cannot run fast until this timeout expires.
                released_by_fast = fast_finished.wait(3)
                return name, {'released_by_fast': released_by_fast}
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.005)
            await asyncio.sleep(.01)  # this account's reel deadline
            fast_finished.set()
            return name, {'reeled': True}

        with patch.object(cycle, 'run_account', side_effect=account), \
             patch.object(cycle, 'LOGIN_STAGGER_SECONDS', 0), \
             patch.object(cycle, 'account_options', side_effect=lambda config, name: {}):
            results = dict(await cycle.run_all(['slow', 'fast'], {}))
        self.assertTrue(results['slow']['released_by_fast'])
        self.assertTrue(results['fast']['reeled'])
        self.assertIsNot(loops['slow'], loops['fast'])
        self.assertIsNot(loops['fast'], parent)
        self.assertNotEqual(threads['slow'], threads['fast'])

    async def test_account_logins_are_staggered_but_run_in_parallel(self):
        import time
        started, finished = {}, {}

        async def account(name, options):
            started[name] = time.monotonic()
            await asyncio.sleep(0.3)  # a cast still in progress when the next login starts
            finished[name] = time.monotonic()
            return name, {}

        with patch.object(cycle, 'run_account', side_effect=account), \
             patch.object(cycle, 'LOGIN_STAGGER_SECONDS', 0.1), \
             patch.object(cycle, 'account_options', side_effect=lambda config, name: {}):
            await cycle.run_all(['a', 'b', 'c'], {})
        self.assertGreaterEqual(started['b'] - started['a'], 0.09)
        self.assertGreaterEqual(started['c'] - started['b'], 0.09)
        self.assertLess(started['c'], finished['a'])  # not serialized behind the first cast

    async def test_empty_batch_creates_no_protocol(self):
        with patch.object(cycle, 'run_account') as run:
            self.assertEqual(await cycle.run_all([], {}), [])
            run.assert_not_called()


class ResourceTests(unittest.TestCase):
    def test_http_pool_is_reused_inside_a_run_and_not_shared_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = protocol.UpstreamFishingProtocol(session_root=Path(tmp)/'accounts')
            pools = [Mock(), Mock()]
            for pool in pools:
                pool.post.return_value.ok = True
                pool.post.return_value.status_code = 200
                pool.post.return_value.json.return_value = {'ok': True}
            async def run(*args):
                bridge._request('start', token='fixture', init_data='fixture')
                bridge._request('result', token='fixture', init_data='fixture')
                return {'status': 'fixture'}
            with patch.object(bridge, '_run_account_with_evidence', side_effect=run), \
                 patch('requests.Session', side_effect=pools), patch('requests.post') as unpooled:
                asyncio.run(bridge.run_account('tg9100000001', {}))
                asyncio.run(bridge.run_account('tg9100000001', {}))
            unpooled.assert_not_called()
            for pool in pools:
                self.assertEqual(pool.post.call_count, 2)
                pool.close.assert_called_once()
            self.assertIsNone(bridge._http_session)

    def test_http_pool_closes_after_protocol_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = protocol.UpstreamFishingProtocol(session_root=Path(tmp)/'accounts')
            pool = Mock()
            with patch('requests.Session', return_value=pool), \
                 patch.object(bridge, '_run_account_with_evidence', new=AsyncMock(side_effect=RuntimeError('fixture'))):
                with self.assertRaises(RuntimeError):
                    asyncio.run(bridge.run_account('tg9100000001', {}))
            pool.close.assert_called_once()
            self.assertIsNone(bridge._http_session)

    def test_connect_failure_removes_private_session_copy(self):
        bridge, client = _fake_bridge('tg9100000001', [])
        client.connect = AsyncMock(side_effect=TimeoutError('fixture'))
        with self.assertRaises(TimeoutError):
            asyncio.run(bridge._authenticated('tg9100000001'))
        self.assertEqual(list(bridge.session_copy_root.iterdir()), [])
        self.assertTrue(client.closed)

    def test_bad_account_env_removes_private_session_copy(self):
        bridge, _client = _fake_bridge('tg9100000001', [])
        bridge.env_loader = lambda account: {}  # no TG_API_ID: fails after the copy exists
        with self.assertRaises(KeyError):
            asyncio.run(bridge._authenticated('tg9100000001'))
        self.assertEqual(list(bridge.session_copy_root.iterdir()), [])

    def test_stale_session_copies_are_swept_and_live_ones_kept(self):
        import time
        import fishing_accounts
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('tg-crashed', 'tg-live'):
                (root / name).mkdir()
                (root / name / 'session.session').write_bytes(b'auth-key')
            old = time.time() - 2 * fishing_accounts.STALE_SESSION_COPY_SECONDS
            for path in (root / 'tg-crashed' / 'session.session', root / 'tg-crashed'):
                os.utime(path, (old, old))
            fishing_accounts.sweep_stale_session_copies(root)
            self.assertEqual([root / 'tg-live'], list(root.iterdir()))


class TransportFactoryTests(unittest.TestCase):
    def test_client_factory_reads_mtproto_env_and_fails_closed(self):
        import fishing_accounts
        from telethon.network.connection import ConnectionTcpMTProxyIntermediate
        clean = {key: value for key, value in os.environ.items()
                 if not key.startswith("TG_MTPROTO_") and key != "TG_TELEGRAM_CONNECTION_GATE_PATH"}
        with tempfile.TemporaryDirectory() as tmp:
            env_file, session = Path(tmp) / "mtproto.env", str(Path(tmp) / "copy")
            with patch.object(fishing_accounts, "MTPROTO_ENV", env_file), patch.dict(os.environ, clean, clear=True):
                direct = fishing_accounts.telegram_client(session, 1, "hash", request_retries=1)
                self.assertIsNone(direct._proxy)  # no mtproto.env: direct, no error
                # transport's 429-storm policy, not Telethon's defaults (True / 60 s)
                self.assertEqual((False, 0, 1), (direct._auto_reconnect, direct.flood_sleep_threshold,
                                                 direct._request_retries))
                self.assertEqual(str(fishing_accounts.CONNECTION_GATE), os.environ["TG_TELEGRAM_CONNECTION_GATE_PATH"])
                env_file.write_text(f"export TG_MTPROTO_ENABLED=1\nexport TG_MTPROTO_SECRET='{'a' * 32}'\n")
                proxied = fishing_accounts.telegram_client(session, 1, "hash")
                self.assertEqual((ConnectionTcpMTProxyIntermediate, ("127.0.0.1", 5753, "a" * 32)),
                                 (proxied._connection, proxied._proxy))
            with patch.object(fishing_accounts, "MTPROTO_ENV", env_file), patch.dict(os.environ, clean, clear=True):
                env_file.write_text("export TG_MTPROTO_ENABLED=1\n")
                with self.assertRaisesRegex(ValueError, "TG_MTPROTO_SECRET"):
                    fishing_accounts.telegram_client(session, 1, "hash")


class EvidenceTests(unittest.TestCase):
    def test_finish_timing_and_score_are_attached_to_confirmed_cast(self):
        from test_fishing_chum_plan import CHALLENGE, ok
        bridge, _ = _fake_bridge('tg9100000001', [
            ok({'result': {'score': 92}}),
            ok({'result': {'ready': True, 'caught': False, 'reason': 'timeout'}}),
        ])
        # This test enters after authentication; omit the helper's two login replies.
        bridge.http_transport.replies = bridge.http_transport.replies[2:]
        with patch.object(protocol.asyncio, 'sleep', new=AsyncMock()):
            result = asyncio.run(bridge._settle_challenge(
                account='tg9100000001', token='fixture', init_data='fixture', challenge=CHALLENGE, steps=[]))
        diagnostics = result['cast_diagnostics']
        self.assertEqual(diagnostics['finish_score'], 92)
        self.assertTrue(diagnostics['finish_ok'])
        self.assertEqual(diagnostics['finish_http_status'], 200)
        self.assertEqual(diagnostics['result_probes'], 1)
        self.assertGreaterEqual(diagnostics['finish_duration_ms'], 0)
        self.assertGreaterEqual(diagnostics['reel_wake_lateness_ms'], 0)
        self.assertEqual(result['catch']['reason'], 'timeout')

    def test_official_timeout_is_distinct_from_other_misses(self):
        for reason, label in [('timeout', '空竿（超时）'), ('miss', '空竿')]:
            catch = extract_catch({'result': {'ready': True, 'caught': False, 'reason': reason}})
            self.assertEqual(catch['reason'], reason)
            self.assertFalse(catch['caught'])
            self.assertEqual(_catch_text(catch), label)
        for reason in ['untrusted request token', {'token': 'fixture'}]:
            catch = extract_catch({'result': {'caught': False, 'reason': reason}})
            self.assertNotIn('reason', catch)

    def test_finish_diagnostics_survive_later_closed_scope_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = protocol.UpstreamFishingProtocol(session_root=Path(tmp)/'accounts')
            diagnostics = {'finish_ok': False, 'finish_http_status': 0, 'finish_duration_ms': 20000,
                           'reel_wake_lateness_ms': 3000, 'result_probes': 9}
            p._save_pending('tg9100000001', pending=True, challenge_id='fixture', cast_diagnostics=diagnostics)
            result = p._record_settlement('tg9100000001', 'fixture', {'caught': False},
                ['closed_result_recovery'], 0, response={'data': {'result': {'caught': False, 'reason': 'timeout'}}})
            self.assertEqual(result['cast_diagnostics'], diagnostics)
            self.assertEqual(result['catch']['reason'], 'timeout')
            self.assertIs(result['server_result_ready'], True)

    def test_solver_matches_javascript_utf16_seed_units(self):
        base = {'challengeId': 'fixture', 'targetLow': 41, 'targetHigh': 68, 'fishPower': 1.7,
                'minDurationMs': 5200, 'maxDurationMs': 70000}
        scalar = protocol.build_fishing_proof({**base, 'fishSeed': '🐟'})
        code_units = protocol.build_fishing_proof({**base, 'fishSeed': '\ud83d\udc1f'})
        self.assertEqual(scalar, code_units)


if __name__ == '__main__':
    unittest.main()
