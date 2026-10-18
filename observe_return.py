#!/usr/bin/env python3
"""Unified return-summary observer for shendubiguan and yuanyingchuqiao.

Watches the target topic for a bot "return summary" event addressed to this
account and, on a fresh one, spawns the flow's send wrapper once. Flow-specific
behavior (default markers, whether to force the pending-age reset) is selected
by TG_RETURN_MARKER_TEXT / TG_OBSERVER_KICK_RESET_PENDING, so one file serves
both flows.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any
from collections.abc import Awaitable, Callable

from dotenv import load_dotenv
from telethon import TelegramClient, events

from account_lifecycle import AccountPausedError, assert_account_allowed
from runtime import env, log_print
from event_monitor_runtime import EventMonitorHealth, HistoryWindow
from transport import disconnect_telegram_client
from reply_parsing import get_message_top_id, normalize_topic_id, parse_markers, slugify_message
from command_confirmation import OFFICIAL_REPLY_SENDER_IDS
from state_store import (
    FlowLockBusy,
    flow_lock,
    is_paused,
    load_json_dict,
    persist_json_atomic,
    write_lock_metadata,
)
from transport import collect_recent_messages, ensure_connected, force_reconnect, telegram_client, with_telegram_retry
from telegram_monitor_targets import monitor_targets


# The spawned sender is bounded by its own wrapper: a session-auth probe
# (TG_SESSION_AUTH_PROBE_TIMEOUT_SECONDS, 25 s) and then its TG_RUN_MAX_SECONDS
# (at most send_topic_message's 180 s default). Outwait both plus a margin:
# terminating a sender mid-command leaves an unconfirmed send behind.
SEND_WRAPPER_TIMEOUT_SECONDS = 25 + 180 + 60
# The observer's own session, scan window and run budget must not become the
# sender's: its wrappers only default these when they are unset.
OBSERVER_ONLY_ENV = ("TG_SESSION_NAME", "TG_SCAN_LIMIT", "TG_RUN_MAX_SECONDS")


def migrate_observer_legacy_cursor(
    cursors: dict[str, int],
    legacy_event_id: int,
    entity: object,
    source_target: str | int,
) -> dict[str, int]:
    migrated = dict(cursors)
    if legacy_event_id > 0 and not migrated:
        migrated[str(getattr(entity, "id", source_target))] = int(legacy_event_id)
    return migrated


async def run_observer_loop(
    run_cycle: Callable[[], Awaitable[int]],
    *,
    poll_seconds: float,
    cycle_timeout_seconds: float | None = None,
    max_cycles: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run observer scans in one process instead of launchd restart churn."""
    cycle = 0
    while max_cycles is None or cycle < max_cycles:
        cycle += 1
        try:
            if cycle_timeout_seconds is None:
                await run_cycle()
            else:
                await asyncio.wait_for(run_cycle(), timeout=cycle_timeout_seconds)
        except asyncio.TimeoutError:
            log_print(f"Observer cycle timed out after {cycle_timeout_seconds:g}s; retrying in {poll_seconds:g}s")
        except SystemExit as exc:
            code = int(exc.code or 0) if isinstance(exc.code, (int, type(None))) else 1
            log_print(f"Observer cycle exited code={code}; retrying in {poll_seconds:g}s")
        except Exception as exc:
            log_print(f"Observer cycle failed: {type(exc).__name__}; retrying in {poll_seconds:g}s")
        if max_cycles is not None and cycle >= max_cycles:
            break
        await sleep(poll_seconds)


