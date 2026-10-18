"""Read-only lifecycle listener respects Telegram history backoff."""
import unittest
from unittest.mock import AsyncMock, patch

from telethon.errors import FloodWaitError
from scripts import account_lifecycle_monitor as monitor


class LifecycleMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_chats_merge_by_timestamp_without_id_collision(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        main, public = monitor.READ_CHAT_IDS
        earlier = SimpleNamespace(id=10, chat_id=main, date=datetime.fromtimestamp(1000, timezone.utc))
        later = SimpleNamespace(id=10, chat_id=public, date=datetime.fromtimestamp(30000, timezone.utc))
        client = SimpleNamespace(get_messages=AsyncMock(side_effect=[[earlier], [later]]))
        with patch.object(monitor, "observe_reply", new_callable=AsyncMock) as observe:
            await monitor.reconcile(client)
        self.assertEqual([call.args[1] for call in observe.await_args_list], [earlier, later])

    async def test_bootstrap_searches_public_success_wording(self):
        from types import SimpleNamespace
        client = SimpleNamespace(get_messages=AsyncMock(return_value=[]))
        await monitor.reconcile(client, bootstrap=True)
        queries = [(call.args[0], call.kwargs.get("search")) for call in client.get_messages.await_args_list]
        self.assertIn((monitor.READ_CHAT_IDS[1], "成功夺舍重生"), queries)

    async def test_history_flood_wait_keeps_listener_and_retries_after_cooldown(self):
        client = object()
        with patch.object(monitor, "reconcile", new_callable=AsyncMock) as read, \
                patch.object(monitor.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            read.side_effect = [FloodWaitError(request=None, capture=121), 12345]
            result = await monitor.reconcile_with_backoff(client, bootstrap=True)
        self.assertEqual(result, 12345)
        sleep.assert_awaited_once_with(121)
        self.assertEqual(read.await_count, 2)
        read.assert_awaited_with(client, bootstrap=True)

    async def test_client_uses_the_transport_factory_gate_and_mtproxy_env(self):
        import contextlib
        import os
        import sqlite3
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from fishing import fishing_accounts
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config/accounts"
            config.mkdir(parents=True)
            (config / "tg9100000001.env").write_text("export TG_API_ID=1\nexport TG_API_HASH='unused-test-value'\n")
            (config / "mtproto.env").write_text("export TG_MTPROTO_ENABLED=1\n")
            session = Path(tmp) / "state/accounts/tg9100000001/session.session"
            session.parent.mkdir(parents=True)
            sqlite3.connect(session).close()
            client = AsyncMock()
            client.is_user_authorized.return_value = True
            with (patch.object(monitor, "ROOT", Path(tmp)), patch.object(fishing_accounts, "CONFIG_ROOT", config),
                  patch.dict(os.environ, {}),
                  patch.object(monitor, "telegram_client", return_value=client) as factory,
                  patch.object(monitor, "ensure_connected", new_callable=AsyncMock) as connect,
                  patch.object(monitor, "reconcile_with_backoff", new_callable=AsyncMock),
                  patch.object(monitor, "queue_ready_reminders"),
                  patch.object(monitor, "transaction", return_value=contextlib.nullcontext({}))):
                os.environ.pop("TG_MTPROTO_ENABLED", None)
                await monitor.run(SimpleNamespace(notify=False, once=True, message_id=None))
                self.assertEqual(os.environ["TG_MTPROTO_ENABLED"], "1")
            self.assertEqual(factory.call_args.args[1:], (1, "unused-test-value"))
            self.assertTrue(factory.call_args.kwargs["lifecycle_observer"])
            self.assertNotIn("request_retries", factory.call_args.kwargs)
            self.assertNotIn("flood_sleep_threshold", factory.call_args.kwargs)
            connect.assert_awaited_once_with(client)
            client.connect.assert_not_awaited()

    async def test_other_errors_are_not_hidden_as_success(self):
        with patch.object(monitor, "reconcile", new_callable=AsyncMock) as read:
            read.side_effect = RuntimeError("unreadable state")
            with self.assertRaisesRegex(RuntimeError, "unreadable state"):
                await monitor.reconcile_with_backoff(object())
