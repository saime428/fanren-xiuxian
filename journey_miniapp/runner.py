#!/usr/bin/env python3
"""Per-account 游历 runner: from 00:00 Asia/Shanghai, fill eight 谨慎 attempts per day."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from miniapp_shared import daily
from miniapp_shared.accounts import ACCOUNT_USERNAMES, stagger_offsets
from miniapp_shared.dwelling import MiniAppError, error_code, open_dwelling, safe_text, verify_username
from miniapp_shared.state import LockBusy, shanghai_now

from .adapter import JourneyIdentityClient
from .protocol import MODE, MODE_LABEL, TARGET_DAILY_COUNT
from .runtime import AWAITING_RESET_RETRY_SECONDS, run_journey_identity
from .state import (
    account_lock,
    read_identity,
    read_state,
    scoped_identities,
    write_identity,
    write_state,
)

FAILURE_RETRY_SECONDS = 15 * 60
UNCERTAIN_RETRY_SECONDS = 10 * 60
MIN_RETRY_SECONDS = 60
# Daily timetable (docs/automation-profiles/README.md): 灵溪垂钓 opens the day at
# 00:00, so the 游历 batch runs 00:15-00:30. Accounts are admitted three
# minutes apart so six dwelling logins never hit the API together.
START_HOUR, START_MINUTE = 0, 15
ACCOUNT_MINUTE_OFFSETS = stagger_offsets("journey", 3)


def admitted(account: str, now_ts: float) -> bool:
    return daily.admitted(account, now_ts, start_hour=START_HOUR, start_minute=START_MINUTE,
                          offsets=ACCOUNT_MINUTE_OFFSETS)


# A full counter seen within this window after Asia/Shanghai midnight, with no
# usable server resetAt, is yesterday's counter still waiting for the server
# roll-over; it must not be cached as today's completion. The window must cover
# the whole 00:15-00:30 admission batch plus retries, hence 45 minutes.
POST_MIDNIGHT_GUARD_SECONDS = 45 * 60
RESET_GRACE_SECONDS = 60
SERVER_WAIT_STATUSES = frozenset({"daily_complete", "cooldown", "awaiting_reset"})


def seconds_since_midnight(now_ts: float) -> int:
    now = shanghai_now(now_ts)
    return now.hour * 3600 + now.minute * 60 + now.second


def _identity_due(record: dict[str, Any], player_id: int, day_key: str, now: float) -> bool:
    if record.get("player_id") != int(player_id) or record.get("day") != day_key:
        return True
    next_due = float(record.get("next_due_at") or 0)
    if record.get("status") == "daily_complete":
        # Complete until the server's own reset when it told us one; otherwise
        # complete for the rest of the calendar day.
        return next_due > 0 and next_due <= float(now)
    return next_due <= float(now)


async def run_account(root: Path, account: str, *, day_key: str, now: float, bridge_factory: Any,
                      transport: Any, force: bool = False, sleep: Any = None) -> dict[str, Any]:
    prior = read_state(root, account)
    if not force and prior.get("day") == day_key:
        next_due = float(prior.get("next_due_at") or 0)
        if prior.get("status") == "daily_complete" and (next_due <= 0 or next_due > float(now)):
            return {"ok": True, "status": "daily_already_complete", "account": account, "day": day_key,
                    "identities": prior.get("identities", []), **({"next_due_at": next_due} if next_due > 0 else {})}
        if next_due > float(now):
            return {"ok": True, "status": "scheduled_wait", "account": account, "day": day_key,
                    "next_due_at": next_due, "last_status": prior.get("status")}
    if not force and not admitted(account, now):
        return {"ok": True, "status": "waiting_admission", "account": account, "day": day_key}
    try:
        with account_lock(root, account):
            return await _run_locked(root, account, day_key=day_key, now=now, bridge_factory=bridge_factory,
                                     transport=transport, force=force, sleep=sleep)
    except LockBusy:
        return {"ok": True, "status": "lock_busy", "account": account, "day": day_key}


async def _run_locked(root: Path, account: str, *, day_key: str, now: float, bridge_factory: Any,
                      transport: Any, force: bool, sleep: Any) -> dict[str, Any]:
    identity_results: list[dict[str, Any]] = []
    next_due_candidates: list[float] = []
    account_error = ""
    skipped: list[dict[str, Any]] = []
    try:
        async with bridge_factory(root, account) as bridge:
            await verify_username(bridge, account)
            dwelling = await open_dwelling(bridge, transport)
            targets = scoped_identities(account, dwelling.identities)
            skipped = [{"player_id": int(i.player_id), "identity": i.label, "source": i.source}
                       for i in dwelling.identities if i not in targets]
            if not targets:
                raise MiniAppError("scope_identity_missing")
            for index, identity in enumerate(targets):
                record = read_identity(root, account, identity.player_id)
                if not force and not _identity_due(record, identity.player_id, day_key, now):
                    identity_results.append({**_public(record), "cached": True})
                    if float(record.get("next_due_at") or 0) > float(now):
                        next_due_candidates.append(float(record["next_due_at"]))
                    continue
                client = JourneyIdentityClient(transport, dwelling, identity.player_id)
                kwargs = {"sleep": sleep} if sleep is not None else {}
                try:
                    result = await run_journey_identity(client, now_ts=now, **kwargs)
                except Exception as exc:  # noqa: BLE001 - codes only, never transport internals
                    result = {"status": "identity_failed", "error": error_code(exc), "actions": 0,
                              "retry_after": FAILURE_RETRY_SECONDS}
                persisted = _persist_identity(result, record=record, identity=identity, day_key=day_key, now=now)
                if persisted.get("next_due_at"):
                    next_due_candidates.append(float(persisted["next_due_at"]))
                write_identity(root, account, identity.player_id, persisted)
                identity_results.append(_public(persisted))
                if index + 1 < len(targets):
                    await (sleep or _default_sleep)(1.0)
    except Exception as exc:  # noqa: BLE001 - codes only, never transport internals
        account_error = error_code(exc)

    complete = bool(identity_results) and all(item.get("status") == "daily_complete" for item in identity_results)
    if account_error:
        status = "account_failed"
    elif complete:
        status = "daily_complete"
    else:
        status = "waiting_server" if all(item.get("status") in SERVER_WAIT_STATUSES for item in identity_results) else "retry"
    summary = {
        "account": account, "username": ACCOUNT_USERNAMES.get(account, ""), "flow": "journey_miniapp",
        "day": day_key, "mode": MODE, "mode_label": MODE_LABEL, "target": TARGET_DAILY_COUNT,
        "status": status, "ok": status in {"daily_complete", "waiting_server"}, "updated_at": float(now),
        "identity_count": len(identity_results), "identities": identity_results,
        "skipped_identities": skipped,
    }
    if account_error:
        summary["error"] = account_error
        summary["next_due_at"] = float(now) + FAILURE_RETRY_SECONDS
    elif complete:
        if next_due_candidates:
            summary["next_due_at"] = min(next_due_candidates)
    else:
        summary["next_due_at"] = min(next_due_candidates) if next_due_candidates else float(now) + FAILURE_RETRY_SECONDS
    write_state(root, account, summary)
    return summary


def _persist_identity(result: dict[str, Any], *, record: dict[str, Any], identity: Any, day_key: str,
                      now: float) -> dict[str, Any]:
    status = str(result.get("status") or "identity_failed")
    retry_after = int(result.get("retry_after") or 0)
    reset_at = float(result.get("reset_at") or 0)
    actions = int(result.get("actions") or 0)
    note = ""
    if status == "daily_limit":
        # The server says today's quota is spent.
        status, note = "daily_complete", "wild_experience_daily_limit"
    if (status == "daily_complete" and actions == 0 and record.get("day") != day_key
            and reset_at <= float(now) and seconds_since_midnight(now) < POST_MIDNIGHT_GUARD_SECONDS):
        status, note = "awaiting_reset", "stale_counter_after_midnight"
        retry_after = AWAITING_RESET_RETRY_SECONDS
    if status == "mutation_uncertain":
        retry_after = max(retry_after, UNCERTAIN_RETRY_SECONDS)
    elif status not in SERVER_WAIT_STATUSES:
        retry_after = max(retry_after, FAILURE_RETRY_SECONDS)
    previous_attempts = list(record.get("attempts") or ()) if record.get("day") == day_key else []
    persisted: dict[str, Any] = {
        "player_id": int(identity.player_id), "identity": identity.label, "source": identity.source,
        "day": day_key, "mode": MODE, "mode_label": MODE_LABEL,
        "target": int(result.get("target") or TARGET_DAILY_COUNT),
        "status": status, "daily_count": int(result.get("daily_count") or 0),
        "actions_this_run": actions,
        "attempts": (previous_attempts + list(result.get("attempts") or ()))[-TARGET_DAILY_COUNT:],
        "updated_at": time.time(),
    }
    if reset_at > 0:
        persisted["server_reset_at"] = reset_at
    if note:
        persisted["server_note"] = note
    if status == "daily_complete":
        if reset_at > float(now):
            # Complete until the server rolls its own day; a later same-day
            # reset (non-midnight server boundary) re-opens the quota.
            persisted["next_due_at"] = reset_at + RESET_GRACE_SECONDS
    else:
        persisted["next_due_at"] = float(now) + max(MIN_RETRY_SECONDS, retry_after)
    if result.get("error"):
        persisted["error"] = safe_text(result["error"], 120)
    return persisted


def _public(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in ("player_id", "identity", "source", "status", "daily_count", "target",
                                             "actions_this_run", "next_due_at", "server_reset_at", "error",
                                             "server_note")
            if record.get(key) is not None}


async def _default_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