async def _wait_for_observer_signal(signal: asyncio.Event, timeout: float) -> bool:
    try:
        await asyncio.wait_for(signal.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def run_observer_event_loop(
    reconcile: Callable[[], Awaitable[int]],
    signal: asyncio.Event,
    *,
    reconcile_seconds: float,
    max_reconciliations: int | None = None,
    wait_for_event: Callable[[float], Awaitable[bool]] | None = None,
) -> None:
    """Reconcile at startup and then on Telegram push or a slow recovery timer."""
    wait = wait_for_event or (lambda timeout: _wait_for_observer_signal(signal, timeout))
    reconciliations = 0
    while max_reconciliations is None or reconciliations < max_reconciliations:
        signal.clear()
        try:
            await reconcile()
        except asyncio.CancelledError:
            raise
        except SystemExit as exc:
            code = int(exc.code or 0) if isinstance(exc.code, (int, type(None))) else 1
            log_print(f"Observer reconciliation exited code={code}; continuing")
        except Exception as exc:
            log_print(f"Observer reconciliation failed: {type(exc).__name__}; continuing")
        reconciliations += 1
        if max_reconciliations is not None and reconciliations >= max_reconciliations:
            break
        await wait(reconcile_seconds)


def event_matches(
    message: object,
    *,
    markers: list[str],
    normalized_topic_id: int | None,
    target_username: str,
    trusted_sender_ids: set[int],
    last_seen_event_id: int,
    allow_root_topic_events: bool = False,
    root_last_seen_event_id: int = 0,
    owned_status_probe_ids: set[int] | None = None,
) -> dict[str, Any] | None:
    message_text = (getattr(message, "message", None) or "").strip()
    if not message_text:
        return None
    top_msg_id = normalize_topic_id(get_message_top_id(message))
    root_topic_event_allowed = allow_root_topic_events and top_msg_id is None
    if normalized_topic_id is not None and top_msg_id != normalized_topic_id and not root_topic_event_allowed:
        return None
    status_probe_reply = any(marker in message_text for marker in (
        "你并未处于深度闭关之中",
        "你並未處於深度閉關之中",
    ))
    if status_probe_reply:
        reply_to_id = getattr(message, "reply_to_msg_id", None)
        if reply_to_id is None:
            reply = getattr(message, "reply_to", None)
            reply_to_id = getattr(reply, "reply_to_msg_id", None)
        owned = {int(item) for item in (owned_status_probe_ids or set())}
        if not owned or int(reply_to_id or 0) not in owned:
            return None
    elif not any(marker in message_text for marker in markers):
        return None
    sender_id = getattr(message, "sender_id", None)
    if not trusted_sender_ids or sender_id not in trusted_sender_ids:
        return None
    # Telegram usernames are case-insensitive; compare both sides lowered.
    if target_username and not status_probe_reply:
        username_pattern = re.compile(
            rf"(?<![A-Za-z0-9_])@{re.escape(target_username)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        if not username_pattern.search(message_text):
            return None
    message_id = getattr(message, "id", None)
    seen_floor = root_last_seen_event_id if root_topic_event_allowed else last_seen_event_id
    if message_id is None or int(message_id) <= seen_floor:
        return None
    return {
        "id": int(message_id),
        "text": message_text,
        "ts": getattr(getattr(message, "date", None), "timestamp", lambda: time.time())(),
        "root_topic_event": root_topic_event_allowed,
    }


async def main_async(
    *,
    on_scan_complete: Callable[[], None] | None = None,
    client_override: TelegramClient | None = None,
    history: HistoryWindow | None = None,
    entities: dict[str | int, Any] | None = None,
) -> int:
    """One reconciliation pass.

    The persistent observer passes its resolved ``entities`` and its
    ``history`` window, so routine passes neither re-resolve the sources nor
    re-read the whole TG_SCAN_LIMIT window (see HistoryWindow).
    """
    load_dotenv()

    api_id_raw = env("TG_API_ID", required=True)
    api_hash = env("TG_API_HASH", required=True)
    session_name = env("TG_SESSION_NAME", required=True)
    target_raw = env("TG_TARGET", required=True)
    topic_id_raw = env("TG_TOPIC_ID", required=True)
    flow_slug = env("TG_FLOW_SLUG", slugify_message(env("TG_MESSAGE", ".深度闭关")))
    state_dir = Path(env("TG_STATE_DIR", "state"))
    scan_limit = max(20, int(env("TG_SCAN_LIMIT", "200")))
    trigger_max_event_age_seconds = max(60, int(env("TG_TRIGGER_MAX_EVENT_AGE_SECONDS", "21600")))
    run_max_seconds = max(30, int(env("TG_RUN_MAX_SECONDS", "120")))
    no_event_log_interval_seconds = max(0, int(env("TG_NO_EVENT_LOG_INTERVAL_SECONDS", "600")))
    send_wrapper = env("TG_SEQUENCE_SEND_WRAPPER", required=True)
    observer_state_path = Path(env("TG_OBSERVER_STATE_PATH", str(state_dir / "observer_last_seen.json")))
    observer_lock_path = Path(env("TG_OBSERVER_LOCK_PATH", str(state_dir / "observer.lock")))
    sequence_lock_path = Path(env("TG_SEQUENCE_LOCK_PATH", str(state_dir / "send.lock")))
    marker_text = env("TG_RETURN_MARKER_TEXT", "深度闭关总结|深度閉關總結")
    markers = parse_markers(marker_text)
    allow_root_topic_events = env("TG_RETURN_ALLOW_ROOT_TOPIC_EVENTS", "0") == "1"
    target_username = env("TG_TRIGGER_TARGET_USERNAME", "").strip().lstrip("@")
    trusted_sender_ids = {
        int(item.strip())
        for item in env("TG_RETURN_TRUSTED_SENDER_IDS", "").split(",")
        if item.strip().isdigit()
    }
    # Wrapper lists drifted (9 or 10 of the 12 official bots); the canonical set
    # is the floor, the env can only add verified senders.
    trusted_sender_ids |= set(OFFICIAL_REPLY_SENDER_IDS)
    if not trusted_sender_ids:
        raise SystemExit("TG_RETURN_TRUSTED_SENDER_IDS must contain at least one verified bot sender ID")
    # yuanying additionally forces the send to ignore any stale pending-sent age.
    kick_reset_pending = env("TG_OBSERVER_KICK_RESET_PENDING", "0") == "1"
    # The observer watchdog looks for pause_<observer_slug>; honor the same slug
    # here so pausing the flow actually stops the observer.
    observer_pause_slug = env("TG_OBSERVER_FLOW_SLUG", f"{flow_slug}_observer")

    paused, pause_path = is_paused(state_dir, observer_pause_slug)
    if not paused:
        paused, pause_path = is_paused(state_dir, flow_slug)
    if paused:
        log_print(f"Paused via {pause_path}; observer skip")
        return 0

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise SystemExit("TG_API_ID must be an integer") from exc
    try:
        topic_id = int(topic_id_raw)
    except ValueError as exc:
        raise SystemExit("TG_TOPIC_ID must be an integer") from exc

    target: str | int
    try:
        target = int(target_raw)
    except ValueError:
        target = target_raw
    source_targets = monitor_targets(env("TG_MONITOR_TARGETS", ""), target)
    root_monitor_targets = {
        str(item) for item in monitor_targets(env("TG_MONITOR_ROOT_TARGETS", ""), "") if str(item).strip()
    }

    # A real flock prevents a manual run and the scheduled run (or a watchdog
    # kickstart) from both spawning a send for the same event.
    try:
        lock_ctx = flow_lock(observer_lock_path)
        lock_ctx.__enter__()
    except FlowLockBusy:
        log_print(f"Another observer run is active; skip this run. lock={observer_lock_path}")
        return 0

    try:
        write_lock_metadata(
            observer_lock_path,
            {
                "pid": os.getpid(),
                "flow_slug": flow_slug,
                "started_at_ts": time.time(),
                "run_max_seconds": run_max_seconds,
                "markers": markers,
                "trigger_target_username": target_username,
                "send_wrapper": send_wrapper,
            },
        )

        client = client_override or telegram_client(Path(session_name), api_id, api_hash)
        owns_client = client_override is None
        state = load_json_dict(observer_state_path)
        last_seen_by_chat = state.get("last_seen_event_ids_by_chat") or {}
        root_seen_by_chat = state.get("root_last_seen_event_ids_by_chat") or {}
        # Backward compatibility: seed the primary group from legacy scalar cursors.
        if not last_seen_by_chat and state.get("last_seen_event_id"):
            last_seen_by_chat[str(target)] = int(state.get("last_seen_event_id") or 0)
        if not root_seen_by_chat and state.get("root_last_seen_event_id"):
            root_seen_by_chat[str(target)] = int(state.get("root_last_seen_event_id") or 0)
        owned_status_probe_ids = {
            int(item)
            for item in state.get("owned_status_probe_ids", [])
            if str(item).isdigit()
        }
        status_probe_state = load_json_dict(state_dir / "status_probe.json")
        status_probe_message_id = status_probe_state.get("sent_message_id")
        if str(status_probe_message_id or "").isdigit():
            owned_status_probe_ids.add(int(status_probe_message_id))

        async def scan_recent_messages() -> list[tuple[str | int, Any, object]]:
            await ensure_connected(client)
            combined: list[tuple[str | int, Any, object]] = []
            if history is not None:
                history.begin()
            for source_target in source_targets:
                entity = (entities or {}).get(source_target)
                if entity is None:
                    entity = await with_telegram_retry(
                        lambda source_target=source_target: client.get_entity(source_target),
                        label=f"get target entity {source_target}",
                        reconnect=lambda: force_reconnect(client),
                    )
                nonlocal last_seen_by_chat, root_seen_by_chat
                last_seen_by_chat = migrate_observer_legacy_cursor(
                    last_seen_by_chat,
                    int(state.get("last_seen_event_id") or 0),
                    entity,
                    source_target,
                )
                root_seen_by_chat = migrate_observer_legacy_cursor(
                    root_seen_by_chat,
                    int(state.get("root_last_seen_event_id") or 0),
                    entity,
                    source_target,
                )
                chat_key = str(getattr(entity, "id", source_target))
                min_id = history.min_id(chat_key) if history is not None else 0
                if min_id:
                    messages = await with_telegram_retry(
                        lambda entity=entity, min_id=min_id: client.get_messages(
                            entity, limit=scan_limit, min_id=min_id),
                        label=f"incremental return observer scan {source_target}",
                        reconnect=lambda: ensure_connected(client),
                    )
                else:
                    messages = await collect_recent_messages(client=client, chat_id=entity, scan_limit=scan_limit)
                if history is not None:
                    history.record(chat_key, messages)
                combined.extend((source_target, entity, message) for message in messages)
            return combined

        # Bound the whole scan so a hung Telethon call cannot wedge the
        # observer past its runtime budget.  Do not use ``async with client``
        # here: Telethon's implicit start() path bypasses our bounded
        # reconnect policy and was leaving this persistent observer stuck on
        # IncompleteReadError until the scheduled 5-minute sender fired.
        try:
            await with_telegram_retry(
                lambda: ensure_connected(client),
                label="connect return observer scan",
                attempts=max(2, int(env("TG_OBSERVER_CONNECT_ATTEMPTS", "3"))),
                base_sleep=max(1.0, float(env("TG_OBSERVER_CONNECT_BASE_SLEEP_SECONDS", "2"))),
                reconnect=lambda: force_reconnect(client),
            )
            recent = await asyncio.wait_for(scan_recent_messages(), timeout=run_max_seconds)
            if history is not None:
                history.complete()
            if on_scan_complete is not None:
                on_scan_complete()
        except TimeoutError as exc:
            raise SystemExit(f"observer scan timed out after {run_max_seconds}s") from exc
        finally:
            if owns_client:
                with suppress(Exception):
                    await disconnect_telegram_client(client)

        normalized_topic_id = normalize_topic_id(topic_id)
        newest_event: dict[str, Any] | None = None
        for source_target, source_entity, message in recent:
            chat_key = str(getattr(source_entity, "id", source_target))
            source_is_root = str(source_target) in root_monitor_targets or chat_key in root_monitor_targets
            candidate = event_matches(
                message,
                markers=markers,
                normalized_topic_id=None if source_is_root else normalized_topic_id,
                target_username=target_username,
                trusted_sender_ids=trusted_sender_ids,
                last_seen_event_id=int(last_seen_by_chat.get(chat_key) or 0),
                allow_root_topic_events=allow_root_topic_events or source_is_root,
                root_last_seen_event_id=int(root_seen_by_chat.get(chat_key) or 0),
                owned_status_probe_ids=owned_status_probe_ids,
            )
            if candidate is None:
                continue
            candidate["source_chat"] = str(source_target)
            candidate["source_chat_key"] = chat_key
            if newest_event is None or float(candidate["ts"]) > float(newest_event["ts"]):
                newest_event = candidate

        def heartbeat(status: str) -> None:
            # The observer watchdog judges liveness by this file's ts; refresh
            # it on every completed run, not only when an event spawns a send.
            state.update({"ts": time.time(), "status": status})
            persist_json_atomic(observer_state_path, state)

        if newest_event is None:
            target_note = f", target=@{target_username}" if target_username else ""
            now_ts = time.time()
            last_no_event_log_ts = float(state.get("last_no_event_log_ts") or 0)
            should_log_no_event = (
                no_event_log_interval_seconds <= 0
                or last_no_event_log_ts <= 0
                or now_ts - last_no_event_log_ts >= no_event_log_interval_seconds
            )
            if should_log_no_event:
                log_print(f"No new return-summary event found for markers={markers}{target_note}")
                state["last_no_event_log_ts"] = now_ts
            heartbeat("no_new_event")
            return 0

        event_age = time.time() - float(newest_event["ts"])
        if event_age > trigger_max_event_age_seconds:
            log_print(f"Ignoring stale return-summary event_message_id={newest_event['id']} age={int(event_age)}s")
            heartbeat("stale_event_ignored")
            return 0

        account = env("XIU_ACCOUNT_SLUG", "")
        if account:
            try:
                assert_account_allowed(account)
            except AccountPausedError as exc:
                # A definite local refusal, not a failed kick: the sender would
                # only be refused, so do not spawn it and keep the event unseen.
                log_print(f"Account paused ({exc}); event_message_id={newest_event['id']} stays pending")
                heartbeat("account_paused")
                return 0

        # Spawn the send wrapper FIRST; only record the event as seen after it
        # exits cleanly, so a failed/timed-out kick is retried next run instead
        # of being silently marked done.
        env_copy = {key: value for key, value in os.environ.items() if key not in OBSERVER_ONLY_ENV}
        env_copy["TG_SEQUENCE_LOCK_PATH"] = str(sequence_lock_path)
        env_copy["TG_MIN_REPEAT_SECONDS"] = "0"
        env_copy["TG_OBSERVER_IMMEDIATE_KICK"] = "1"
        if kick_reset_pending:
            env_copy["TG_PENDING_SENT_MAX_AGE_SECONDS"] = "0"

        state.update(
            {
                "pending_event_message_id": int(newest_event["id"]),
                "pending_event_text": newest_event["text"],
                "pending_event_ts": float(newest_event["ts"]),
                "pending_send_reason": "observer_immediate_kick",
                "pending_send_bypass_cache": True,
                "source_chat": str(newest_event["source_chat"]),
            }
        )
        persist_json_atomic(observer_state_path, state)

        proc = subprocess.Popen([send_wrapper], env=env_copy)
        try:
            # Keep push callbacks responsive while the original sender waits
            # for its own official reply.
            rc = await asyncio.to_thread(proc.wait, timeout=SEND_WRAPPER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.terminate()
            raise SystemExit(f"send wrapper timed out after {SEND_WRAPPER_TIMEOUT_SECONDS}s")
        if rc != 0:
            raise SystemExit(f"send wrapper failed with exit code {rc}")

        completed_state = {
            "ts": time.time(),
            "status": "spawned_send",
            "event_message_id": int(newest_event["id"]),
            "event_text": newest_event["text"],
            "spawned_pid": proc.pid,
            "send_wrapper": send_wrapper,
            "last_send_reason": "observer_immediate_kick",
            "last_send_bypass_cache": True,
            "source_chat": str(newest_event["source_chat"]),
        }
        if newest_event.get("root_topic_event"):
            root_seen_by_chat[newest_event["source_chat_key"]] = int(newest_event["id"])
            completed_state["root_last_seen_event_ids_by_chat"] = root_seen_by_chat
        else:
            last_seen_by_chat[newest_event["source_chat_key"]] = int(newest_event["id"])
            completed_state["last_seen_event_ids_by_chat"] = last_seen_by_chat
        state.update(completed_state)
        persist_json_atomic(observer_state_path, state)
        log_print(f"Spawned {flow_slug} send pid={proc.pid} for return-summary event_message_id={newest_event['id']}")
        return 0
    finally:
        lock_ctx.__exit__(None, None, None)


def _observer_session_sqlite_path(session_name: str | Path) -> Path:
    path = Path(session_name)
    return path if path.suffix == ".session" else Path(f"{path}.session")


async def run_persistent_observer() -> None:
    """Keep one update client alive and use bounded one-shot scans for recovery."""
    load_dotenv()
    state_dir = Path(env("TG_STATE_DIR", "state"))
    # Take the process lease before the health file, the session copy or
    # Telegram: a second instance is a normal skip and must not overwrite
    # the running observer's health. (observer.lock is the per-pass lock.)
    with ExitStack() as lease:
        try:
            lease.enter_context(flow_lock(state_dir / "observer_monitor.lock"))
        except FlowLockBusy:
            log_print(f"Another persistent return observer is running; skip this start. state={state_dir}")
            return
        await _run_persistent_observer_locked(state_dir)


async def _run_persistent_observer_locked(state_dir: Path) -> None:
    api_id = int(env("TG_API_ID", required=True))
    api_hash = env("TG_API_HASH", required=True)
    session_name = env("TG_SESSION_NAME", required=True)
    listener_name = env("TG_OBSERVER_PUSH_SESSION_NAME", f"{session_name}_push")
    target_raw = env("TG_TARGET", required=True)
    try:
        target: str | int = int(target_raw)
    except ValueError:
        target = target_raw
    source_targets = monitor_targets(env("TG_MONITOR_TARGETS", ""), target)
    trusted_sender_ids = {
        int(item.strip())
        for item in env("TG_RETURN_TRUSTED_SENDER_IDS", "").split(",")
        if item.strip().isdigit()
    }
    # Wrapper lists drifted (9 or 10 of the 12 official bots); the canonical set
    # is the floor, the env can only add verified senders.
    trusted_sender_ids |= set(OFFICIAL_REPLY_SENDER_IDS)
    if not trusted_sender_ids:
        raise SystemExit("TG_RETURN_TRUSTED_SENDER_IDS must contain at least one verified bot sender ID")
    reconcile_seconds = max(30.0, float(env("TG_OBSERVER_RECONCILE_SECONDS", "60")))
    flow_slug = env("TG_FLOW_SLUG", required=True)
    observer_state_path = Path(env("TG_OBSERVER_STATE_PATH", str(state_dir / "observer_last_seen.json")))
    markers = parse_markers(env("TG_RETURN_MARKER_TEXT", "深度闭关总结|深度閉關總結"))
    target_username = env("TG_TRIGGER_TARGET_USERNAME", "").strip().lstrip("@")
    topic_id = normalize_topic_id(int(env("TG_TOPIC_ID", "7310786")))
    allow_root_events = env("TG_RETURN_ALLOW_ROOT_TOPIC_EVENTS", "0") == "1"
    max_event_age = max(60, int(env("TG_TRIGGER_MAX_EVENT_AGE_SECONDS", "21600")))
    root_targets = {str(item) for item in monitor_targets(env("TG_MONITOR_ROOT_TARGETS", ""), "")}

    source_session = _observer_session_sqlite_path(session_name)
    listener_session = _observer_session_sqlite_path(listener_name)
    listener_session.parent.mkdir(parents=True, exist_ok=True)
    if not source_session.exists():
        raise SystemExit(f"Authorized observer session missing: {source_session}")
    if not listener_session.exists():
        shutil.copy2(source_session, listener_session)

    client = telegram_client(
        listener_name,
        api_id,
        api_hash,
        auto_reconnect=True,
        connection_retries=3,
    )
    signal = asyncio.Event()
    health = EventMonitorHealth(
        state_dir, env("XIU_ACCOUNT_SLUG", required=True),
        flow_slug, "return_observer", source_targets, reconcile_seconds,
    )
    try:
        await health.verify_listener(client)
        entities = []
        for source_target in source_targets:
            entity = await with_telegram_retry(
                lambda source_target=source_target: client.get_entity(source_target),
                label=f"resolve return observer push target {source_target}",
                reconnect=lambda: ensure_connected(client),
            )
            entities.append(entity)
        root_chat_ids = {str(entity.id) for source, entity in zip(source_targets, entities)
                         if str(source) in root_targets}

        async def on_message(event: object) -> None:
            message = getattr(event, "message", event)
            if int(getattr(message, "sender_id", 0) or 0) not in trusted_sender_ids:
                return
            text = getattr(message, "message", "") or ""
            if not any(marker in text for marker in [*markers, "你并未处于深度闭关之中", "你並未處於深度閉關之中"]):
                return
            chat_id = int(getattr(event, "chat_id", 0) or 0)
            if chat_id < -1_000_000_000_000:
                chat_id = -chat_id - 1_000_000_000_000
            chat_key = str(chat_id)
            state = load_json_dict(observer_state_path)
            owned = {int(value) for value in state.get("owned_status_probe_ids", [])
                     if str(value).isdigit()}
            probe_id = load_json_dict(state_dir / "status_probe.json").get("sent_message_id")
            if str(probe_id or "").isdigit():
                owned.add(int(probe_id))
            candidate = event_matches(
                message, markers=markers,
                normalized_topic_id=None if chat_key in root_chat_ids else topic_id,
                target_username=target_username, trusted_sender_ids=trusted_sender_ids,
                last_seen_event_id=int((state.get("last_seen_event_ids_by_chat") or {}).get(chat_key, 0)),
                allow_root_topic_events=allow_root_events or chat_key in root_chat_ids,
                root_last_seen_event_id=int((state.get("root_last_seen_event_ids_by_chat") or {}).get(chat_key, 0)),
                owned_status_probe_ids=owned,
            )
            if candidate is not None and 0 <= time.time() - candidate["ts"] <= max_event_age:
                # The pushed message may be an edit below the incremental floor.
                health.history.invalidate()
                signal.set()

        client.add_event_handler(on_message, events.NewMessage(chats=entities))
        client.add_event_handler(on_message, events.MessageEdited(chats=entities))
        log_print(
            f"Return observer push listener active targets={source_targets}; "
            f"history reconciliation every {reconcile_seconds:g}s"
        )
        await run_observer_event_loop(
            lambda: health.run_cycle(
                client,
                lambda **kwargs: main_async(client_override=client, history=health.history,
                                            entities=dict(zip(source_targets, entities)), **kwargs),
            ),
            signal,
            reconcile_seconds=reconcile_seconds,
        )
    except (Exception, SystemExit) as exc:
        health.failed("push_listener_startup", exc)
        raise
    finally:
        health.close()
        with suppress(Exception):
            await disconnect_telegram_client(client, 15)


def main() -> int:
    load_dotenv()
    persistent = env("TG_OBSERVER_PERSISTENT", "0") == "1"
    if not persistent:
        return asyncio.run(main_async())
    asyncio.run(run_persistent_observer())
    return 0


if __name__ == "__main__":
    sys.exit(main())
