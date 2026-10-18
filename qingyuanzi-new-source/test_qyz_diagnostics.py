import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from qyz_diagnostics import archive_battle
import qingyuanzi_host as host


class BattleArchiveTests(unittest.TestCase):
    def test_account_entry_archive_is_atomic_private_and_immutable_across_reconciliation(self):
        with tempfile.TemporaryDirectory() as folder:
            state_file = Path(folder) / 'tg9100000001' / 'world_boss_state.json'
            payload = {'account': 'tg9100000001', 'entry_message_id': 123,
                       'source_chat_id': -1001, 'record': {'status': 'unknown_mutation_outcome'}}
            first = archive_battle(state_file, payload)
            original = first.read_bytes()
            self.assertEqual(first.name, '123.json')
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            self.assertEqual(archive_battle(state_file, payload), first)
            payload['record']['status'] = 'settled_partial'
            second = archive_battle(state_file, payload)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), original)
            self.assertEqual(json.loads(second.read_bytes())['record']['status'], 'settled_partial')
            self.assertFalse(list(first.parent.glob('*.tmp')))

    def test_archive_rejects_another_account_path(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'account_path_mismatch'):
                archive_battle(Path(folder) / 'tg9100000002' / 'world_boss_state.json',
                               {'account': 'tg9100000001', 'entry_message_id': 123})


class HostArchiveTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, folder):
        account = 'tg9100000001'
        entry = host.OfficialWorldBossEntry(123, host.CANONICAL_CHAT_ID, 'hantianzun16_bot',
                                           None, 'a' * 64, 'qyz_fixture_never_persist')
        record = {'message_id': 123, 'fingerprint': entry.fingerprint, 'mutation_started': True,
                  'status': 'settled_damage_reduced', 'accepted_hits': 16, 'server_perfects': 16,
                  'protocol_result': {'reference_outcome': {'diagnostics': {
                      'hits': [{'index': index, 'server_hit': {'perfect': True,
                                'automationDamageMultiplier': .55}} for index in range(1, 17)],
                      'finish': {'server_result': {'automation_risk': {
                          'damageMultiplier': .55, 'review': True, 'flags': ['official fixture']}}}}}}}
        actor = SimpleNamespace(state_file=Path(folder) / account / 'world_boss_state.json',
                                state={'world_boss_events': [record]}, save_state=lambda: None,
                                client=SimpleNamespace(get_messages=AsyncMock(return_value=None)))
        monitor = host.OfficialWorldBossMonitor(actor, account, logger=Mock())
        return actor, monitor, entry, record

    async def test_archive_keeps_all_receipts_and_official_penalty_without_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            actor, monitor, entry, record = self.fixture(folder)
            record['private_fields'] = {key: 'private-placeholder-value' for key in (
                'token', 'initData', 'Authorization', 'cookie', 'secret', 'signature',
                'signed_url', 'callbackURL', 'charge_ticket', 'webview_url')}
            original = json.dumps(actor.state)
            await monitor._archive_event(entry)
            target = actor.state_file.parent / 'battle_diagnostics' / '123.json'
            raw = target.read_text()
            archived = json.loads(raw)
            self.assertNotIn('private-placeholder-value', raw)
            self.assertNotIn(entry.token, raw)
            self.assertEqual(archived['account'], 'tg9100000001')
            self.assertEqual(archived['source_chat_id'], host.CANONICAL_CHAT_ID)
            diagnostics = archived['record']['protocol_result']['reference_outcome']['diagnostics']
            self.assertEqual([hit['index'] for hit in diagnostics['hits']], list(range(1, 17)))
            self.assertEqual(diagnostics['finish']['server_result']['automation_risk']['damageMultiplier'], .55)
            # v23: once archived, the live journal keeps only statuses, counts and
            # the health inputs; the official penalty stays visible from both.
            from qyz_state import server_damage_multiplier_min
            live = actor.state['world_boss_events'][0]
            self.assertTrue(live['slimmed'])
            self.assertEqual(server_damage_multiplier_min(live), .55)
            self.assertEqual((live['status'], live['accepted_hits'], live['server_perfects']),
                             ('settled_damage_reduced', 16, 16))
            self.assertNotIn('private-placeholder-value', raw)
            self.assertNotIn(entry.token, json.dumps(actor.state))
            self.assertLess(len(json.dumps(actor.state)), len(original))

    async def test_snapshot_failure_does_not_replace_battle_outcome(self):
        with tempfile.TemporaryDirectory() as folder:
            actor, monitor, entry, record = self.fixture(folder)
            with patch.object(host, 'sanitize_state', side_effect=ValueError('fixture')):
                await monitor._archive_event(entry)
            self.assertEqual(record['status'], 'settled_damage_reduced')
            monitor.log.error.assert_called_once()
            self.assertFalse((actor.state_file.parent / 'battle_diagnostics').exists())

    async def test_cancelled_archive_still_releases_inflight_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            _actor, monitor, entry, _record = self.fixture(folder)
            monitor._inflight.add(entry.fingerprint)
            with patch.object(monitor, '_archive_event', new=AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await monitor.run_entry(entry)
            self.assertNotIn(entry.fingerprint, monitor._inflight)
