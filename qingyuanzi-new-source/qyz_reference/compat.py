"""XIUXIAN bindings for the three Telegram reference scripts.

The reference battle code receives an account-owned HTTP callable. Telegram
authentication and source selection remain with the existing six-account host.
"""
from __future__ import annotations

import hashlib
import inspect
import re
import urllib.parse
from typing import Any


class MiniAppBeastError(RuntimeError):
    def __init__(self, code: str, status: int = 0):
        self.code = str(code) if re.fullmatch(r"[a-z0-9_]{1,80}", str(code)) else "request_failed"
        self.status = int(status or 0)
        self.details: dict[str, Any] = {}
        self.uncertain = False
        super().__init__(self.code)


class MiniAppCircuitOpenError(MiniAppBeastError):
    def __init__(self, code="miniapp_circuit_open", *, retry_at="", retry_after=0):
        super().__init__(code)
        self.retry_at, self.retry_after = retry_at, retry_after


async def _post_json(origin, path, payload, timeout, *, post_json=None,
                     time_critical=False, executor=None):
    if post_json is None:
        raise MiniAppBeastError("account_transport_required")
    result = post_json(origin, path, payload, timeout)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise MiniAppBeastError("server_response_unconfirmed")
    return result


def miniapp_origin(entry_url):
    from qyz_http import ORIGIN
    parsed = urllib.parse.urlsplit(str(entry_url))
    if parsed.scheme == "https" and parsed.netloc.lower() in {
        "t.me", "www.t.me", "telegram.me", "www.telegram.me",
    }:
        return ORIGIN
    if str(entry_url).rstrip("/") == ORIGIN:
        return ORIGIN
    raise MiniAppBeastError("invalid_entry_url")


def miniapp_circuit_preflight(*_args, **_kwargs):
    # The XIUXIAN host owns readiness. Never import a different game's circuit.
    return None


def world_boss_identities_for_account(account):
    from qingyuanzi_host import ACCOUNTS
    return ["主魂"] if account in ACCOUNTS else []


async def resolve_actor_target_chats(actor, *_args):
    from qingyuanzi_host import resolve_actor_target_chats as resolve
    return await resolve(actor)


def is_game_bot_sender(actor, sender):
    # The host admits a qyz_ deep link from any bot sender; there is no single
    # game bot username to compare against (it rotates between events).
    return getattr(sender, "bot", False) is True


async def request_webview_init_data(client, bot_username, entry_token):
    from qingyuanzi_host import ACCOUNTS, request_official_webview_url
    from qyz_auth import parse_webview_auth
    me = await client.get_me()
    account = "tg" + str(getattr(me, "id", ""))
    if account not in ACCOUNTS:
        raise MiniAppBeastError("account_binding_mismatch")
    url = await request_official_webview_url(client, bot_username, entry_token)
    auth = parse_webview_auth(url, account=account, username=ACCOUNTS[account]["username"],
                             entry_token=entry_token,
                             entry_fingerprint=hashlib.sha256(entry_token.encode()).hexdigest())
    return auth.init_data
