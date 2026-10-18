import asyncio
import fcntl
import gc
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors, functions
from telethon.network.connection import ConnectionTcpMTProxyIntermediate

from transport import _connect_with_gate, disconnect_telegram_client, telegram_client, with_telegram_retry


class TelegramRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_flood_wait_survives_telethon_without_hidden_sleep_or_value_error(self):
        with patch.dict(os.environ, {}, clear=True):
            client = telegram_client(":memory:", 12345, "unused-test-value")
        request = functions.help.GetConfigRequest()
        future = asyncio.get_running_loop().create_future()
        future.set_exception(errors.FloodWaitError(request=request, capture=8))
        sender = SimpleNamespace(send=Mock(return_value=future))
        try:
            with patch("telethon.client.users.asyncio.sleep", new_callable=AsyncMock) as sleep:
                with self.assertRaises(errors.FloodWaitError) as caught:
                    await client._call(sender, request)
                self.assertEqual(caught.exception.seconds, 8)
                sleep.assert_not_awaited()
            sender.send.assert_called_once()
        finally:
            client.session.close()

    async def test_flood_wait_and_server_fault_back_off_without_reconnect(self):
        for error in (errors.FloodWaitError(request=None, capture=8),
                      errors.SlowModeWaitError(request=None, capture=8),
                      errors.ServerError(request=None, message="INTERNAL"),
                      errors.TimedOutError(request=None, message="TIMEOUT")):
            with self.subTest(error=type(error).__name__):
                action = AsyncMock(side_effect=[error, "ok"])
                reconnect = AsyncMock()
                with patch("transport.asyncio.sleep", new_callable=AsyncMock) as sleep:
                    self.assertEqual(await with_telegram_retry(
                        action, label="typed-rpc-recovery", attempts=2, base_sleep=1,
                        reconnect=reconnect), "ok")
                self.assertEqual(action.await_count, 2)
                reconnect.assert_not_awaited()
                self.assertGreaterEqual(sleep.await_args.args[0], getattr(error, "seconds", 1))

    async def test_cancelled_gate_wait_does_not_leave_a_background_lock_holder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'connect.lock'
            with path.open('a+') as held, patch.dict(os.environ, {'TG_TELEGRAM_CONNECTION_GATE_PATH': str(path), 'TG_TELEGRAM_MAX_CONCURRENT_CONNECTS': '1'}):
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                client = AsyncMock()
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(_connect_with_gate(client, 0.03), timeout=0.2)
                client.connect.assert_not_awaited()
                fcntl.flock(held, fcntl.LOCK_UN)
                await asyncio.wait_for(_connect_with_gate(client, 0.3), timeout=0.5)
                client.connect.assert_awaited_once()

    async def test_failed_connect_releases_gate_for_next_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'TG_TELEGRAM_CONNECTION_GATE_PATH': str(Path(tmp) / 'gate')}):
            failing = AsyncMock()
            failing.connect.side_effect = ConnectionResetError()
            with self.assertRaises(ConnectionResetError):
                await _connect_with_gate(failing, 0.2)
            next_client = AsyncMock()
            await _connect_with_gate(next_client, 0.2)
            next_client.connect.assert_awaited_once()

    async def test_handshakes_can_overlap_but_never_exceed_fleet_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'TG_TELEGRAM_CONNECTION_GATE_PATH': str(Path(tmp) / 'gate'),
            'TG_TELEGRAM_MAX_CONCURRENT_CONNECTS': '4',
        }):
            entered = 0
            four_entered = asyncio.Event()
            release = asyncio.Event()

            async def connect():
                nonlocal entered
                entered += 1
                if entered == 4:
                    four_entered.set()
                await release.wait()

            tasks = [asyncio.create_task(_connect_with_gate(SimpleNamespace(connect=connect), 1))
                     for _ in range(4)]
            try:
                await asyncio.wait_for(four_entered.wait(), 0.5)
                fifth = AsyncMock()
                with self.assertRaises(TimeoutError):
                    await _connect_with_gate(fifth, 0.03)
                fifth.connect.assert_not_awaited()
                self.assertEqual(entered, 4)
            finally:
                release.set()
                await asyncio.gather(*tasks)
            await _connect_with_gate(fifth, 0.2)
            fifth.connect.assert_awaited_once()

    async def test_disconnect_consumes_terminal_future_without_hiding_caller_error(self) -> None:
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        unhandled = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            future = loop.create_future()
            future.set_exception(ConnectionResetError('connection lost'))
            client = SimpleNamespace(disconnected=future, disconnect=AsyncMock())
            await disconnect_telegram_client(client)
            client.disconnect.assert_awaited_once()
            del client, future
            gc.collect()
            await asyncio.sleep(0)
            self.assertEqual(unhandled, [])
        finally:
            loop.set_exception_handler(previous_handler)

    async def test_http_429_retries_without_reconnect_storm(self) -> None:
        attempts = 0
        reconnects = 0

        from telethon.errors.common import InvalidBufferError

        async def action() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise InvalidBufferError("HTTP code 429")
            return "ok"

        async def reconnect() -> None:
            nonlocal reconnects
            reconnects += 1

        with patch.dict("os.environ", {"TG_TELEGRAM_429_COOLDOWN_SECONDS": "0.01"}):
            result = await with_telegram_retry(
                action,
                label="429-unit-test",
                attempts=3,
                base_sleep=1,
                reconnect=reconnect,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 3)
        self.assertEqual(reconnects, 0)

    async def test_telethon_unsuccessful_request_is_retried_as_connection_fault(self) -> None:
        attempts = 0
        reconnects = 0

        async def action() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("Request was unsuccessful 1 time(s)")
            return "ok"

        async def reconnect() -> None:
            nonlocal reconnects
            reconnects += 1

        result = await with_telegram_retry(
            action,
            label="telethon-request-failure",
            attempts=2,
            base_sleep=0,
            reconnect=reconnect,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 2)
        self.assertEqual(reconnects, 1)

    async def test_incomplete_read_is_retried_as_connection_fault(self) -> None:
        attempts = 0
        reconnects = 0

        async def action() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise asyncio.IncompleteReadError(partial=b"", expected=8)
            return "ok"

        async def reconnect() -> None:
            nonlocal reconnects
            reconnects += 1

        result = await with_telegram_retry(
            action,
            label="unit-test",
            attempts=2,
            base_sleep=0,
            reconnect=reconnect,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 2)
        self.assertGreaterEqual(reconnects, 1)


