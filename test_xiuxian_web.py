"""scripts/xiuxian_web.py: login, feature checkboxes, apply, and the loopback/token guard."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from telethon.errors import SessionPasswordNeededError

from scripts import xiuxian_web as web


class FakeClient:
    """Telethon's login calls; the first code needs a two-step password."""

    def __init__(self, session, api_id, api_hash):
        self.session, self.signed_in = Path(session), False

    async def connect(self):
        self.session.with_suffix(".session").write_bytes(b"sqlite")

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash="hash-1")

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is None:
            assert (code, phone_code_hash) == ("12345", "hash-1")
            raise SessionPasswordNeededError(request=None)
        assert password == "secret"
        self.signed_in = True

    async def get_me(self):
        assert self.signed_in
        return SimpleNamespace(id=9300000001, username="first_cultivator")

    async def disconnect(self):
        pass


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"XIUXIAN_ACCOUNTS_FILE": str(self.out / "config/accounts/accounts.json")})
        env.start()
        self.addCleanup(env.stop)
        self.app = web.App(out=self.out, install_extra=("--dry-run", "--dest", str(self.out / "units")),
                           client_factory=FakeClient)
        self.server = web.serve(self.app, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, name, body=None, *, token=None, host=None):
        headers = {"Content-Type": "application/json", "X-Token": token or self.app.token}
        if host:
            headers["Host"] = host
        request = urllib.request.Request(f"{self.base}/api/{name}", json.dumps(body or {}).encode(), headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_page_carries_the_token_and_other_origins_are_refused(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            self.assertIn(self.app.token, response.read().decode())
        self.assertEqual(403, self.call("state", token="wrong")[0])
        self.assertEqual(403, self.call("state", host="rebound.example:8765")[0])
        self.assertEqual(200, self.call("state", host="localhost:9000")[0])

    def test_login_checkboxes_and_apply(self):
        status, started = self.call("login_start", {"api_id": "123", "api_hash": "0" * 32, "phone": "+86 138 0000 0000"})
        self.assertEqual(200, status, started)
        self.assertEqual({"need_password": True}, self.call("login_code", {"login": started["login"], "code": "12345"})[1])
        status, state = self.call("login_code", {"login": started["login"], "password": "secret"})
        self.assertEqual((200, "tg9300000001"), (status, state["added"]), state)
        account = state["accounts"][0]
        self.assertEqual(("first_cultivator", True, True), (account["username"], account["logged_in"], account["api_ready"]))
        env_file = self.out / "config/accounts/tg9300000001.env"
        self.assertIn("export TG_API_HASH='" + "0" * 32 + "'", env_file.read_text())
        self.assertEqual(0o600, env_file.stat().st_mode & 0o777)

        status, error = self.call("features", {"id": "tg9300000001", "features": {"shuangxiu_wenyang": True}})
        self.assertEqual(400, status)
        self.assertIn("partner", error["error"])
        status, state = self.call("features", {"id": "tg9300000001", "features": {
            "shendubiguan": True, "tower": True, "fishing": False, "jiyin_choice": {"reply": ".献上魂魄"}}})
        self.assertEqual(200, status, state)
        self.assertEqual({"shendubiguan": True, "tower": True, "jiyin_choice": {"reply": ".献上魂魄"}},
                         state["accounts"][0]["features"])
        status, state = self.call("game", {"group": "@real_group", "chat_id": "-1009300000042", "topic_id": "42",
                                           "second_chat_id": "-1009300000043"})
        self.assertEqual((200, False), (status, state["game_is_placeholder"]), state)

        status, applied = self.call("apply", {"scheduler": "systemd"})
        self.assertTrue(applied.get("ok"), applied.get("log") or applied)
        units = {path.name for path in (self.out / "units").iterdir()}
        self.assertIn("ai.hermes.xiuxian.tg9300000001.shendubiguan.send.timer", units)
        self.assertIn("ai.hermes.xiuxian.tower-miniapp.daily.timer", units)
        self.assertIn("'.献上魂魄'", (self.out / "scripts/send_jiyin_choice_tg9300000001.sh").read_text())


if __name__ == "__main__":
    unittest.main()
