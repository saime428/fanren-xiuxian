#!/usr/bin/env python3
"""Shared account-scoped 洞府 (dwelling) MiniApp session helpers.

Every MiniApp flow package (``star_observatory``, ``tower_miniapp``,
``journey_miniapp``) reuses the audited chain already used by the Tianji Trial
and fishing integrations: dedicated Telethon session copy → pinned group
dwelling entry → ``RequestMainWebView`` initData →
``/api/miniapp/xianxia-dwelling/start``. Child MiniApps are entered only through
``/api/miniapp/xianxia-dwelling/external`` for one explicit ``playerId``.

Signed values (initData, entry tokens, child tokens, full launch URLs) stay in
memory; state files and logs only ever receive sanitized summaries.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import inspect
import json
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from .accounts import ACCOUNT_USERNAMES, ALL_ACCOUNTS, DWELLING_SESSIONS  # noqa: F401 - re-exported for flow packages
from .errors import (  # noqa: F401 - re-exported: flow packages import these from here
    DWELLING_BOT_PATTERN,
    MiniAppError,
    error_code,
    is_uncertain,
    safe_text,
    server_error_code,
)

if TYPE_CHECKING:  # the Telethon/Tianji chain is imported only by make_bridge/open_dwelling
    from tianji_trial.adapter import LiveAccountBridge
    from tianji_trial.runtime import Identity

API_HOST = "asc.aiopenai.app"
API_ORIGIN = f"https://{API_HOST}"
DWELLING_START = "/api/miniapp/xianxia-dwelling/start"
DWELLING_DETAILS = "/api/miniapp/xianxia-dwelling/details"
DWELLING_EXTERNAL = "/api/miniapp/xianxia-dwelling/external"
DWELLING_JOURNEY = "/api/miniapp/xianxia-dwelling/journey"

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{4,200}$")


class JsonTransport:
    """Allowlisted JSON POST transport against the official MiniApp origin.

    ``allowed`` maps every permitted path to whether it mutates game state. A
    request to an unknown path, or with the wrong mutation flag, fails before any
    network activity. Transport failures on mutations are ``uncertain``.
    """

    def __init__(
        self,
        allowed: Mapping[str, bool],
        *,
        timeout: float = 20.0,
        post: Callable[..., Any] | None = None,
        origin: str = API_ORIGIN,
    ) -> None:
        self.allowed = {str(path): bool(flag) for path, flag in allowed.items()}
        self.timeout = float(timeout)
        self.post = post
        self.origin = str(origin)
        self._reward_contexts = {}

    def bind_reward_account(self, account: str, init_data: str, root: Path):
        if account not in ACCOUNT_USERNAMES:
            raise MiniAppError('reward_account_unknown')
        self._reward_contexts[hashlib.sha256(init_data.encode()).hexdigest()] = (account, Path(root))

    def _record_rewards(self, path, payload, reply, *, mutation, request_id):
        """Observe explicit successful reward fields without recording auth data."""
        if not mutation or self.origin != API_ORIGIN:
            return
        context = self._reward_contexts.get(hashlib.sha256(str(payload.get('initData') or '').encode()).hexdigest())
        if context is None or not isinstance(reply, dict) or reply.get('ok') is False:
            return
        root = reply.get('data') if isinstance(reply.get('data'), dict) else reply
        scopes = [root.get('actionResult'), root.get('result')]
        if not any(isinstance(scope, dict) for scope in scopes):
            scopes = [root]
        from rare_item_notice import structured_gains, spool_rewards
        account, project_root = context
        for scope in scopes:
            if (not isinstance(scope, dict) or scope.get('ok') is False or scope.get('error')
                    or scope.get('completed') is False or scope.get('handled') is False):
                continue
            if not (reply.get('ok') is True or scope.get('ok') is True or scope.get('completed') is True):
                continue
            rewards = structured_gains(scope, account)
            receipt_id = next((scope.get(k) for k in ('receiptId','transactionId','challengeId','resultId','settlementId')
                               if isinstance(scope.get(k), (str,int)) and not isinstance(scope.get(k),bool)), request_id)
            spool_rewards(project_root, rewards, source='miniapp', source_id=f'{path}:{receipt_id}')

    async def __call__(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        mutation: bool,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        expected = self.allowed.get(str(path))
        if expected is None or expected is not bool(mutation):
            raise MiniAppError("endpoint_not_allowed")
        request_id = uuid.uuid4().hex
        if self.post is not None:
            value = self.post(path, dict(payload), mutation=bool(mutation))
            value = await value if inspect.isawaitable(value) else value
            if not isinstance(value, dict):
                raise MiniAppError("bad_response", uncertain=bool(mutation))
        else:
            value = await asyncio.to_thread(
                self._post, str(path), dict(payload), bool(mutation), float(timeout or self.timeout)
            )
        try:
            self._record_rewards(path, payload, value, mutation=mutation, request_id=request_id)
        except Exception as exc:
            print(f'Rare reward queue failed: {type(exc).__name__}')
        return value

    def _post(self, path: str, payload: dict[str, Any], mutation: bool, timeout: float) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.origin}{path}",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": self.origin,
                "Referer": f"{self.origin}/miniapp/xianxia-dwelling",
                "User-Agent": "Mozilla/5.0 Telegram-Android/11.0",
                "Connection": "close",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(512 * 1024)
        except urllib.error.HTTPError as exc:
            try:
                error_payload = json.loads(exc.read(512 * 1024).decode("utf-8", errors="replace"))
            except Exception:
                error_payload = {}
            if (mutation and isinstance(error_payload, dict)
                    and isinstance(error_payload.get("actionResult"), dict)):
                # A business rejection carried in an HTTP error body is a
                # certain, classifiable outcome for the caller.
                return error_payload
            # A 5xx after a mutation may or may not have been applied.
            raise MiniAppError(server_error_code(error_payload, f"http_{exc.code}"),
                               uncertain=bool(mutation and exc.code >= 500)) from exc
        except (TimeoutError, OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            # IncompleteRead/BadStatusLine are not OSErrors; a mutation may still
            # have been applied, so the caller must treat it as uncertain.
            raise MiniAppError("request_failed", uncertain=True) from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise MiniAppError("bad_response", uncertain=mutation) from exc
        if not isinstance(value, dict):
            raise MiniAppError("bad_response", uncertain=mutation)
        if value.get("ok") is False and not isinstance(value.get("actionResult"), dict):
            raise MiniAppError(server_error_code(value, "request_failed"))
        return value


@dataclass(frozen=True)
class DwellingSession:
    account: str
    init_data: str = field(repr=False)
    entry_token: str = field(repr=False)
    start_payload: dict[str, Any]
    identities: tuple[Identity, ...]

    @property
    def account_id(self) -> int:
        return int(str(self.account).removeprefix("tg"))

    def personal_identity(self) -> Identity:
        for identity in self.identities:
            if int(identity.player_id) == self.account_id:
                return identity
        for identity in self.identities:
            if identity.source == "personal":
                return identity
        raise MiniAppError("identity_missing")

    def base_payload(self, player_id: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"token": self.entry_token, "initData": self.init_data}
        if player_id is not None:
            payload["playerId"] = int(player_id)
        return payload


# Server codes that mean "this entry token is no longer valid" (the pinned
# 【洞府公共入口】 rotates roughly monthly); the next-newest candidate is tried.
ENTRY_REJECTED_CODES = frozenset({
    "dwelling_token_missing", "dwelling_token_invalid", "dwelling_token_expired", "token_missing",
    "token_invalid", "token_expired", "invalid_token", "entry_expired", "http_404", "http_401", "http_403",
})
MAX_ENTRY_CANDIDATES = 3


def _raise_flood_wait(exc: BaseException) -> None:
    """A FloodWait stops entry discovery: trying the next group or entry only adds to the wait."""
    from telethon.errors import FloodError
    if isinstance(exc, FloodError):
        print(f"Telegram flood wait during dwelling entry discovery: {int(getattr(exc, 'seconds', 0) or 0)}s")
        raise MiniAppError("telegram_flood_wait") from exc


async def _entry_candidates(protocol: Any, client: Any, *, refresh: bool) -> list[dict[str, Any]]:
    listing = getattr(protocol, "_dwelling_entry_candidates", None)
    try:
        if listing is not None and getattr(protocol, "pinned_cave_entry", None) is None:
            candidates = await (listing(client, refresh=True) if refresh else listing(client))
            return list(candidates or [])[:MAX_ENTRY_CANDIDATES]
        return [dict(await protocol._pinned_cave_entry(client) or {})]
    except MiniAppError:
        raise
    except Exception as exc:
        _raise_flood_wait(exc)
        raise MiniAppError("dwelling_entry_failed", uncertain=True) from exc


async def first_working_entry(protocol: Any, client: Any, attempt: Callable[[dict[str, Any], str, str], Any]) -> Any:
    """Return ``await attempt(entry, token, init_data)`` for the first official entry that works.

    Entries are tried newest-first; ``attempt`` raises a MiniAppError with an
    ``ENTRY_REJECTED_CODES`` code to move on, so a rotated 【洞府公共入口】 is
    picked up without any code change. Discovery reads the cached entry first;
    when only that entry was tried and it failed, the groups are scanned again.
    """
    last_error: MiniAppError | None = None
    for refresh in (False, True):
        candidates = await _entry_candidates(protocol, client, refresh=refresh)
        for entry in candidates:
            token = str((entry or {}).get("token") or "").strip()
            if not _TOKEN.fullmatch(token):
                last_error = MiniAppError("dwelling_entry_invalid")
                continue
            try:
                init_data = str(await protocol._cave_webview(client, entry) or "").strip()
            except MiniAppError as exc:
                last_error = exc
                continue
            except Exception as exc:
                _raise_flood_wait(exc)
                last_error = MiniAppError("dwelling_entry_failed", uncertain=True)
                last_error.__cause__ = exc
                continue
            if not init_data:
                last_error = MiniAppError("dwelling_entry_invalid")
                continue
            try:
                result = await attempt(entry, token, init_data)
            except MiniAppError as exc:
                if exc.code not in ENTRY_REJECTED_CODES:
                    raise
                last_error = exc
                continue
            remember = getattr(protocol, "_remember_entry", None)
            if remember is not None and entry.get("source"):
                try:
                    remember(entry)
                except Exception:
                    pass
            return result
        if refresh or not any(entry.get("source") == "cache" for entry in candidates):
            break
    raise last_error or MiniAppError("dwelling_entry_failed", uncertain=True)


async def open_dwelling(bridge: Any, transport: JsonTransport) -> DwellingSession:
    """Resolve the current official dwelling entry and authenticate ``/start``."""
    async def start(entry: dict[str, Any], token: str, init_data: str) -> DwellingSession:
        payload = await transport(DWELLING_START, {"token": token, "initData": init_data}, mutation=False)
        from tianji_trial.adapter import parse_identities
        return DwellingSession(
            account=str(bridge.account),
            init_data=init_data,
            entry_token=token,
            start_payload=payload,
            identities=parse_identities(payload),  # raises TianjiTrialError, a MiniAppError
        )

    session = await first_working_entry(bridge._entry_protocol, bridge.client, start)
    if isinstance(transport, JsonTransport):
        transport.bind_reward_account(str(bridge.account), session.init_data,
                                      Path(getattr(bridge, 'project_root', Path(__file__).resolve().parents[1])))
    return session


def extract_child_token(url: Any, *, web_path: str, prefixes: tuple[str, ...]) -> str:
    raw = str(url or "").strip()
    if raw.startswith("/"):
        raw = f"{API_ORIGIN}{raw}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    token = ""
    for part in (parsed.fragment, parsed.query):
        query = parse_qs(part, keep_blank_values=True)
        for key in ("tgWebAppStartParam", "startapp", "start_param"):
            values = query.get(key) or []
            if values:
                token = str(values[0]).strip()
                break
        if token:
            break
    api_launch = host == API_HOST and parsed.path.rstrip("/") == web_path
    bot_launch = (host in {"t.me", "telegram.me"}
                  and DWELLING_BOT_PATTERN.fullmatch(parsed.path.strip("/")) is not None)
    if (parsed.scheme != "https" or not (api_launch or bot_launch)
            or not token.startswith(tuple(prefixes)) or not _TOKEN.fullmatch(token)):
        raise MiniAppError("child_launch_invalid")
    return token


async def mint_child_token(
    transport: JsonTransport,
    session: DwellingSession,
    *,
    player_id: int,
    action: str,
    web_path: str,
    prefixes: tuple[str, ...],
) -> str:
    """Mint one child MiniApp token through ``dwelling/external`` for a player."""
    payload = await transport(
        DWELLING_EXTERNAL,
        {**session.base_payload(player_id), "action": str(action)},
        mutation=False,
    )
    return extract_child_token(
        payload.get("url") if isinstance(payload, dict) else None,
        web_path=web_path,
        prefixes=prefixes,
    )


def external_apps(payload: Any) -> list[dict[str, Any]]:
    """Return sanitized external app entries (key/title/action/available only)."""
    account = payload.get("account") if isinstance(payload, dict) else None
    groups = ((account or {}).get("externalApps") or {}).get("groups") or []
    apps: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for app in group.get("apps") or []:
            if isinstance(app, dict):
                apps.append({
                    "group": safe_text(group.get("key") or group.get("title"), 40),
                    "key": safe_text(app.get("key"), 40),
                    "title": safe_text(app.get("title") or app.get("name"), 40),
                    "action": safe_text(app.get("action"), 40),
                    "available": app.get("available"),
                })
    return apps


def account_session_path(project_root: Path, account: str) -> Path:
    return Path(project_root) / DWELLING_SESSIONS[account]


def missing_sessions(project_root: Path, accounts: tuple[str, ...]) -> list[str]:
    return [account for account in accounts if not account_session_path(project_root, account).is_file()]


def make_bridge(project_root: Path, account: str) -> LiveAccountBridge:
    from tianji_trial.adapter import LiveAccountBridge
    return LiveAccountBridge(Path(project_root), account)


async def verify_username(bridge: Any, account: str) -> str:
    """The bridge already checks the user id; also pin the expected username."""
    me = await bridge.client.get_me()
    username = str(getattr(me, "username", "") or "").lstrip("@").lower()
    expected = ACCOUNT_USERNAMES[account]
    if username != expected:
        raise MiniAppError("account_identity_mismatch")
    return username
