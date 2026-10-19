"""扫码登录：二维码过期自动换新、两步验证转密码表单、放弃时删掉没授权的会话文件；网页流程能走通到建号登录。

run: PYTHONPATH=app/src python tools/test_qr_login.py
"""
import asyncio
import importlib
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from telethon.errors import SessionPasswordNeededError  # noqa: E402
from tg_game.config import get_settings  # noqa: E402
from tg_game.storage import CompatDb, Storage  # noqa: E402
from tg_game.telegram import account  # noqa: E402
import biz_fanren_game  # noqa: E402
import biz_sect_game  # noqa: E402

ME = SimpleNamespace(id=9300000001, username="scan_user", first_name="扫码", last_name="", phone="")


class FakeQR:
    def __init__(self, outcomes):
        self.outcomes, self.round = list(outcomes), 0

    @property
    def url(self):
        return f"tg://login?token=t{self.round}"

    @property
    def expires(self):
        return datetime.now(timezone.utc) + timedelta(seconds=30)

    async def wait(self, timeout=None):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "sleep":
            await asyncio.sleep(timeout)
            raise asyncio.TimeoutError()
        return outcome

    async def recreate(self):
        self.round += 1


class FakeClient:
    def __init__(self, qr):
        self.qr = qr

    async def connect(self):
        pass

    async def qr_login(self):
        return self.qr

    def is_connected(self):
        return False

    @property
    def disconnected(self):
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future


async def check_account_helper(folder: Path):
    urls = []
    with patch.object(get_settings(), "database_path", folder / "t.db"):
        with patch.object(account, "build_client", lambda name: FakeClient(FakeQR([asyncio.TimeoutError(), ME]))):
            result = await account.run_qr_login("tg_game_login_qr_a", lambda url, exp: urls.append(url))
        assert urls == ["tg://login?token=t0", "tg://login?token=t1"], urls
        assert result["account"]["id"] == ME.id and result["account"]["session_name"] == "tg_game_login_qr_a"

        with patch.object(account, "build_client", lambda name: FakeClient(FakeQR([SessionPasswordNeededError(None)]))):
            result = await account.run_qr_login("tg_game_login_qr_b", lambda url, exp: None)
        assert result == {"requires_password": True, "session_name": "tg_game_login_qr_b"}

        leftover = folder / "tg_game_login_qr_c.session"
        leftover.write_bytes(b"unauthorized")
        with patch.object(account, "build_client", lambda name: FakeClient(FakeQR(["sleep"] * 5))):
            try:
                await account.run_qr_login("tg_game_login_qr_c", lambda url, exp: None, total_seconds=1.5)
                raise AssertionError("expected the QR login to give up")
            except TimeoutError:
                pass
        assert not leftover.exists(), "an abandoned QR login must not leave its session file"


async def check_web_flow(folder: Path):
    storage = Storage(folder / "web.db")
    storage.init_schema()
    db = CompatDb(storage)
    try:
        biz_fanren_game.ensure_tables(db)
        biz_sect_game.ensure_tables(db)
    finally:
        db.close()
    web = importlib.import_module("tg_game.web.app")

    async def fake_run(session_name, on_url, total_seconds=180):
        on_url("tg://login?token=web", time.time() + 30)
        await asyncio.sleep(0.2)
        return {"requires_password": False, "account": {"id": ME.id, "username": ME.username, "session_name": session_name}}

    async def fake_info(session_name, allow_fallback=True):
        assert allow_fallback is False and session_name.startswith("tg_game_login_qr_")
        return {"id": ME.id, "username": ME.username, "first_name": "扫码", "phone": "", "session_name": session_name}

    with patch.multiple(get_settings(), database_path=storage.path, bound_chat_id=None), \
            patch.object(web, "run_qr_login", fake_run), patch.object(web, "get_authorized_account_info", fake_info):
        transport = httpx.ASGITransport(app=web.create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/auth/telegram/qr/status")).json() == {"status": "missing"}
            started = await client.post("/auth/telegram/qr/start")
            assert started.status_code == 303
            first = (await client.get("/auth/telegram/qr/status")).json()
            assert first["status"] in ("qr_starting", "qr_pending"), first
            for _ in range(50):
                state = (await client.get("/auth/telegram/qr/status")).json()
                if state["status"] == "qr_done":
                    break
                await asyncio.sleep(0.05)
            assert state["status"] == "qr_done", state
            done = await client.get("/auth/telegram/qr/complete")
            assert done.status_code == 303 and done.headers["location"] == "/features?new=1", done.headers  # 新号先去勾功能
            assert (await client.get("/auth/telegram/qr/status")).json() == {"status": "missing"}
            page = await client.get("/login")
            assert "扫码登录" in page.text
    profile = next(p for p in storage.list_profiles() if str(p.telegram_user_id) == str(ME.id))
    assert profile.telegram_username == ME.username
    assert profile.telegram_session_name.startswith("tg_game_login_qr_")


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        await check_account_helper(Path(tmp))
        await check_web_flow(Path(tmp))
    print("qr login: ok")


if __name__ == "__main__":
    asyncio.run(main())
