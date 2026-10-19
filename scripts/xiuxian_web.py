#!/usr/bin/env python3
"""本地网页：登录账号、勾选功能、一键生成并安装定时任务。

    .venv/bin/python3 scripts/xiuxian_web.py         # 浏览器打开 http://127.0.0.1:8765
    ssh -L 8765:127.0.0.1:8765 <服务器>               # 跑在 VPS 上时，从自己电脑转发过去再打开

It listens on loopback only: the page carries API hashes and Telegram login
codes. Every request must also carry the per-start token embedded in the page
and a loopback Host header, so another website open in the same browser cannot
drive it (CSRF) or read it through a rebound DNS name.

Edits go through scripts/xiuxian_accounts.py's validation; "apply" runs its
render and install commands in a fresh process, so they always see the saved config.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import xiuxian_accounts as cli  # noqa: E402

PAGE = Path(__file__).with_name("xiuxian_web.html")
MAX_BODY = 64 * 1024
# Options the page edits; any other option already in accounts.json is kept as is.
OPTION_FIELDS = {"jiyin_choice": ("reply",), "shuangxiu_wenyang": ("partner",)}


def default_client(session: Path, api_id: int, api_hash: str):
    from fishing.fishing_accounts import load_transport_env
    from transport import telegram_client
    load_transport_env()
    return telegram_client(str(session), api_id, api_hash)


class App:
    def __init__(self, *, out: Path = ROOT, install_extra: tuple[str, ...] = (),
                 client_factory: Callable[[Path, int, str], Any] = default_client):
        self.out = Path(out)
        self.install_extra = list(install_extra)
        self.client_factory = client_factory
        self.token = secrets.token_urlsafe(24)
        self.pending: dict[str, dict[str, Any]] = {}
        # ponytail: one lock for every config write; the page has one user.
        self.lock = threading.Lock()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def wait(self, coro, timeout: float = 90):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # ------------------------------------------------------------ reads

    def env_path(self, source: str) -> Path:
        return cli.config_file().parent / f"{source}.env"

    def state(self, _body=None) -> dict[str, Any]:
        acc = cli.accounts_module()
        payload = cli.load_config()
        accounts = []
        for row in payload["accounts"]:
            source = row.get("credential_source", row["id"])
            session = self.out / (row.get("session") or f"state/{source}.session")
            env = self.env_path(source)
            accounts.append({"id": row["id"], "username": row["username"], "credential_source": source,
                             "features": row.get("features", {}), "logged_in": session.is_file(),
                             "api_ready": env.is_file() and "TG_API_HASH=''" not in env.read_text(encoding="utf-8")})
        features = [{"key": key, "name": name, "note": cli.NOTES.get(key, ""),
                     "group": "account" if key in cli.TEMPLATE else "daily",
                     "single": key in acc.SINGLE_ACCOUNT_FEATURES} for key, name in acc.FEATURES.items()]
        game = {**acc.GAME_DEFAULTS, **(payload.get("game") or {})}
        return {"game": game, "game_is_placeholder": game["group"] == acc.GAME_DEFAULTS["group"],
                "notify": payload.get("notify") or {}, "accounts": accounts, "features": features,
                "jiyin_replies": list(cli.JIYIN_REPLIES),
                "scheduler": "launchd" if sys.platform == "darwin" else "systemd"}

    # ------------------------------------------------------------ writes

    def _save(self, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self.lock:
            payload = cli.load_config()
            change(payload)
            cli.save_config(payload)
        return self.state()

    def game(self, body: dict[str, Any]) -> dict[str, Any]:
        def change(payload):
            payload["game"] = {"group": str(body.get("group", "")).strip().lstrip("@"),
                               **{key: _int(body.get(key), key) for key in ("chat_id", "topic_id", "second_chat_id")}}
        return self._save(change)

    def notify(self, body: dict[str, Any]) -> dict[str, Any]:
        def change(payload):
            payload["notify"] = {key: str(body[key]).strip() for key in ("target", "hermes_bin")
                                 if str(body.get(key) or "").strip()}
        return self._save(change)

    def features(self, body: dict[str, Any]) -> dict[str, Any]:
        """Set one account's features from the page's checkboxes and option fields."""
        wanted = body.get("features")
        if not isinstance(wanted, dict):
            raise ValueError("features must be an object")

        def change(payload):
            row = cli.find_account(payload, str(body.get("id")))
            old = row.get("features", {})
            new = {}
            for key, value in wanted.items():
                if key not in cli.accounts_module().FEATURES:
                    raise ValueError(f"unknown feature {key}")
                if value is False:
                    continue
                kept = dict(old[key]) if isinstance(old.get(key), dict) else {}
                if isinstance(value, dict):
                    for field in OPTION_FIELDS.get(key, ()):
                        if value.get(field):
                            kept[field] = value[field]
                        else:
                            kept.pop(field, None)
                new[key] = kept or True
            row["features"] = new
        return self._save(change)

    def remove(self, body: dict[str, Any]) -> dict[str, Any]:
        def change(payload):
            cli.find_account(payload, str(body.get("id")))
            payload["accounts"] = [row for row in payload["accounts"] if row["id"] != body.get("id")]
        return self._save(change)

    # ------------------------------------------------------------ Telegram login

    def login_start(self, body: dict[str, Any]) -> dict[str, Any]:
        api_id = _int(body.get("api_id"), "api_id")
        api_hash = str(body.get("api_hash", "")).strip()
        phone = re.sub(r"[\s-]", "", str(body.get("phone", "")))
        if not re.fullmatch(r"[0-9a-f]{32}", api_hash):
            raise ValueError("API hash 应是 32 位十六进制")
        if not re.fullmatch(r"\+?\d{6,15}", phone):
            raise ValueError("手机号带国家码，例如 +8613800000000")
        login = secrets.token_urlsafe(12)
        (self.out / "state").mkdir(parents=True, exist_ok=True)
        session = self.out / "state" / f"login-{login}"
        client = self.client_factory(session, api_id, api_hash)

        async def start():
            await client.connect()
            return await client.send_code_request(phone)

        sent = self.wait(start())
        self.pending[login] = {"client": client, "session": session, "phone": phone, "api_id": api_id,
                               "api_hash": api_hash, "hash": sent.phone_code_hash, "at": time.time()}
        return {"login": login}

    def login_code(self, body: dict[str, Any]) -> dict[str, Any]:
        from telethon.errors import SessionPasswordNeededError
        pending = self.pending.get(str(body.get("login")))
        if pending is None:
            raise ValueError("登录已过期，请重新发送验证码")
        client = pending["client"]
        code, password = str(body.get("code", "")).strip(), str(body.get("password", ""))

        async def sign_in():
            if not pending.get("need_password"):
                try:
                    await client.sign_in(phone=pending["phone"], code=code, phone_code_hash=pending["hash"])
                except SessionPasswordNeededError:
                    pending["need_password"] = True  # the code was right; two-step password next
            if pending.get("need_password"):
                if not password:
                    return None
                await client.sign_in(password=password)
            me = await client.get_me()
            await client.disconnect()
            return me

        me = self.wait(sign_in())
        if me is None:
            return {"need_password": True}
        self.pending.pop(str(body.get("login")))
        account = f"tg{me.id}"
        if not me.username:
            raise ValueError("这个 Telegram 账号没有用户名，请先在 Telegram 设置里设一个再登录")
        if account in cli.demo_ids():
            raise ValueError(f"{account} 是仓库示例账号")
        seed = self.out / "state" / f"{account}.session"
        if seed.exists():
            seed.replace(seed.with_name(f"{seed.name}.bak-{int(time.time())}"))
        pending["session"].with_suffix(".session").replace(seed)
        os.chmod(seed, 0o600)
        env = self.env_path(account)
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text(f"export XIU_ACCOUNT_SLUG='{account}'\n\n# From https://my.telegram.org\n"
                       f"export TG_API_ID='{pending['api_id']}'\nexport TG_API_HASH='{pending['api_hash']}'\n",
                       encoding="utf-8")
        os.chmod(env, 0o600)

        def change(payload):
            row = next((row for row in payload["accounts"] if row["id"] == account), None)
            if row is None:
                payload["accounts"].append({"id": account, "username": me.username, "features": {}})
            else:
                row["username"] = me.username
        state = self._save(change)
        return {**state, "added": account}

    # ------------------------------------------------------------ apply

    def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        scheduler = body.get("scheduler")
        if scheduler not in ("launchd", "systemd"):
            raise ValueError("scheduler must be launchd or systemd")
        command = [sys.executable, str(ROOT / "scripts/xiuxian_accounts.py")]
        log, ok = [], True
        for args in (["render", "--out", str(self.out)],
                     ["install", f"--{scheduler}", "--out", str(self.out), *self.install_extra]):
            result = subprocess.run([*command, *args], cwd=ROOT, capture_output=True, text=True, timeout=300)
            log.append(f"$ xiuxian_accounts.py {args[0]}\n{result.stdout}{result.stderr}")
            if result.returncode:
                ok = False
                break
        return {**self.state(), "ok": ok, "log": "\n".join(log)}


def _int(value: Any, name: str) -> int:
    try:
        return int(str(value).strip())
    except ValueError:
        raise ValueError(f"{name} 必须是整数") from None


ACTIONS = ("state", "game", "notify", "features", "remove", "login_start", "login_code", "apply")


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _trusted(self) -> bool:
            # Any port (an ssh -L forward may use another one), but only a loopback name:
            # a rebound attacker domain arrives with its own Host header.
            host = re.sub(r":\d+$", "", self.headers.get("Host") or "")
            return host in {"127.0.0.1", "localhost"}

        def do_GET(self) -> None:
            if not self._trusted():
                return self._json(403, {"error": "forbidden host"})
            if self.path != "/":
                return self._json(404, {"error": "not found"})
            page = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", app.token)
            self._send(200, page.encode(), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            name = self.path.removeprefix("/api/")
            if not self._trusted() or self.headers.get("X-Token") != app.token:
                return self._json(403, {"error": "forbidden"})
            if name not in ACTIONS:
                return self._json(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("expected a JSON object")
                self._json(200, getattr(app, name)(body))
            except (ValueError, SystemExit) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - shown to the one local user
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            pass

    return Handler


def serve(app: App, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    server = serve(App(), args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"打开 {url}  （Ctrl+C 退出）")
    if not args.no_browser:
        import webbrowser
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
