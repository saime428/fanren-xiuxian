"""Authorization probes must not overwrite credentials after an inconclusive RPC."""
import asyncio
import gc
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon.errors import AuthKeyUnregisteredError, FloodWaitError, ServerError
from telethon.tl.functions.updates import GetStateRequest

from scripts import ensure_session_authorized as auth


class ProbeClient:
    def __init__(self, error=None, *, during_connect=False, terminal_error=False):
        self.disconnected = asyncio.get_running_loop().create_future()
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.request = AsyncMock(return_value=object())
        self.is_user_authorized = AsyncMock(return_value=False)
        if error is not None:
            (self.connect if during_connect else self.request).side_effect = error
        if terminal_error:
            self.disconnected.set_exception(error)

    async def __call__(self, request):
        return await self.request(request)


class SessionAuthorizationProbeTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, root):
        return {
            "TG_API_ID": "12345", "TG_API_HASH": "fixture-only",
            "TG_SESSION_NAME": str(root / "target"),
            "TG_SESSION_FALLBACK_NAME": str(root / "fallback"),
            "TG_SESSION_AUTH_PROBE_TIMEOUT_SECONDS": "5",
            "TG_TELEGRAM_CONNECTION_GATE_PATH": str(root / "connect.lock"),
            "TG_TELEGRAM_MAX_CONCURRENT_CONNECTS": "1",
        }

    async def test_network_and_server_failures_preserve_target_and_never_probe_fallback(self):
        cases = [
            asyncio.IncompleteReadError(b"", 4),
            asyncio.IncompleteReadError(b"partial", 8),
            ConnectionResetError(), BrokenPipeError(), TimeoutError(),
            ValueError("Request was unsuccessful 1 time(s)"),
            FloodWaitError(request=None, capture=3),
            ServerError(request=None, message="INTERNAL"),
            RuntimeError("unclassified probe failure"),
        ]
        for error in cases:
            for during_connect in (False, True):
                with self.subTest(error=type(error).__name__, during_connect=during_connect):
                    with tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        target = root / "target.session"
                        target.write_bytes(b"existing credential fixture")
                        sidecar = root / "target.session-wal"
                        sidecar.write_bytes(b"existing journal fixture")
                        (root / "fallback.session").write_bytes(b"fallback fixture")
                        client = ProbeClient(error, during_connect=during_connect)
                        with patch.dict(os.environ, self.settings(root)), \
                             patch.object(auth, "load_dotenv"), \
                             patch.object(auth, "telegram_client", return_value=client) as factory, \
                             patch.object(auth.shutil, "copy2") as copy, \
                             patch("builtins.print"):
                            await auth.main()
                        self.assertEqual(target.read_bytes(), b"existing credential fixture")
                        self.assertEqual(sidecar.read_bytes(), b"existing journal fixture")
                        self.assertEqual(list(root.glob("*.before-reseed-*")), [])
                        factory.assert_called_once()
                        copy.assert_not_called()
                        client.is_user_authorized.assert_not_awaited()
                        client.disconnect.assert_awaited_once()

    async def test_success_requires_a_fresh_authorized_rpc(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ProbeClient()
            with patch.dict(os.environ, self.settings(Path(tmp))), \
                 patch.object(auth, "telegram_client", return_value=client):
                result = await auth.probe_authorization("fixture", 12345, "fixture", timeout_seconds=1)
            self.assertEqual(result, ("ok", True))
            self.assertIsInstance(client.request.call_args.args[0], GetStateRequest)
            client.is_user_authorized.assert_not_awaited()
            client.disconnect.assert_awaited_once()

    async def test_explicit_unauthorized_response_can_reseed_from_verified_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.session"
            target.write_bytes(b"rejected target fixture")
            # The fallback is open elsewhere with a committed row still only in
            # its WAL file: a byte copy of the .session file would miss it.
            fallback = sqlite3.connect(root / "fallback.session")
            self.addCleanup(fallback.close)
            fallback.execute("PRAGMA journal_mode=WAL")
            fallback.execute("CREATE TABLE fixture (value TEXT)")
            fallback.execute("INSERT INTO fixture VALUES ('authorized fallback fixture')")
            fallback.commit()
            (root / "target.session-wal").write_bytes(b"old journal")
            clients = [ProbeClient(AuthKeyUnregisteredError(request=None)), ProbeClient(), ProbeClient()]
            with patch.dict(os.environ, self.settings(root)), \
                 patch.object(auth, "load_dotenv"), \
                 patch.object(auth, "telegram_client", side_effect=clients), \
                 patch("builtins.print"):
                await auth.main()
            copied = sqlite3.connect(target)
            self.assertEqual(copied.execute("SELECT value FROM fixture").fetchall(),
                             [("authorized fallback fixture",)])
            copied.close()
            backups = list(root.glob("target.session.before-reseed-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"rejected target fixture")
            self.assertFalse((root / "target.session-wal").exists())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            for client in clients:
                client.disconnect.assert_awaited_once()

    async def test_unavailable_probe_for_missing_target_does_not_reseed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fallback.session").write_bytes(b"fallback fixture")
            with patch.dict(os.environ, self.settings(root)), \
                 patch.object(auth, "load_dotenv"), \
                 patch.object(auth, "telegram_client", return_value=ProbeClient(TimeoutError())) as factory, \
                 patch.object(auth.shutil, "copy2") as copy, \
                 patch("builtins.print"):
                with self.assertRaises(SystemExit) as result:
                    await auth.main()
            self.assertEqual(result.exception.code, 75)
            factory.assert_called_once()
            copy.assert_not_called()
            self.assertFalse((root / "target.session").exists())

    async def test_unavailable_fallback_does_not_replace_rejected_target_or_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.session"
            target.write_bytes(b"target fixture")
            clients = [ProbeClient(AuthKeyUnregisteredError(request=None)), ProbeClient(TimeoutError())]
            with patch.dict(os.environ, self.settings(root)), \
                 patch.object(auth, "load_dotenv"), \
                 patch.object(auth, "telegram_client", side_effect=clients), \
                 patch.object(auth.shutil, "copy2") as copy, \
                 patch("builtins.print"):
                with self.assertRaises(SystemExit) as result:
                    await auth.main()
            self.assertEqual(result.exception.code, 75)
            copy.assert_not_called()
            self.assertEqual(target.read_bytes(), b"target fixture")

    async def test_failed_connection_consumes_telethon_terminal_future(self):
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        unhandled = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = ProbeClient(asyncio.IncompleteReadError(b"", 4), during_connect=True,
                                     terminal_error=True)
                with patch.dict(os.environ, self.settings(Path(tmp))), \
                     patch.object(auth, "telegram_client", return_value=client):
                    self.assertEqual(await auth.probe_authorization("fixture", 1, "fixture", timeout_seconds=1),
                                     ("probe_failed", False))
                del client
                gc.collect()
                await asyncio.sleep(0)
                self.assertEqual(unhandled, [])
        finally:
            loop.set_exception_handler(previous_handler)


if __name__ == "__main__":
    unittest.main()
