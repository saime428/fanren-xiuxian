"""Validate Telegram-returned WebView context in memory; never manufacture initData.

The game server verifies Telegram's signature. This boundary checks the response
of the authenticated Telegram RPC against the expected account and entry.
"""
from __future__ import annotations
import json
import re
import time
import sqlite3
from pathlib import Path
from urllib.parse import quote
from telethon.sessions import MemorySession
from telethon.crypto import AuthKey
from urllib.parse import parse_qsl, urlsplit

from qyz_http import ORIGIN, PAGE_PATH
from qyz_protocol import ProtocolError, WebViewAuth

ENTRY_ROUTER_PATH = "/miniapp/xianxia-verify"


def read_session_in_memory(path: Path) -> MemorySession:
    """Read an existing credential source without opening a SQLite writer or copying it."""
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("invalid session source")
        uri = "file:" + quote(str(path.absolute()), safe="/") + "?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as database:
            rows = database.execute("SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 2").fetchall()
        if len(rows) != 1:
            raise ValueError("ambiguous session")
        dc_id, address, port, key = rows[0]
        if not isinstance(key, bytes) or len(key) != 256 or not isinstance(dc_id, int) or dc_id <= 0:
            raise ValueError("invalid session")
        session = MemorySession()
        session.set_dc(dc_id, address, port)
        session.auth_key = AuthKey(data=key)
        return session
    except (OSError, ValueError, TypeError, sqlite3.Error):
        raise ProtocolError("telegram_session_unavailable", phase="identity") from None


def _unique_query(text: str) -> dict[str, str]:
    pairs = parse_qsl(text, keep_blank_values=True, max_num_fields=40)
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate parameters")
    return result


def parse_webview_auth(url: str, *, account: str, username: str, entry_token: str,
                       entry_fingerprint: str, now: float | None = None) -> WebViewAuth:
    try:
        if not isinstance(url, str) or not 1 <= len(url) <= 32768:
            raise ValueError("invalid URL")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != urlsplit(ORIGIN).hostname
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.path not in {PAGE_PATH, ENTRY_ROUTER_PATH}):
            raise ValueError("untrusted origin or path")
        outer = _unique_query(parsed.fragment)
        query = _unique_query(parsed.query)
        if set(query).intersection(outer):
            raise ValueError("ambiguous context")
        # The official router copies the fragment unchanged and routes qyz_
        # entries directly to the battle page before its drag UI initializes.
        # Parse that original signed context; never send it to a browser.
        if parsed.path == ENTRY_ROUTER_PATH and "tgWebAppData" not in outer:
            raise ValueError("router would discard query-only identity")
        outer.update(query)
        raw = outer.get("tgWebAppData", "")
        fields = _unique_query(raw)
        user = json.loads(fields.get("user", "null"))
        if not isinstance(user, dict) or not re.fullmatch(r"tg[0-9]+", account):
            raise ValueError("missing identity")
        if (isinstance(user.get("id"), bool) or user.get("id") != int(account[2:])
                or str(user.get("username", "")).casefold() != username.lstrip("@").casefold()):
            raise ValueError("identity mismatch")
        if (not isinstance(entry_token, str) or len(entry_token) > 160
                or not re.fullmatch(r"qyz_[A-Za-z0-9_-]+", entry_token)
                or fields.get("start_param") != entry_token):
            raise ValueError("entry mismatch")
        for key in ("tgWebAppStartParam", "startapp", "start_param"):
            if key in outer and outer[key] != entry_token:
                raise ValueError("entry mismatch")
        if not re.fullmatch(r"[0-9a-f]{64}", fields.get("hash", "")):
            raise ValueError("signature absent")
        issued = int(fields.get("auth_date", ""))
        age = (time.time() if now is None else now) - issued
        if not -30 <= age <= 120:
            raise ValueError("stale context")
        return WebViewAuth(account=account, init_data=raw, entry_token=entry_token,
                           entry_fingerprint=entry_fingerprint)
    except (ValueError, TypeError, OverflowError):
        raise ProtocolError("webview_identity_or_source_invalid", phase="webview") from None
