#!/usr/bin/env python3
"""Hourly two-account 温养双修 coordinator.

The initiator (@demo_player_01) posts a fresh anchor message. The partner
(@demo_player_03) replies directly to that exact anchor with `.双修 温养`.
Only a direct bot business reply to the partner command advances cooldown.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from account_lifecycle import AccountPausedError, assert_account_allowed
from runtime import env, log_print
from command_confirmation import CANONICAL_CHAT_ID, CANONICAL_TARGET, CANONICAL_TOPIC_ID, select_reply_scan_ids
from reply_parsing import parse_cooldown_retry_delay_seconds
from shuangxiu_wenyang_reply import (
    INITIATOR_ACCOUNT, INITIATOR_USERNAME, PARTNER_ACCOUNT, PARTNER_USERNAME,
    TRUSTED_REPLY_SENDER_IDS, classify_partner_reply,
)
from state_store import FlowLockBusy, flow_lock, is_paused, load_json_dict, persist_json_atomic, write_lock_metadata
from transport import disconnect_telegram_client, ensure_connected, send_with_global_gate, telegram_client, with_telegram_retry


ANCHOR_TEXT = "1"
ACTION_TEXT = ".双修 温养"
DEFAULT_INTERVAL_SECONDS = 3600
FINAL_SETTLEMENT_WAIT_SECONDS = 180
UNRESOLVED_STATUSES = frozenset({"anchor_send_submitted", "anchor_send_unknown", "anchor_sent",
    "command_send_submitted", "command_send_unknown", "command_sent_waiting_reply",
    "sent_unconfirmed_assume_consumed", "reply_retry_pending", "sent_no_ack"})


def _reply_to_msg_id(message: Any) -> int | None:
    direct = getattr(message, "reply_to_msg_id", None)
    if direct:
        return int(direct)
    reply = getattr(message, "reply_to", None)
    nested = getattr(reply, "reply_to_msg_id", None)
    return int(nested) if nested else None


def select_direct_business_reply(
    replies: list[dict[str, Any]], *, command_message_id: int
) -> tuple[dict[str, Any] | None, str]:
    for item in sorted(replies, key=lambda row: float(row.get("ts") or 0), reverse=True):
        if int(item.get("reply_to_msg_id") or 0) != command_message_id:
            continue
        if item.get("sender_id") not in TRUSTED_REPLY_SENDER_IDS or item.get("forwarded"):
            continue
        status = classify_partner_reply(str(item.get("text") or ""))
        if status != "unknown":
            return item, status
    return None, "unknown"


def compute_next_due_ts(success_ts: float, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> float:
    return float(success_ts) + max(DEFAULT_INTERVAL_SECONDS, int(interval_seconds))


def reply_next_due_ts(reply: dict[str, Any], status: str, interval_seconds=DEFAULT_INTERVAL_SECONDS) -> float:
    if status == "cooldown":
        remaining = parse_cooldown_retry_delay_seconds(str(reply.get("text") or ""), ["冷却", "冷卻", "剩余", "请在"])
        if remaining is not None:
            return float(reply["ts"]) + remaining + 3
    return compute_next_due_ts(float(reply["ts"]), interval_seconds)


def next_run_due_ts(last_success: dict[str, Any], last_attempt: dict[str, Any], interval_seconds=DEFAULT_INTERVAL_SECONDS) -> float:
    # A newer, attributed server countdown supersedes an older nominal hour.
    text = str(last_attempt.get("reply_text") or "")
    if (last_attempt.get("status") == "cooldown"
            and last_attempt.get("reply_sender_id") in TRUSTED_REPLY_SENDER_IDS
            and classify_partner_reply(text) == "cooldown"
            and float(last_attempt.get("ts") or 0) >= float(last_success.get("ts") or 0)
            and parse_cooldown_retry_delay_seconds(text, ["冷却", "冷卻", "剩余", "请在"]) is not None):
        return reply_next_due_ts({"text": text, "ts": last_attempt["ts"]}, "cooldown", interval_seconds)
    return max(float(last_success.get("next_due_ts") or 0), float(last_attempt.get("next_due_ts") or 0))


def _persist(path: Path, payload: dict[str, Any]) -> None:
    persist_json_atomic(path, payload)


def message_row(message) -> dict[str, Any]:
    date = getattr(message, "date", None)
    edited = getattr(message, "edit_date", None)
    reply = getattr(message, "reply_to", None)
    return {"id": int(message.id), "sender_id": message.sender_id,
            "forwarded": getattr(message, "fwd_from", None) is not None,
            "chat_id": getattr(message, "chat_id", None), "text": (message.message or "").strip(),
            "date_ts": date.timestamp() if date else 0,
            "ts": (edited or date).timestamp() if (edited or date) else 0,
            "reply_to_msg_id": _reply_to_msg_id(message),
            "reply_to_top_id": getattr(reply, "reply_to_top_id", None)}


def select_anchor_round(rows: list[dict[str, Any]], anchor_id: int):
    """Reconcile only the verified two-account chain rooted at this anchor."""
    rows = [row for row in rows if row.get("chat_id") == CANONICAL_CHAT_ID and not row.get("forwarded")]
    anchor = next((row for row in rows if row["id"] == anchor_id
                   and row.get("sender_id") == int(INITIATOR_ACCOUNT[2:])
                   and row.get("text") == ANCHOR_TEXT
                   and row.get("reply_to_msg_id") == CANONICAL_TOPIC_ID), None)
    if anchor is None:
        return None
    commands = [row for row in rows if row.get("sender_id") == int(PARTNER_ACCOUNT[2:])
                and row.get("text") == ACTION_TEXT and row.get("reply_to_msg_id") == anchor_id
                and row["id"] > anchor_id and row.get("reply_to_top_id") == CANONICAL_TOPIC_ID
                and row.get("date_ts", 0) >= anchor.get("date_ts", 0)]
    results = []
    for command in commands:
        replies = [row for row in rows if row["id"] > command["id"]
                   and row.get("reply_to_top_id") == CANONICAL_TOPIC_ID
                   and row.get("ts", 0) >= command.get("date_ts", 0)]
        reply, status = select_direct_business_reply(replies, command_message_id=command["id"])
        if status in {"success", "cooldown"}:
            results.append({"anchor": anchor, "command": command, "reply": reply, "status": status,
                            "command_message_ids": sorted(row["id"] for row in commands)})
    # Legacy retries can produce a successful first command and later cooldown
    # replies. The settlement of this exact anchor still governs the round.
    return max(results, key=lambda row: (row["status"] == "success", row["reply"]["ts"])) if results else None


def persist_round_result(state_dir: Path, base: dict[str, Any], result: dict[str, Any], interval_seconds=3600):
    reply = result["reply"]
    payload = {**base, "status": result["status"], "ts": reply["ts"],
               "anchor_message_id": result["anchor"]["id"], "command_message_id": result["command"]["id"],
               "reply_message_id": reply["id"], "reply_sender_id": reply["sender_id"],
               "reply_to_msg_id": reply["reply_to_msg_id"], "reply_text": reply["text"],
               "next_due_ts": reply_next_due_ts(reply, result["status"], interval_seconds),
               "reconciled_command_message_ids": result["command_message_ids"], "reconciled_at_ts": time.time()}
    payload.pop("error_type", None)
    if payload["status"] == "success":
        _persist(state_dir / "last_success.json", payload)
    _persist(state_dir / "last_attempt.json", payload)
    return payload


async def reconcile_anchor_round(client, target, base, scan_limit):
    anchor_id = int(base.get("anchor_message_id") or 0)
    if not anchor_id:
        return None
    async def read():
        await ensure_connected(client)
        anchor = await client.get_messages(target, ids=anchor_id)
        following = await client.get_messages(target, min_id=anchor_id, reverse=True, limit=scan_limit)
        recent = await client.get_messages(target, limit=scan_limit)
        # Re-read known command neighbourhoods, including edited settlements
        # that have fallen out of the recent-message window.
        commands = (set(base.get("command_message_ids", [])) | {base.get("command_message_id")}) - {None}
        lookups = select_reply_scan_ids(commands, base.setdefault("reply_scans_by_message", {}), now=time.time())
        recovered = []
        # Match a recent receipt of any old copy by fetching just its parent.
        known = {getattr(message, "id", None) for message in [*following, *recent]}
        parents = {_reply_to_msg_id(message) for message in [*following, *recent]} & commands
        if missing := parents - known:
            recovered.extend(await client.get_messages(target, ids=sorted(missing)))
        for command_id in lookups:
            try:
                recovered.extend(await asyncio.wait_for(client.get_messages(
                    target, ids=list(range(command_id, command_id + 41))), 10))
            except TimeoutError:
                pass  # Retain the old copy; the next retry must not starve.
        rows = [message_row(message) for message in [anchor, *following, *recent, *recovered] if message is not None]
        rows = list({row["id"]: row for row in rows}.values())
        return select_anchor_round(rows, anchor_id)
    return await with_telegram_retry(read, label="reconcile shuangxiu anchor", reconnect=lambda: ensure_connected(client))


async def _connect_identity(session_name: str, api_id: int, api_hash: str, expected_id: int, expected_username: str):
    client = telegram_client(session_name, api_id, api_hash)
    await with_telegram_retry(lambda: client.start(), label=f"start account={expected_id}")
    me = await with_telegram_retry(client.get_me, label=f"get_me account={expected_id}", reconnect=lambda: ensure_connected(client))
    actual_id = int(getattr(me, "id", 0) or 0)
    actual_username = str(getattr(me, "username", "") or "").lower()
    if actual_id != expected_id or actual_username != expected_username.lower():
        await client.disconnect()
        raise RuntimeError(
            f"identity mismatch expected={expected_id}/@{expected_username} "
            f"actual={actual_id}/@{actual_username or '[none]'}"
        )
    return client, me


async def _wait_for_business_reply(
    client,
    *,
    chat_id: int | str,
    command_message_id: int,
    timeout_seconds: int,
    poll_seconds: int,
    scan_limit: int,
) -> tuple[dict[str, Any] | None, str]:
    deadline = time.monotonic() + timeout_seconds
    last_message_id = command_message_id
    watched_reply_ids: set[int] = set()

    async def read_after_command() -> list[Any]:
        # Advance through new history, while retaining direct bot replies that
        # may be edited into a final settlement after the cursor passes them.
        messages = [message async for message in client.iter_messages(
            chat_id, min_id=last_message_id, reverse=True, limit=scan_limit)]
        if watched_reply_ids:
            messages.extend(await client.get_messages(chat_id, ids=sorted(watched_reply_ids)))
        return messages

    while time.monotonic() < deadline:
        messages = await with_telegram_retry(read_after_command, label="poll shuangxiu reply",
                                             reconnect=lambda: ensure_connected(client))
        rows: list[dict[str, Any]] = []
        for message in messages:
            message_id = getattr(message, "id", None)
            if message_id is None or int(message_id) <= command_message_id:
                continue
            last_message_id = max(last_message_id, int(message_id))
            row = message_row(message)
            if row["chat_id"] == CANONICAL_CHAT_ID and row["reply_to_top_id"] == CANONICAL_TOPIC_ID:
                rows.append(row)
                if (row["reply_to_msg_id"] == command_message_id
                        and row["sender_id"] in TRUSTED_REPLY_SENDER_IDS and not row["forwarded"]):
                    watched_reply_ids.add(row["id"])
        selected, status = select_direct_business_reply(rows, command_message_id=command_message_id)
        if selected is not None and status in {"success", "cooldown"}:
            return selected, status
        await asyncio.sleep(max(1, poll_seconds))
    return None, "unknown"


async def run_once() -> None:
    load_dotenv()
    state_dir = Path(env("TG_STATE_DIR", "state/shuangxiu_wenyang"))
    state_dir.mkdir(parents=True, exist_ok=True)
    if is_paused(state_dir, "shuangxiu_wenyang")[0]:
        return
    try:
        for account in (INITIATOR_ACCOUNT, PARTNER_ACCOUNT):
            assert_account_allowed(account)
    except AccountPausedError as exc:
        # A definite local refusal: connecting both accounts and re-sending
        # would only be refused again and recorded as an unknown outcome.
        log_print(f"shuangxiu_wenyang account paused ({exc}); skip")
        return
    lock_path = state_dir / "send.lock"
    attempt_path = state_dir / "last_attempt.json"
    success_path = state_dir / "last_success.json"
    interval_seconds = max(DEFAULT_INTERVAL_SECONDS, int(env("TG_MIN_REPEAT_SECONDS", str(DEFAULT_INTERVAL_SECONDS))))
    timeout_seconds = max(FINAL_SETTLEMENT_WAIT_SECONDS, int(env("TG_ACTION_REPLY_TIMEOUT_SECONDS", str(FINAL_SETTLEMENT_WAIT_SECONDS))))
    poll_seconds = max(1, int(env("TG_REPLY_POLL_SECONDS", "2")))
    scan_limit = max(40, int(env("TG_SCAN_LIMIT", "120")))

    target_raw = env("TG_TARGET", CANONICAL_TARGET)
    topic_id = int(env("TG_TOPIC_ID", str(CANONICAL_TOPIC_ID)))
    target: int | str = int(target_raw) if target_raw.lstrip("-").isdigit() else target_raw
    if target not in {CANONICAL_TARGET, CANONICAL_CHAT_ID} or topic_id != CANONICAL_TOPIC_ID:
        raise ValueError("shuangxiu outbound route is not the canonical topic")

    last_success = load_json_dict(success_path)
    last_attempt = load_json_dict(attempt_path)
    next_due_ts = next_run_due_ts(last_success, last_attempt, interval_seconds)
    now = time.time()
    if next_due_ts > now and last_attempt.get("status") not in UNRESOLVED_STATUSES:
        log_print(f"shuangxiu_wenyang cooldown remaining={int(next_due_ts - now)}s")
        return

    try:
        lock_ctx = flow_lock(lock_path)
        lock_ctx.__enter__()
    except FlowLockBusy:
        log_print(f"shuangxiu_wenyang lock busy: {lock_path}")
        return

    initiator = None
    partner = None
    try:
        # Another invocation may have completed while this one was waiting for
        # the lease. Re-read the primary state under the lock.
        last_success = load_json_dict(success_path)
        last_attempt = load_json_dict(attempt_path)
        next_due_ts = next_run_due_ts(last_success, last_attempt, interval_seconds)
        if next_due_ts > time.time() and last_attempt.get("status") not in UNRESOLVED_STATUSES:
            return
        write_lock_metadata(
            lock_path,
            {
                "pid": os.getpid(),
                "flow_slug": "shuangxiu_wenyang",
                "started_at_ts": time.time(),
                "initiator_account": INITIATOR_ACCOUNT,
                "partner_account": PARTNER_ACCOUNT,
            },
        )
        initiator = await _connect_identity(
            env("INITIATOR_SESSION_NAME", required=True),
            int(env("INITIATOR_API_ID", required=True)),
            env("INITIATOR_API_HASH", required=True),
            int(INITIATOR_ACCOUNT[2:]),
            INITIATOR_USERNAME,
        )
        partner = await _connect_identity(
            env("PARTNER_SESSION_NAME", required=True),
            int(env("PARTNER_API_ID", required=True)),
            env("PARTNER_API_HASH", required=True),
            int(PARTNER_ACCOUNT[2:]),
            PARTNER_USERNAME,
        )

        if last_attempt.get("status") in UNRESOLVED_STATUSES:
            try:
                result = await reconcile_anchor_round(partner[0], target, last_attempt, scan_limit)
            finally:
                _persist(attempt_path, last_attempt)
            if result is not None:
                persist_round_result(state_dir, last_attempt, result, interval_seconds)
                log_print(f"shuangxiu_wenyang reconciled anchor={last_attempt.get('anchor_message_id')} status={result['status']}")
                return
            retry_at = float(last_attempt.get("retry_not_before_ts") or
                             (float(last_attempt.get("ts") or 0) + timeout_seconds))
            if time.time() < retry_at:
                log_print(f"shuangxiu_wenyang awaiting official reply; retry at {retry_at}")
                return

        base_payload: dict[str, Any] = {
            "ts": time.time(), "status": "anchor_send_submitted",
            "initiator_account": INITIATOR_ACCOUNT, "initiator_username": INITIATOR_USERNAME,
            "partner_account": PARTNER_ACCOUNT, "partner_username": PARTNER_USERNAME,
            "target": str(target_raw), "topic_id": topic_id,
            "anchor_text": ANCHOR_TEXT, "action_text": ACTION_TEXT,
        }
        if last_attempt.get("status") in UNRESOLVED_STATUSES:
            base_payload.update(last_attempt)
        base_payload.update(ts=time.time(), next_due_ts=None,
                            retry_not_before_ts=time.time() + timeout_seconds)
        anchor_id = int(base_payload.get("anchor_message_id") or 0)
        _persist(attempt_path, base_payload)

        if not anchor_id:
            try:
                anchor = await send_with_global_gate(
                    lambda: asyncio.wait_for(initiator[0].send_message(target, ANCHOR_TEXT, reply_to=topic_id), 25),
                    label="send shuangxiu anchor", state_dir=state_dir,
                )
            except Exception as exc:
                _persist(attempt_path, {**base_payload, "status": "anchor_send_unknown", "error_type": type(exc).__name__,
                    "retry_not_before_ts": time.time() + max(timeout_seconds, float(getattr(exc, "seconds", 0) or 0) + 1)})
                log_print(f"shuangxiu_wenyang anchor outcome unknown: {type(exc).__name__}; retry scheduled")
                return
            anchor_id = int(getattr(anchor, "id", 0) or 0)
        if not anchor_id:
            raise RuntimeError("initiator anchor send returned no message id")
        base_payload.update(anchor_message_id=anchor_id, status="command_send_submitted")
        _persist(attempt_path, base_payload)

        try:
            command = await send_with_global_gate(
                lambda: asyncio.wait_for(partner[0].send_message(target, ACTION_TEXT, reply_to=anchor_id), 25),
                label="send shuangxiu partner command", state_dir=state_dir,
            )
        except Exception as exc:
            _persist(attempt_path, {**base_payload, "status": "command_send_unknown", "error_type": type(exc).__name__,
                "retry_not_before_ts": time.time() + max(timeout_seconds, float(getattr(exc, "seconds", 0) or 0) + 1)})
            log_print(f"shuangxiu_wenyang command outcome unknown: {type(exc).__name__}; reconcile before any new send")
            return
        command_id = int(getattr(command, "id", 0) or 0)
        if not command_id:
            raise RuntimeError("partner command send returned no message id")

        commands = set(base_payload.get("command_message_ids", [])) | {base_payload.get("command_message_id"), command_id}
        base_payload.update(ts=time.time(), status="command_sent_waiting_reply", command_message_id=command_id,
                            command_message_ids=sorted(mid for mid in commands if mid))
        _persist(attempt_path, base_payload)

        reply, status = await _wait_for_business_reply(
            partner[0],
            chat_id=target,
            command_message_id=command_id,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            scan_limit=scan_limit,
        )
        payload = {
            **base_payload,
            "ts": float(reply.get("ts") or time.time()) if reply else time.time(),
            "status": status if reply else "sent_no_ack",
            "reply_message_id": int(reply["id"]) if reply else None,
            "reply_sender_id": reply.get("sender_id") if reply else None,
            "reply_to_msg_id": reply.get("reply_to_msg_id") if reply else None,
            "reply_text": str(reply.get("text") or "") if reply else "",
            "next_due_ts": None,
        }
        if status == "success" and reply is not None:
            payload["next_due_ts"] = compute_next_due_ts(float(reply.get("ts") or time.time()), interval_seconds)
            _persist(success_path, payload)
            log_print(
                f"shuangxiu_wenyang success anchor={anchor_id} command={command_id} "
                f"reply={reply['id']} next_due_ts={payload['next_due_ts']}"
            )
        elif status == "cooldown" and reply is not None:
            # Prefer the explicit server countdown; an untimed refusal keeps the hourly fallback.
            payload["next_due_ts"] = reply_next_due_ts(reply, status, interval_seconds)
            log_print(f"shuangxiu_wenyang server cooldown command={command_id} reply={reply['id']}")
        else:
            payload["status"] = "reply_retry_pending"
            payload["retry_not_before_ts"] = max(float(base_payload["retry_not_before_ts"]), time.time())
            log_print(f"shuangxiu_wenyang command={command_id} unconfirmed; retry on the same anchor after reconciliation")
        _persist(attempt_path, payload)
    finally:
        for pair in (partner, initiator):
            if pair is not None:
                try:
                    await disconnect_telegram_client(pair[0], 10)
                except Exception:
                    pass
        lock_ctx.__exit__(None, None, None)


def main() -> None:
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
