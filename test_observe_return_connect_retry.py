import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import observe_return
from observe_return import event_matches, run_observer_event_loop


class ObserveReturnConnectRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_push_loop_reconciles_on_startup_signal_and_timer(self):
        scans = 0
        waits = 0
        signal = asyncio.Event()

        async def reconcile():
            nonlocal scans
            scans += 1

        async def wait_for_event(timeout):
            nonlocal waits
            self.assertEqual(timeout, 60)
            waits += 1
            if waits == 1:
                signal.set()
                return True
            return False

        await run_observer_event_loop(
            reconcile,
            signal,
            reconcile_seconds=60,
            max_reconciliations=3,
            wait_for_event=wait_for_event,
        )
        self.assertEqual(scans, 3)

    async def test_observer_does_not_use_implicit_async_context_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = {
                'TG_API_ID': '123', 'TG_API_HASH': 'unused-test-value',
                'TG_SESSION_NAME': str(Path(tmp) / 'session'), 'TG_TARGET': '@example_game_group',
                'TG_TOPIC_ID': '7310786', 'TG_FLOW_SLUG': 'shendubiguan', 'TG_STATE_DIR': tmp,
                'TG_SEQUENCE_SEND_WRAPPER': '/unused-test-wrapper', 'TG_RETURN_TRUSTED_SENDER_IDS': '456',
                'TG_MONITOR_TARGETS': '@example_game_group|-1009400000005',
            }
            client = AsyncMock()
            client.get_entity.return_value = SimpleNamespace(id=9400000004)
            completed = Mock()
            with (patch.dict(os.environ, settings, clear=True),
                  patch.object(observe_return, 'load_dotenv'),
                  patch.object(observe_return, 'telegram_client', return_value=client),
                  patch.object(observe_return, 'ensure_connected', new_callable=AsyncMock) as connect,
                  patch.object(observe_return, 'force_reconnect', new_callable=AsyncMock) as reconnect,
                  patch.object(observe_return, 'collect_recent_messages', new_callable=AsyncMock, return_value=[]) as read,
                  patch.object(observe_return, 'disconnect_telegram_client', new_callable=AsyncMock) as disconnect,
                  patch('transport.asyncio.sleep', new_callable=AsyncMock)):
                connect.side_effect = [ConnectionResetError(), None, None]
                self.assertEqual(await observe_return.main_async(on_scan_complete=completed), 0)
            self.assertEqual(read.await_count, 2)
            reconnect.assert_awaited_once_with(client)
            disconnect.assert_awaited_once_with(client)
            client.start.assert_not_awaited()
            client.__aenter__.assert_not_awaited()
            completed.assert_called_once_with()
            state = json.loads((Path(tmp) / 'observer_last_seen.json').read_text())
            self.assertEqual(state['status'], 'no_new_event')

    async def test_event_matches_remains_strict_after_connect_change(self):
        from types import SimpleNamespace

        valid = SimpleNamespace(
            id=623069,
            message="📜 修士 @demo_player_01 深度闭关总结",
            sender_id=8917921351,
            date=SimpleNamespace(timestamp=lambda: 1785644180.0),
            reply_to=None,
        )
        result = event_matches(
            valid,
            markers=["深度闭关总结"],
            normalized_topic_id=None,
            target_username="demo_player_01",
            trusted_sender_ids={8917921351},
            last_seen_event_id=0,
            allow_root_topic_events=True,
        )
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
