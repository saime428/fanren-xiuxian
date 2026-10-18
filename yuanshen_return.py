"""Independent second-soul return listener and durable completion inbox.

The observer owns only this inbox. The soul runner remains the sole writer of
training/cooldown state; a launchd kick is never a business acknowledgement.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from command_confirmation import CANONICAL_CHAT_ID, CANONICAL_TOPIC_ID, OFFICIAL_REPLY_SENDER_IDS, official_message
from flow_registry import kickstart_argv
from miniapp_shared.state import exclusive_lock, read_json, write_json
from reply_parsing import get_message_top_id
from yuanshen_automation import (ACCOUNT, ACTOR_ID, USERNAME, STATE_RELATIVE, TRAIN_COMMAND,
                                STATUS_COMMAND, parse_reply, retry_deadline, training_due)

INBOX_RELATIVE = STATE_RELATIVE.with_name("return_events.json")
TRAIN_LABEL = f"ai.hermes.xiuxian.{ACCOUNT}.yuanshen-xiulian"
OBSERVER_LABEL = f"ai.hermes.xiuxian.{ACCOUNT}.yuanshen.return_observer"
SESSION_RELATIVE = STATE_RELATIVE.with_name("return_listener.session")
PROCESS_LOCK_RELATIVE = STATE_RELATIVE.with_name("return_listener.lock")
RECONCILE_SECONDS = 60
SCAN_LIMIT = 200
MAX_EVENT_AGE = 6 * 3600
LISTENER_STALE_SECONDS = 300


def completion_kind(text: str) -> str | None:
    """Classify only official, account-addressed completion wording.

    Identity, topic, freshness and cycle ordering are checked by the callers.
    Injury recovery is a separate server push from normal training return.
    """
    patterns = {
        "returned": r"【第二元神归位】\s*道友\s+@([A-Za-z0-9_]+)\s+的第二元神已结束修炼[，,]\s*回归窍中温养[。.]\s*(.*)",
        "injury_recovered": r"道友\s+@([A-Za-z0-9_]+)[，,]\s*你的第二元神已从【受伤】状态中恢复[。.]\s*",
    }
    for kind, pattern in patterns.items():
        matched = re.fullmatch(pattern, text, re.DOTALL)
        if matched and matched[1].lower() == USERNAME:
            return kind
    return None


def valid_return(event: dict[str, Any], now: float) -> bool:
    try:
        text = event["text"]
        return bool(
            event["chat_id"] == CANONICAL_CHAT_ID and event["topic_id"] == CANONICAL_TOPIC_ID
            and event["sender_id"] in OFFICIAL_REPLY_SENDER_IDS
            and not event.get("forwarded", False)
            and int(event["id"]) > 0 and 0 <= now - float(event["at"]) <= MAX_EVENT_AGE
            and completion_kind(text) is not None
        )
    except (KeyError, TypeError, ValueError):
        return False


def return_event(message: Any, now: float) -> dict[str, Any] | None:
    if not official_message(message, chat_id=CANONICAL_CHAT_ID):
        return None
    date = getattr(message, "date", None)
    if date is None:
        return None
    event = {"id": getattr(message, "id", 0), "at": date.timestamp(),
             "chat_id": getattr(message, "chat_id", None), "topic_id": get_message_top_id(message),
             "sender_id": getattr(message, "sender_id", None), "text": getattr(message, "message", "")}
    return event if valid_return(event, now) else None


def valid_state(state: dict[str, Any]) -> bool:
    return (state.get("version") == 1 and state.get("account") == ACCOUNT
            and state.get("actor_id") == ACTOR_ID)


def current_return(state: dict[str, Any], inbox: dict[str, Any], now: float) -> dict[str, Any] | None:
    """An old/edited completion must never unlock a newer training attempt."""
    if not valid_state(state) or not valid_state(inbox):
        return None
    attempts = [row for row in state.get("attempts", []) if row.get("command") == TRAIN_COMMAND]
    if not attempts:
        return None
    latest = attempts[-1]
    floor_id = max(int(latest.get("sent_id", 0)), int(latest.get("watermark", 0)))
    # Telegram dates have second precision; same-second ordering uses IDs.
    floor_time = math.floor(max(float(latest.get("at", 0)), float(state.get("last_training_started_at", 0))))
    for row in state.get("attempts", []):
        reply = row.get("reply") or {}
        if (row.get("command") == STATUS_COMMAND and row.get("status") == "confirmed"
                and reply.get("sender_id") in OFFICIAL_REPLY_SENDER_IDS
                and int(reply.get("id", 0)) > int(row.get("sent_id", 0)) > 0
                and parse_reply(STATUS_COMMAND, reply.get("text", ""))["status"] in {"training", "injured", "heart_demon"}):
            # A newer panel may describe a manually started next cycle. Replayed
            # completion pushes cannot erase this authoritative busy state.
            floor_id = max(floor_id, int(reply["id"]))
    candidates = [row for row in inbox.get("events", []) if valid_return(row, now)
                  and row["id"] > floor_id and row["at"] >= floor_time]
    return max(candidates, key=lambda row: row["id"]) if candidates else None


def training_dispatch_due(root: Path, now: float) -> bool:
    """Read-only admission shared by push kicks and the 30-second dispatcher."""
    from yuanshen_heart_demon import current_resolution, registry
    root = Path(root)
    if not any(row.get("label") == TRAIN_LABEL and row.get("enabled") is True
               for row in registry(root).get("helpers", [])):
        return False
    state = read_json(root / STATE_RELATIVE)
    if not valid_state(state):
        return False
    try:
        # Explicit server/FloodWait deadlines survive a completion push.
        hard_due = max(float(state.get("train_not_before", 0)), float(state.get("transport_not_before", 0)))
        if not math.isfinite(hard_due) or hard_due > now:
            return False
        if current_resolution(root, state, now):
            return True
        if current_return(state, read_json(root / INBOX_RELATIVE), now):
            return True
        due = max(training_due(state, now), retry_deadline(state, "train"))
        return math.isfinite(due) and 0 < due <= now
    except (TypeError, ValueError, KeyError):
        return False


async def kick_training() -> bool:
    proc = await asyncio.create_subprocess_exec(
        *kickstart_argv(TRAIN_LABEL),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(proc.wait(), timeout=5) == 0
    except asyncio.TimeoutError:
        proc.kill()  # Only the launchctl client, never the sender.
        await proc.wait()
        return False


class ReturnObserver:
    def __init__(self, root: Path, *, clock: Any = time.time, kick: Any = kick_training):
        self.root, self.clock, self.kick = Path(root), clock, kick
        self.path = self.root / INBOX_RELATIVE
        self.last_kick_at = 0.0
        from yuanshen_heart_demon import HeartDemonResponder
        self.heart_demon = HeartDemonResponder(root, clock=clock)

    def update(self, *, events: list[dict[str, Any]] | None = None, **fields: Any) -> None:
        with exclusive_lock(self.path.with_suffix(".lock")):
            data = read_json(self.path)
            if data and not valid_state(data):
                raise ValueError("invalid_return_inbox")
            merged = {row["id"]: row for row in data.get("events", [])}
            for event in events or []:
                # Repeated push, edit, history and restart keep one receipt.
                merged.setdefault(event["id"], event)
            data.update(version=1, account=ACCOUNT, actor_id=ACTOR_ID, events=sorted(
                merged.values(), key=lambda row: row["id"])[-50:], updated_at=self.clock(), **fields)
            write_json(self.path, data)

    async def verify(self, client: Any) -> None:
        from yuanshen_heart_demon import POLICY_VERSION, policy_enabled
        me = await client.get_me()
        if int(me.id) != ACTOR_ID or str(me.username or "").lower() != USERNAME:
            raise ValueError("return_listener_identity_mismatch")
        self.heart_demon.client = client
        self.update(status="starting", listener_account=ACCOUNT, listener_verified_at=self.clock(), pid=os.getpid(),
                    listener_label=OBSERVER_LABEL, mode="standalone", listener_connected=True,
                    heart_demon_policy=POLICY_VERSION, heart_demon_enabled=policy_enabled(self.root))

    async def maybe_kick(self) -> None:
        now = self.clock()
        if now - self.last_kick_at < 10 or not training_dispatch_due(self.root, now):
            return
        self.last_kick_at = now
        ok = await self.kick()
        self.update(last_kick_at=now, last_kick_status="scheduled" if ok else "failed")

    async def on_message(self, message: Any) -> None:
        event = return_event(message, self.clock())
        if event is not None:
            self.update(events=[event], last_push_at=self.clock())
            await self.maybe_kick()
        await self.heart_demon.process([message])
        await self.maybe_kick()

    async def on_scan(self, messages: list[Any]) -> None:
        events = [event for message in messages if (event := return_event(message, self.clock())) is not None]
        self.update(events=events, status="healthy", last_scan_at=self.clock(), error_type=None)
        await self.heart_demon.process(messages, reconcile=True)
        await self.maybe_kick()

    def failed(self, exc: Exception) -> None:
        self.update(status="failed", error_type=type(exc).__name__)

    def close(self) -> None:
        self.update(status="closed", listener_connected=False)


async def make_listener_client(root: Path) -> Any:
    from scripts.ensure_session_authorized import main as ensure_authorized
    from transport import telegram_client

    # Pin both target and seed; never borrow another flow's live SQLite session.
    os.environ["TG_SESSION_NAME"] = str(root / SESSION_RELATIVE)
    os.environ["TG_SESSION_FALLBACK_NAME"] = str(root / f"state/accounts/{ACCOUNT}/session")
    await ensure_authorized()
    return telegram_client(str(root / SESSION_RELATIVE), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"],
                           auto_reconnect=True, connection_retries=3)


async def run_persistent(root: Path, *, client_factory: Any = make_listener_client,
                         observer_factory: Any = ReturnObserver, max_cycles: int | None = None,
                         sleep: Any = asyncio.sleep) -> None:
    from event_monitor_runtime import HistoryWindow
    from observe_return import run_observer_loop
    from telethon import events
    from transport import collect_recent_messages, disconnect_telegram_client, ensure_connected

    root = Path(root)
    # Lock before authorization or opening SQLite, not only around inbox writes.
    with exclusive_lock(root / PROCESS_LOCK_RELATIVE):
        observer = observer_factory(root)
        client = None
        disconnected = None
        verified = False
        history = HistoryWindow()
        try:
            client = await asyncio.wait_for(client_factory(root), timeout=120)
            await asyncio.wait_for(ensure_connected(client), timeout=30)
            await asyncio.wait_for(observer.verify(client), timeout=30)
            verified = True
            entity = await asyncio.wait_for(client.get_entity(CANONICAL_CHAT_ID), timeout=30)

            async def on_message(event: Any) -> None:
                try:
                    await observer.on_message(getattr(event, "message", event))
                except Exception as exc:
                    observer.failed(exc)

            client.add_event_handler(on_message, events.NewMessage(chats=entity))
            client.add_event_handler(on_message, events.MessageEdited(chats=entity))

            def on_disconnect(done: Any) -> None:
                if not done.cancelled():
                    error = done.exception()
                    observer.update(status="disconnected", listener_connected=False,
                                    error_type=type(error).__name__ if error else "ConnectionError")

            async def reconcile() -> int:
                nonlocal disconnected, verified
                try:
                    await asyncio.wait_for(ensure_connected(client), timeout=30)
                    if disconnected is None or disconnected.done():
                        # First pass, or the connection dropped and reconnected.
                        if disconnected is not None:
                            disconnected.remove_done_callback(on_disconnect)
                            verified = False
                        disconnected = client.disconnected
                        disconnected.add_done_callback(on_disconnect)
                    if not verified:
                        # One connection's identity cannot change: verify (GetUsers)
                        # only at start and after a disconnect or a failed pass.
                        await asyncio.wait_for(observer.verify(client), timeout=30)
                        verified = True
                        history.invalidate()  # pushes may have been missed meanwhile
                    # Pushes deliver new messages and edits; routine passes read only
                    # past the high-water mark, the full window when it is due.
                    history.begin()
                    min_id = history.min_id(CANONICAL_CHAT_ID)
                    messages = await asyncio.wait_for(
                        client.get_messages(entity, limit=SCAN_LIMIT, min_id=min_id) if min_id
                        else collect_recent_messages(client=client, chat_id=entity, scan_limit=SCAN_LIMIT),
                        timeout=90)
                    if not client.is_connected():
                        raise ConnectionError("return_listener_disconnected")
                    history.record(CANONICAL_CHAT_ID, messages)
                    await observer.on_scan(messages)
                    history.complete()
                except Exception as exc:
                    verified = False
                    observer.failed(exc)
                return 0

            print(json.dumps({"status": "listening", "label": OBSERVER_LABEL,
                              "account": ACCOUNT, "reconcile_seconds": RECONCILE_SECONDS}))
            await run_observer_loop(reconcile, poll_seconds=RECONCILE_SECONDS,
                                    max_cycles=max_cycles, sleep=sleep)
        except Exception as exc:
            observer.failed(exc)
            raise
        finally:
            if disconnected is not None:
                disconnected.remove_done_callback(on_disconnect)
            if client is not None:
                with suppress(Exception):
                    await disconnect_telegram_client(client, 15)
            observer.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Independent @{USERNAME} second-soul return listener")
    parser.add_argument("--listen", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    if args.check or not args.listen:
        print(json.dumps({"status": "offline_check", "label": OBSERVER_LABEL, "account": ACCOUNT,
                          "session_path": str(root / SESSION_RELATIVE), "inbox_path": str(root / INBOX_RELATIVE),
                          "reconcile_seconds": RECONCILE_SECONDS, "network_used": False}, ensure_ascii=False))
        return 0
    try:
        asyncio.run(run_persistent(root))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}))
        return 1
    return 0