class TelegramClientFactoryTests(unittest.TestCase):
    def test_mtproxy_options_are_applied_when_enabled(self) -> None:
        settings = {
            "TG_MTPROTO_ENABLED": "1",
            "TG_MTPROTO_HOST": "127.0.0.1",
            "TG_MTPROTO_PORT": "5753",
            "TG_MTPROTO_SECRET": "a" * 32,
        }
        with patch.dict(os.environ, settings, clear=True), patch("transport.TelegramClient") as constructor:
            telegram_client("test-session", 12345, "api-hash")

        kwargs = constructor.call_args.kwargs
        self.assertIs(kwargs["connection"], ConnectionTcpMTProxyIntermediate)
        self.assertEqual(kwargs["proxy"], ("127.0.0.1", 5753, "a" * 32))

    def test_mtproxy_options_are_absent_when_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("transport.TelegramClient") as constructor:
            telegram_client("test-session", 12345, "api-hash")

        kwargs = constructor.call_args.kwargs
        self.assertNotIn("connection", kwargs)
        self.assertNotIn("proxy", kwargs)

    def test_mtproxy_rejects_invalid_secret(self) -> None:
        settings = {
            "TG_MTPROTO_ENABLED": "1",
            "TG_MTPROTO_SECRET": "not-a-valid-secret",
        }
        with patch.dict(os.environ, settings, clear=True):
            with self.assertRaisesRegex(ValueError, "32 hexadecimal"):
                telegram_client("test-session", 12345, "api-hash")


if __name__ == "__main__":
    unittest.main()
