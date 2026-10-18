"""Synthetic identity fixtures; no Telegram login or game mutation."""
import importlib
import importlib.util
import json
import hashlib
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest
from urllib.parse import urlencode

from qyz_protocol import ProtocolError


class AuthBindingTests(unittest.TestCase):
    def test_telegram_auth_key_is_loaded_read_only_into_a_memory_session(self):
        auth = importlib.import_module("qyz_auth")
        self.assertTrue(hasattr(auth, "read_session_in_memory"))
        from telethon.sessions import MemorySession
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"synthetic.session"
            with sqlite3.connect(path) as database:
                database.execute("CREATE TABLE sessions (dc_id INTEGER, server_address TEXT, port INTEGER, auth_key BLOB, takeout_id INTEGER)")
                database.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", (2,"149.154.167.51",443,b"synthetic-key-fixture-only-".ljust(256,b"x"),None))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch.object(auth.sqlite3, "connect", wraps=sqlite3.connect) as connect:
                first = auth.read_session_in_memory(path)
                second = auth.read_session_in_memory(path)
            self.assertIsInstance(first, MemorySession)
            self.assertIsNot(first, second)
            self.assertEqual(first.dc_id, 2)
            self.assertEqual(len(first.auth_key.key), 256)
            self.assertTrue(all("mode=ro" in call.args[0] and "immutable=1" in call.args[0] for call in connect.call_args_list))
            first.save()
            second.save()
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["synthetic.session"])

    def test_signed_webview_context_is_bound_to_id_username_start_param_and_age(self):
        self.assertIsNotNone(importlib.util.find_spec("qyz_auth"), "Official WebView identity boundary is required")
        auth = importlib.import_module("qyz_auth")
        arguments = {"account":"tg9100000001", "username":"demo_player_01", "entry_token":"qyz_fixture", "entry_fingerprint":"a"*64, "now":1000}
        def url(**changes):
            fields = {"user":json.dumps({"id":9100000001,"username":"demo_player_01"}), "start_param":"qyz_fixture", "auth_date":"999", "hash":"a"*64}
            fields.update(changes)
            return "https://asc.aiopenai.app/miniapp/xianxia-world-boss#" + urlencode({"tgWebAppData":urlencode(fields)})
        value = auth.parse_webview_auth(url(), **arguments)
        self.assertEqual(value.account, "tg9100000001")
        self.assertNotIn("auth_date", repr(value))
        self.assertNotIn("qyz_fixture", repr(value))
        for bad in (
            url(user=json.dumps({"id":9100000003,"username":"demo_player_01"})),
            url(user=json.dumps({"id":9100000001,"username":"demo_player_03"})),
            url(start_param="qyz_another"), url(auth_date="700"), url(auth_date="2000"),
            url().replace("https://", "http://"), url().replace("asc.aiopenai.app", "evil.invalid"),
            url().replace("/miniapp/xianxia-world-boss#", "/untrusted#"),
            url().replace("asc.aiopenai.app", "secret@asc.aiopenai.app"),
        ):
            with self.subTest(boundary="invalid_context"), self.assertRaises(ProtocolError):
                auth.parse_webview_auth(bad, **arguments)

    def test_official_qyz_router_keeps_original_signed_auth_in_memory(self):
        auth = importlib.import_module("qyz_auth")
        arguments = {"account":"tg9100000001", "username":"demo_player_01", "entry_token":"qyz_fixture", "entry_fingerprint":"a"*64, "now":1000}
        fields = {"user":json.dumps({"id":9100000001,"username":"demo_player_01", "first_name":"fixture + 測試"}), "start_param":"qyz_fixture", "auth_date":"999", "hash":"a"*64}
        original_init_data = urlencode(fields)
        fragment = urlencode({"tgWebAppData":original_init_data, "tgWebAppPlatform":"macos"})
        router = "https://asc.aiopenai.app/miniapp/xianxia-verify"
        for query in ("", "?tgWebAppStartParam=qyz_fixture", "?startapp=qyz_fixture"):
            with self.subTest(query=query):
                value = auth.parse_webview_auth(router + query + "#" + fragment, **arguments)
                self.assertEqual(value.account, "tg9100000001")
                self.assertEqual(value.init_data, original_init_data)
                self.assertEqual(value.entry_token, "qyz_fixture")
                self.assertNotIn(original_init_data, repr(value))
                direct = auth.parse_webview_auth(
                    router.replace("xianxia-verify", "xianxia-world-boss") + query + "#" + fragment,
                    **arguments,
                )
                self.assertEqual(value, direct)

    def test_router_rejects_ambiguous_foreign_and_non_qyz_context(self):
        auth = importlib.import_module("qyz_auth")
        arguments = {"account":"tg9100000001", "username":"demo_player_01", "entry_token":"qyz_fixture", "entry_fingerprint":"a"*64, "now":1000}
        base = "https://asc.aiopenai.app/miniapp/xianxia-verify"
        def fields(**extra):
            return urlencode({"user":json.dumps({"id":9100000001,"username":"demo_player_01"}),
                              "start_param":"qyz_fixture", "auth_date":"999", "hash":"a"*64, **extra})
        def url(query="", **extra):
            return base + query + "#" + urlencode({"tgWebAppData":fields(**extra)})
        bad_urls = [
            url("?startapp=qyz_other"), url("?start_param=verify_fixture"),
            url("?tgWebAppStartParam=qyz_other"),
            url("?startapp=qyz_fixture&startapp=qyz_other"),
            url() + "&tgWebAppData=duplicate", url() + "&startapp=fish_fixture",
            base + "?" + urlencode({"tgWebAppData":fields(), "startapp":"qyz_fixture"}),
            url(user=json.dumps({"id":9100000003,"username":"demo_player_01"})),
            url(user=json.dumps({"id":9100000001,"username":"demo_player_03"})),
            url(auth_date="700"), url(auth_date="2000"), url(hash="not-a-signature"),
            url(start_param="qyz_other"), url().replace("https://", "http://"),
            url().replace("asc.aiopenai.app", "evil.invalid"),
            url().replace("asc.aiopenai.app", "user@asc.aiopenai.app"),
            url().replace("asc.aiopenai.app", "asc.aiopenai.app:444"),
            url().replace("xianxia-verify#", "xianxia-verify/#"),
        ]
        for index, value in enumerate(bad_urls):
            with self.subTest(case=index), self.assertRaises(ProtocolError):
                auth.parse_webview_auth(value, **arguments)
        for token in ("verify_fixture", "fish_fixture", "qyz_", "qyz_has/slash", "qyz_" + "x"*160):
            with self.subTest(token_kind=token[:8]), self.assertRaises(ProtocolError):
                auth.parse_webview_auth(url(start_param=token), **{**arguments, "entry_token":token})


if __name__ == "__main__":
    unittest.main()
