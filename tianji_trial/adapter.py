#!/usr/bin/env python3
"""Local account/WebView adapter for Tianji Trial.

Telegram sessions and signed WebView values stay in memory or disposable session
copies. Production discovery reuses the audited canonical dwelling-entry logic
from the independent fishing integration; no bot-history fallback is added here.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
import shutil
import tempfile
import http.client
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from fishing.fishing_accounts import read_account_env, sweep_stale_session_copies, telegram_client
from fishing.miniapp_fishing_integration import UpstreamFishingProtocol
from miniapp_shared.accounts import ALL_ACCOUNTS, CREDENTIAL_SOURCE, DWELLING_SESSIONS
from miniapp_shared.dwelling import open_dwelling
from miniapp_shared.errors import MiniAppError
from transport import disconnect_telegram_client, ensure_connected  # root transport.py, not .transport

from .protocol import TianjiTrialError
from .runtime import Identity
from .transport import AccountAuth


API_ORIGIN = "https://asc.aiopenai.app"
ALLOWED_PATHS = {
    "/api/miniapp/xianxia-dwelling/start",
    "/api/miniapp/xianxia-dwelling/external",
    "/api/miniapp/xianxia-trial/start",
    "/api/miniapp/xianxia-trial/finish",
}
ACCOUNT_CONFIG = {account: {"source": CREDENTIAL_SOURCE[account], "session": DWELLING_SESSIONS[account]}
                  for account in ALL_ACCOUNTS}
_ERROR_CODE = re.compile(r"^[a-z0-9_]{3,80}$")


def parse_identities(payload: Any) -> tuple[Identity, ...]:
    identity = payload.get("identity") if isinstance(payload, dict) else None
    choices = identity.get("choices") if isinstance(identity, dict) else None
    if not isinstance(choices, list):
        raise TianjiTrialError("miniapp_identity_missing")
    result: list[Identity] = []
    seen: set[int] = set()
    for item in choices:
        if not isinstance(item, dict):
            continue
        raw_player_id = item.get("playerId")
        try:
            player_id = int(str(raw_player_id))
        except (TypeError, ValueError):
            continue
        if player_id <= 0 or player_id in seen:
            raise TianjiTrialError("miniapp_identity_invalid")
        seen.add(player_id)
        label = str(item.get("daoName") or item.get("displayName") or item.get("username") or player_id).strip()
        result.append(Identity(player_id=player_id, label=label, source=str(item.get("source") or "unknown")))
    if not result:
        raise TianjiTrialError("miniapp_identity_missing")
    return tuple(result)


def _server_error(payload: Any, default: str) -> str:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.extend((payload.get("code"), payload.get("error")))
        nested = payload.get("data")
        if isinstance(nested, dict):
            candidates.extend((nested.get("code"), nested.get("error")))
    for candidate in candidates:
        code = str(candidate or "").strip().lower()
        if _ERROR_CODE.fullmatch(code):
            return code
    return default


class LiveAccountBridge:
    def __init__(
        self,
        project_root: Path,
        account: str,
        *,
        http_transport: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        if account not in ACCOUNT_CONFIG:
            raise TianjiTrialError("account_unknown")
        self.project_root = Path(project_root)
        self.account = account
        self.http_transport = http_transport
        self.client: Any = None
        self.copy_dir: Path | None = None
        self._entry_protocol = UpstreamFishingProtocol()

    async def __aenter__(self) -> "LiveAccountBridge":
        await self._connect()
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        await self.close()

    async def _connect(self) -> None:
        if self.client is not None:
            return
        config = ACCOUNT_CONFIG[self.account]
        source_session = self.project_root / str(config["session"])
        if not source_session.is_file():
            raise TianjiTrialError("dedicated_session_missing")
        env = read_account_env(str(config["source"]))
        copies = self.project_root / "tianji_trial" / "state" / "session_copies"
        copies.mkdir(parents=True, exist_ok=True)
        sweep_stale_session_copies(copies)
        # __aenter__ failing never reaches __aexit__: everything after the copy
        # (which holds the account's auth key) must clean up here.
        self.copy_dir = Path(tempfile.mkdtemp(prefix=f"{self.account}-", dir=copies))
        try:
            copy_base = self.copy_dir / "session"
            shutil.copy2(source_session, copy_base.with_suffix(".session"))
            self.client = telegram_client(
                str(copy_base),
                int(env["TG_API_ID"]),
                env["TG_API_HASH"],
                request_retries=1,
                connection_retries=1,
                retry_delay=1,
            )
            await ensure_connected(self.client)  # the fleet's bounded handshake gate
            if not await self.client.is_user_authorized():
                raise TianjiTrialError("session_not_authorized")
            me = await self.client.get_me()
            if int(getattr(me, "id", 0) or 0) != int(self.account.removeprefix("tg")):
                raise TianjiTrialError("session_identity_mismatch")
        except BaseException as exc:
            with contextlib.suppress(Exception):  # keep the original error
                await self.close()
            if isinstance(exc, Exception) and not isinstance(exc, TianjiTrialError):
                raise TianjiTrialError("telegram_connect_failed", uncertain=True) from exc
            raise

    async def close(self) -> None:
        client, self.client = self.client, None
        copy_dir, self.copy_dir = self.copy_dir, None
        try:
            if client is not None:
                await disconnect_telegram_client(client)
        finally:
            if copy_dir is not None:
                shutil.rmtree(copy_dir, ignore_errors=True)

    async def authenticate(self, force: bool = False) -> AccountAuth:
        del force
        await self._connect()

        async def start(path: str, payload: dict[str, Any], *, mutation: bool) -> dict[str, Any]:
            del mutation  # dwelling /start is a read
            return await self.post_json(path, payload)

        try:
            # The shared chain: cached entry first, the next candidate when a
            # rotated entry's token is rejected (not just the newest one).
            session = await open_dwelling(self, start)
        except TianjiTrialError:
            raise
        except MiniAppError as exc:
            raise TianjiTrialError(exc.code, uncertain=exc.uncertain) from exc
        except Exception as exc:
            raise TianjiTrialError("dwelling_auth_failed", uncertain=True) from exc
        return AccountAuth(init_data=session.init_data, dwelling_token=session.entry_token,
                           identities=session.identities)

    async def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path not in ALLOWED_PATHS:
            raise TianjiTrialError("endpoint_not_allowed")
        if self.http_transport is not None:
            value = self.http_transport(path, dict(payload))
            value = await value if inspect.isawaitable(value) else value
            if not isinstance(value, dict):
                raise TianjiTrialError("bad_response")
            return value

        def request() -> dict[str, Any]:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                f"{API_ORIGIN}{path}",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Origin": API_ORIGIN,
                    "Referer": f"{API_ORIGIN}/miniapp/xianxia-dwelling",
                    "User-Agent": "Mozilla/5.0 Telegram-Android/11.0",
                    "Connection": "close",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as response:
                    raw = response.read(256 * 1024)
            except urllib.error.HTTPError as exc:
                try:
                    error_payload = json.loads(exc.read(256 * 1024).decode("utf-8", errors="replace"))
                except Exception:
                    error_payload = {}
                raise TianjiTrialError(_server_error(error_payload, f"http_{exc.code}")) from exc
            except (TimeoutError, OSError, urllib.error.URLError, http.client.HTTPException) as exc:
                raise TianjiTrialError("request_failed", uncertain=True) from exc
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise TianjiTrialError("bad_response") from exc
            if not isinstance(value, dict):
                raise TianjiTrialError("bad_response")
            if value.get("ok") is False:
                raise TianjiTrialError(_server_error(value, "request_failed"))
            return value

        return await asyncio.to_thread(request)
