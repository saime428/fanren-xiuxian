import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import qingyuanzi_host as host


class _FakeClient:
    disconnected = False

    def __init__(self, *_args, **_kwargs):
        pass

    async def connect(self):
        return None

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=123, username="test")

    async def disconnect(self):
        self.disconnected = True


class HostMonitorInstallTests(unittest.IsolatedAsyncioTestCase):
    def test_world_boss_sources_use_both_authorized_groups(self):
        self.assertEqual(host.TARGET_CHAT_IDS, (-1009400000004, -1009400000005))

    async def test_run_account_fails_closed_when_monitor_install_is_inactive(self):
        account = "tg123"
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            session = state_root / "accounts" / account / "session.session"
            session.parent.mkdir(parents=True)
            session.touch()

            async def inactive_install(monitor):
                monitor.actor.state["world_boss_monitor_active"] = False
                monitor.actor.state["world_boss_last_error"] = "target_chat_unavailable"
                return False

            verifier = SimpleNamespace(start_account=Mock(), close_account=AsyncMock())
            connect = AsyncMock()

            with (
                patch.object(host, "ensure_connected", connect),
                patch.object(host, "STATE_ROOT", state_root),
                patch.object(host, "ACCOUNTS", {account: {"source": "source", "username": "test"}}),
                patch.object(host, "_read_env", return_value={"TG_API_ID": "1", "TG_API_HASH": "x"}),
                patch.object(host, "_telegram_client", _FakeClient),
                patch.object(host.OfficialWorldBossMonitor, "install", inactive_install),
                patch.object(host, "VERIFICATION_SERVICE", verifier),
            ):
                with self.assertRaisesRegex(RuntimeError, "monitor install inactive"):
                    await host.run_account(account, asyncio.Event())
            # The first connection goes through the fleet's connection gate.
            connect.assert_awaited_once()
            self.assertIsInstance(connect.await_args.args[0], _FakeClient)

if __name__ == "__main__":
    unittest.main()
