#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import fcntl
import os
import random
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from account_lifecycle_telegram import LifecycleTelegramClient as TelegramClient
from telethon.errors import FloodError, ServerError, TimedOutError
from telethon.errors.common import InvalidBufferError
from telethon.network.connection import ConnectionTcpMTProxyIntermediate

from runtime import env, log_print
from message_fact_store import MessageFactStore
from telegram_send_gate import run_with_global_send_gate

T = TypeVar("T")


def _mtproxy_connection_options() -> dict[str, object]:
    enabled = env("TG_MTPROTO_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return {}

    host = env("TG_MTPROTO_HOST", "127.0.0.1").strip()
    if not host:
        raise ValueError("TG_MTPROTO_HOST must not be empty when MTProto is enabled")
    try:
        port = int(env("TG_MTPROTO_PORT", "5753"))
    except ValueError as exc:
        raise ValueError("TG_MTPROTO_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("TG_MTPROTO_PORT must be between 1 and 65535")

    secret = env("TG_MTPROTO_SECRET").strip().lower()
    if len(secret) != 32 or any(char not in "0123456789abcdef" for char in secret):
        raise ValueError("TG_MTPROTO_SECRET must be exactly 32 hexadecimal characters")

    return {
        "connection": ConnectionTcpMTProxyIntermediate,
        "proxy": (host, port, secret),
    }


def telegram_client(
    session_name: str | Path,
    api_id: int,
    api_hash: str,
    **telethon_options: object,
) -> TelegramClient:
    # Keep Telethon's internal retry loops disabled. Otherwise every worker's
    # retry loop can multiply with our bounded outer retry policy and create a
    # reconnect/request storm that triggers HTTP 429.
    request_retries = max(0, int(env("TG_TELEGRAM_REQUEST_RETRIES", "0")))
    connection_retries = max(0, int(env("TG_TELEGRAM_CONNECTION_RETRIES", "0")))
    retry_delay_seconds = max(0.0, float(env("TG_TELEGRAM_RETRY_DELAY_SECONDS", "2")))
    auto_reconnect = env("TG_TELEGRAM_AUTO_RECONNECT", "0").strip().lower() in {"1", "true", "yes", "on"}
    options: dict[str, object] = {
        "request_retries": request_retries,
        "connection_retries": connection_retries,
        "retry_delay": retry_delay_seconds,
        "auto_reconnect": auto_reconnect,
        # With request_retries=0, Telethon's default short FloodWait sleep ends
        # in a generic ValueError. Let our outer policy see the actual wait and
        # preserve server errors instead of reconnecting on a hidden throttle.
        "flood_sleep_threshold": 0,
        "raise_last_call_error": True,
    }
    options.update(telethon_options)
    # When explicitly enabled, the local Surge listener is mandatory.  Apply
    # these options last so a caller cannot accidentally bypass it.
    options.update(_mtproxy_connection_options())
    return TelegramClient(
        str(session_name),
        api_id,
        api_hash,
        **options,
    )


def _connection_gate_path() -> Path:
    return Path(env("TG_TELEGRAM_CONNECTION_GATE_PATH", "state/telegram_connection_gate.lock"))


async def _connect_with_gate(client: TelegramClient, timeout_seconds: float) -> None:
    """Bound concurrent MTProto handshakes without serializing the whole fleet."""
    path = _connection_gate_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    slots = max(1, min(8, int(env("TG_TELEGRAM_MAX_CONCURRENT_CONNECTS", "4"))))
    slot_paths = [path] + [path.with_name(f"{path.name}.{index}") for index in range(1, slots)]
    offset = os.getpid() % slots
    slot_paths = slot_paths[offset:] + slot_paths[:offset]
    handle = None
    try:
        # A cancelled to_thread(flock) can acquire the lock later and strand it
        # forever. Keep acquisition cancellable and inside the connect budget.
        async with asyncio.timeout(timeout_seconds):
            while handle is None:
                for slot_path in slot_paths:
                    candidate = slot_path.open("a+")
                    try:
                        fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        candidate.close()
                    except BaseException:
                        candidate.close()
                        raise
                    else:
                        handle = candidate
                        break
                if handle is None:
                    await asyncio.sleep(0.05)
            await client.connect()
    finally:
        if handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


async def disconnect_telegram_client(client: TelegramClient, timeout_seconds: float = 10) -> None:
    """Close a bounded client and consume its separately reported disconnect error."""
    disconnected = client.disconnected
    try:
        await asyncio.wait_for(client.disconnect(), timeout=timeout_seconds)
    finally:
        if disconnected.done() and not disconnected.cancelled():
            disconnected.exception()
        else:
            disconnected.cancel()


async def ensure_connected(client: TelegramClient) -> None:
    if not client.is_connected():
        connect_timeout_seconds = max(5.0, float(env("TG_TELEGRAM_CONNECT_TIMEOUT_SECONDS", "20")))
        await _connect_with_gate(client, connect_timeout_seconds)


async def force_reconnect(client: TelegramClient) -> None:
    connect_timeout_seconds = max(5.0, float(env("TG_TELEGRAM_CONNECT_TIMEOUT_SECONDS", "20")))
    try:
        await disconnect_telegram_client(client, connect_timeout_seconds)
    except Exception:
        pass
    await _connect_with_gate(client, connect_timeout_seconds)


async def with_telegram_retry(
    action: Callable[[], Awaitable[T]],
    *,
    label: str,
    attempts: int | None = None,
    base_sleep: float | None = None,
    reconnect: Callable[[], Awaitable[None]] | None = None,
) -> T:
    last_exc: Exception | None = None
    attempts = max(1, int(attempts if attempts is not None else env("TG_TELEGRAM_RETRY_ATTEMPTS", "3")))
    base_sleep = max(1.0, float(base_sleep if base_sleep is not None else env("TG_TELEGRAM_RETRY_BASE_SLEEP_SECONDS", "8")))
    op_timeout_seconds = max(5.0, float(env("TG_TELEGRAM_OP_TIMEOUT_SECONDS", "30")))
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(action(), timeout=op_timeout_seconds)
        except FloodError as exc:
            last_exc = exc
            wait_seconds = max(base_sleep, float(getattr(exc, "seconds", 0) or 0) + 1.0)
            log_print(f"Telegram flood wait during {label}; sleeping {wait_seconds:.1f}s (attempt {attempt}/{attempts})")
            if attempt >= attempts:
                raise
            await asyncio.sleep(wait_seconds)
        except (ServerError, TimedOutError) as exc:
            last_exc = exc
            wait_seconds = base_sleep * attempt
            log_print(f"Telegram server error during {label}: {type(exc).__name__}; "
                      f"backing off {wait_seconds:.1f}s (attempt {attempt}/{attempts})")
            if attempt >= attempts:
                raise
            await asyncio.sleep(wait_seconds)
        except InvalidBufferError as exc:
            last_exc = exc
            err_text = str(exc)
            if "429" not in err_text:
                raise
            cooldown = max(1.0, float(env("TG_TELEGRAM_429_COOLDOWN_SECONDS", "30")))
            wait_seconds = max(cooldown, base_sleep * (2 ** (attempt - 1))) + random.uniform(0.0, 1.0)
            log_print(f"Telegram HTTP 429 during {label}; backing off {wait_seconds:.1f}s without reconnect (attempt {attempt}/{attempts})")
            if attempt >= attempts:
                raise
            await asyncio.sleep(wait_seconds)
        except ValueError as exc:
            # Telethon surfaces a dropped/failed MTProto request as
            # `Request was unsuccessful N time(s)` rather than a ConnectionError.
            # Treat only that exact family as a reconnectable transport fault;
            # unrelated ValueErrors must still fail fast.
            if not str(exc).startswith("Request was unsuccessful "):
                raise
            last_exc = exc
            wait_seconds = base_sleep * attempt + random.uniform(0.0, 1.0)
            log_print(
                f"Telegram unsuccessful request during {label}: {exc}; "
                f"reconnecting and sleeping {wait_seconds:.1f}s (attempt {attempt}/{attempts})"
            )
            if attempt >= attempts:
                raise
            if reconnect is not None:
                try:
                    await asyncio.wait_for(reconnect(), timeout=op_timeout_seconds)
                except Exception as reconnect_exc:
                    log_print(f"Reconnect attempt during {label} failed: {reconnect_exc}")
            await asyncio.sleep(wait_seconds)
        except (ConnectionError, asyncio.IncompleteReadError, OSError) as exc:
            # TimeoutError (asyncio.wait_for included) is an OSError subclass, so op timeouts land here and reconnect.
            last_exc = exc
            wait_seconds = base_sleep * attempt + random.uniform(0.0, 1.0)
            log_print(
                f"Telegram connection error during {label}: {type(exc).__name__}: {exc}; "
                f"reconnecting and sleeping {wait_seconds:.1f}s (attempt {attempt}/{attempts})"
            )
            if attempt >= attempts:
                raise
            if reconnect is not None:
                try:
                    await asyncio.wait_for(reconnect(), timeout=op_timeout_seconds)
                except Exception as reconnect_exc:
                    log_print(f"Reconnect attempt during {label} failed: {reconnect_exc}")
            await asyncio.sleep(wait_seconds)
    assert last_exc is not None
    raise last_exc


async def collect_recent_messages(
    client: TelegramClient,
    chat_id: int | str,
    scan_limit: int,
) -> list[object]:
    async def _collect() -> list[object]:
        items = []
        # Telegram throttles rapid GetHistory pages even when the monitor's
        # next cycle is a minute away. Keep the full recovery horizon, spacing
        # pages instead of repeatedly restarting the same burst after a wait.
        async for m in client.iter_messages(chat_id, limit=scan_limit, wait_time=1 if scan_limit > 100 else 0):
            items.append(m)
        return items

    return await with_telegram_retry(
        _collect,
        label="iter_messages",
        reconnect=lambda: ensure_connected(client),
    )


async def fetch_message_by_id(
    client: TelegramClient,
    chat_id: int | str,
    message_id: int,
) -> object | None:
    async def _fetch() -> object | None:
        return await client.get_messages(chat_id, ids=message_id)

    try:
        message = await with_telegram_retry(
            _fetch,
            label=f"get_messages id={message_id}",
            reconnect=lambda: ensure_connected(client),
        )
    except Exception as exc:
        log_print(f"fetch_message_by_id skipped for {message_id}: {exc}")
        return None
    return message if getattr(message, "id", None) is not None else None


async def collect_direct_replies_to_message(
    client: TelegramClient,
    chat_id: int | str,
    message_id: int,
    scan_limit: int,
) -> list[object]:
    async def _collect() -> list[object]:
        items = []
        async for m in client.iter_messages(chat_id, reply_to=message_id, limit=scan_limit):
            items.append(m)
        return items

    try:
        replies = await with_telegram_retry(
            _collect,
            label=f"iter_messages reply_to={message_id}",
            reconnect=lambda: ensure_connected(client),
        )
        if replies:
            return replies
    except Exception as exc:
        log_print(f"collect_direct_replies_to_message skipped for {message_id}: {exc}")
    # Telegram rejects getReplies for some forum messages. Read a bounded
    # chronological neighbourhood instead; callers still enforce exact parent,
    # official sender and account. This also finds edited, no-longer-recent replies.
    try:
        return list(await with_telegram_retry(
            lambda: client.get_messages(chat_id, min_id=message_id, reverse=True,
                                        limit=min(max(20, scan_limit), 300)),
            label=f"reply neighbourhood after={message_id}",
            reconnect=lambda: ensure_connected(client),
        ))
    except Exception as exc:
        log_print(f"reply neighbourhood unavailable for {message_id}: {type(exc).__name__}")
        return []


async def delete_own_messages(
    client: TelegramClient,
    chat_id: int | str,
    message_ids,
) -> list[int]:
    delete_ids = sorted({int(mid) for mid in message_ids if mid})
    if not delete_ids:
        return []
    await with_telegram_retry(
        lambda: client.delete_messages(chat_id, delete_ids),
        label="delete_own_messages",
        reconnect=lambda: ensure_connected(client),
    )
    return delete_ids


async def send_with_global_gate(
    action: Callable[[], Awaitable[T]],
    *,
    label: str,
    state_dir: Path,
    fact: dict[str, object] | None = None,
) -> T:
    min_spacing_seconds = max(1.0, float(env("TG_GLOBAL_MIN_SEND_SPACING_SECONDS", "1")))

    async def guarded_action() -> T:
        result = await action()
        if fact is not None:
            payload = dict(fact)
            sent_id = getattr(result, "id", None)
            if sent_id is not None and not payload.get("message_id"):
                payload["message_id"] = int(sent_id)
            MessageFactStore(
                Path(env("TG_MESSAGE_LOG_PATH", str(state_dir / "messages.jsonl")))
            ).append(payload)
        return result

    return await run_with_global_send_gate(
        guarded_action,
        label=label,
        min_interval_seconds=min_spacing_seconds,
        state_dir=state_dir,
        log=log_print,
    )
