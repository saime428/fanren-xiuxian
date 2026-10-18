"""Admission read failures must not consume the sole shared signal."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import qingyuanzi_host as host
from test_qyz_source_policy import entry_message


class SharedEntryRetryTests(unittest.IsolatedAsyncioTestCase):
    def setup_signal(self):
        self.message = entry_message(chat_id=host.CANONICAL_CHAT_ID)
        entry = host.extract_world_boss_entry(self.message)
        self.now = self.message.date.timestamp()
        self.payload = {'chat_id': entry.chat_id, 'message_id': entry.message_id,
                        'fingerprint': entry.fingerprint, 'published_at': self.message.date.isoformat()}
        self.client = SimpleNamespace(get_messages=AsyncMock(return_value=self.message))
        self.monitor = SimpleNamespace(process_message=AsyncMock(return_value=True))
        self.stop = asyncio.Event()
        self.delays = []

    async def sleep(self, delay):
        self.delays.append(delay)
        self.now += delay

    async def invoke(self):
        with patch.object(host, 'ensure_connected', new=AsyncMock()):
            return await host.deliver_shared_entry(self.client, self.monitor, 'tg9100000001', self.payload,
                                                  self.stop, now=lambda: self.now, sleep=self.sleep)

    async def test_first_connection_error_and_empty_read_retry_then_admit_once(self):
        self.setup_signal()
        self.client.get_messages.side_effect = [ConnectionError(), None, self.message]
        self.assertEqual(await self.invoke(), 'handled')
        self.assertEqual(self.client.get_messages.await_count, 3)
        self.monitor.process_message.assert_awaited_once_with(self.message, source='shared_entry')
        self.assertEqual(self.delays, [1, 2])

    async def test_permanent_failure_is_bounded_by_original_publication_time(self):
        self.setup_signal()
        self.client.get_messages.side_effect = ConnectionError()
        self.assertEqual(await self.invoke(), 'expired')
        self.assertEqual(sum(self.delays), host.ENTRY_RECOVERY_WINDOW_SECONDS)
        self.assertLessEqual(self.client.get_messages.await_count, 12)
        self.assertLessEqual(max(self.delays), 8)
        self.monitor.process_message.assert_not_awaited()

    async def test_stale_signal_does_not_reconnect_or_read(self):
        self.setup_signal()
        self.now += host.ENTRY_RECOVERY_WINDOW_SECONDS + 1
        self.assertEqual(await self.invoke(), 'expired')
        self.client.get_messages.assert_not_awaited()

    async def test_wrong_message_is_never_admitted(self):
        self.setup_signal()
        self.client.get_messages.return_value = entry_message(chat_id=host.CANONICAL_CHAT_ID, message_id=999)
        self.assertEqual(await self.invoke(), 'message_mismatch')
        self.monitor.process_message.assert_not_awaited()
        self.assertEqual(self.client.get_messages.await_count, 1)

    async def test_already_inflight_battle_does_not_get_retried(self):
        self.setup_signal()
        self.monitor.process_message.return_value = False
        self.assertEqual(await self.invoke(), 'handled')
        self.monitor.process_message.assert_awaited_once()
        self.assertEqual(self.delays, [])

    async def test_shutdown_and_cancellation_propagate_without_new_reads(self):
        self.setup_signal()
        self.stop.set()
        self.assertEqual(await self.invoke(), 'stopped')
        self.client.get_messages.assert_not_awaited()
        self.stop.clear()
        self.client.get_messages.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.invoke()
        self.assertEqual(self.delays, [])

    async def test_read_finishing_after_deadline_is_not_admitted(self):
        self.setup_signal()
        async def slow_read(*args, **kwargs):
            self.now += host.ENTRY_RECOVERY_WINDOW_SECONDS + 1
            return self.message
        self.client.get_messages.side_effect = slow_read
        self.assertEqual(await self.invoke(), 'expired')
        self.monitor.process_message.assert_not_awaited()

    async def test_stop_during_read_does_not_start_a_battle(self):
        self.setup_signal()
        async def stop_during_read(*args, **kwargs):
            self.stop.set()
            return self.message
        self.client.get_messages.side_effect = stop_during_read
        self.assertEqual(await self.invoke(), 'stopped')
        self.monitor.process_message.assert_not_awaited()

    async def test_reconnect_failure_is_retried_before_read(self):
        self.setup_signal()
        with patch.object(host, 'ensure_connected', new=AsyncMock(side_effect=[ConnectionError(), None])) as connect:
            status = await host.deliver_shared_entry(self.client, self.monitor, 'tg9100000001', self.payload,
                                                     self.stop, now=lambda: self.now, sleep=self.sleep)
        self.assertEqual(status, 'handled')
        self.assertEqual(connect.await_count, 2)
        self.client.get_messages.assert_awaited_once()
