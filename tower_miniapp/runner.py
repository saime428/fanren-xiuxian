#!/usr/bin/env python3
"""Per-account 琉璃问心塔 runner: every dwelling identity, once per Asia/Shanghai day."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from miniapp_shared import daily
from miniapp_shared.accounts import ACCOUNT_USERNAMES, stagger_offsets
from miniapp_shared.dwelling import error_code, open_dwelling, safe_text, verify_username
from miniapp_shared.state import LockBusy

from .adapter import TowerIdentityClient
from .runtime import TERMINAL_STATUSES, run_tower_identity
from .state import (
    account_lock,
    read_identity,
    read_state,
    write_identity,
    write_state,
)

# Daily admission gate (Asia/Shanghai). Timetable slot 02:00-02:10 (operator
# request 2026-09-23: "琉璃问心塔调整到凌晨2点开始吧"), after 宗门日常 (01:00) and the
# first 天机试炼 pass (01:30-01:40): see docs/automation-profiles/README.md.
# Retries continue every tick until done.
START_HOUR = 2
START_MINUTE = 0
ACCOUNT_MINUTE_OFFSETS = stagger_offsets("tower", 2)
RETRY_SECONDS = 15 * 60


def admitted(account: str, now_ts: float) -> bool:
    return daily.admitted(account, now_ts, start_hour=START_HOUR, start_minute=START_MINUTE,
                          offsets=ACCOUNT_MINUTE_OFFSETS)


def _identity_terminal(record: dict[str, Any], player_id: int, day_key: str) -> bool:
    return (record.get("player_id") == int(player_id) and record.get("day") == day_key
            and record.get("status") in TERMINAL_STATUSES)


async def run_account(root: Path, account: str, *, day_key: str, now: float, bridge_factory: Any,
                      transport: Any, force: bool = False, sleep: Any = None) -> dict[str, Any]:
    prior = read_state(root, account)
    if prior.get("last_terminal_day") == day_key and not force:
        return {"ok": True, "status": "daily_already_terminal", "account": account, "day": day_key,
                "identities": prior.get("identities", [])}
    if not force and not admitted(account, now):
        return {"ok": True, "status": "waiting_admission", "account": account, "day": day_key}
    if not force and prior.get("day") == day_key and float(prior.get("next_due_at") or 0) > float(now):
        return {"ok": True, "status": "scheduled_wait", "account": account, "day": day_key,
                "next_due_at": float(prior["next_due_at"]), "last_status": prior.get("status")}
    try:
        with account_lock(root, account):
            return await _run_locked(root, account, day_key=day_key, now=now, bridge_factory=bridge_factory,
                                     transport=transport, sleep=sleep)
    except LockBusy:
        return {"ok": True, "status": "lock_busy", "account": account, "day": day_key}


async def _run_locked(root: Path, account: str, *, day_key: str, now: float, bridge_factory: Any,
                      transport: Any, sleep: Any) -> dict[str, Any]:
    identity_results: list[dict[str, Any]] = []
    account_error = ""
    try:
        async with bridge_factory(root, account) as bridge:
            await verify_username(bridge, account)
            dwelling = await open_dwelling(bridge, transport)
            for index, identity in enumerate(dwelling.identities):
                record = read_identity(root, account, identity.player_id)
                if _identity_terminal(record, identity.player_id, day_key):
                    identity_results.append({**_public(record), "status": record["status"], "cached": True})
                    continue
                pending = bool(record.get("day") == day_key and record.get("status") == "mutation_uncertain")
                uncertain_since = float(record.get("uncertain_since_ts") or 0) if pending else 0.0
                replay_count = int(record.get("replay_count") or 0) if record.get("day") == day_key else 0
                client = TowerIdentityClient(transport, dwelling, identity.player_id)
                kwargs = {"sleep": sleep} if sleep is not None else {}
                try:
                    result = await run_tower_identity(
                        client, pending_uncertain=pending,
                        pending_age_seconds=(float(now) - uncertain_since) if uncertain_since else 0.0,
                        replay_count=replay_count, **kwargs)
                except Exception as exc:  # noqa: BLE001 - codes only, never transport internals
                    result = {"ok": False, "status": "identity_failed", "error": error_code(exc)}
                persisted = {
                    "player_id": int(identity.player_id), "identity": identity.label, "source": identity.source,
                    "day": day_key, "status": str(result.get("status") or "identity_failed"),
                    "ok": bool(result.get("ok")), "updated_at": time.time(),
                    "replay_count": int(result.get("replay_count") or replay_count),
                }
                if persisted["status"] == "mutation_uncertain":
                    # Age the pending challenge from its first uncertain outcome,
                    # not from the latest read-only reconciliation.
                    persisted["uncertain_since_ts"] = uncertain_since or float(now)
                for key in ("state", "replay", "text"):
                    if result.get(key):
                        persisted[key] = result[key]
                if result.get("error"):
                    persisted["error"] = safe_text(result["error"], 120)
                if persisted["status"] in TERMINAL_STATUSES:
                    persisted["last_terminal_day"] = day_key
                write_identity(root, account, identity.player_id, persisted)
                identity_results.append(_public(persisted))
                if index + 1 < len(dwelling.identities):
                    await (sleep or _default_sleep)(1.0)
    except Exception as exc:  # noqa: BLE001 - codes only, never transport internals
        account_error = error_code(exc)

    all_terminal = bool(identity_results) and all(item.get("status") in TERMINAL_STATUSES for item in identity_results)
    status = "completed" if all_terminal and not account_error else ("account_failed" if account_error else "retry")
    summary = {
        "account": account, "username": ACCOUNT_USERNAMES.get(account, ""), "flow": "tower_miniapp",
        "day": day_key, "status": status, "ok": status == "completed", "updated_at": float(now),
        "identity_count": len(identity_results), "identities": identity_results,
    }
    if account_error:
        summary["error"] = account_error
    if status == "completed":
        summary["last_terminal_day"] = day_key
    else:
        summary["next_due_at"] = float(now) + RETRY_SECONDS
        previous_terminal = read_state(root, account).get("last_terminal_day")
        if previous_terminal:
            summary["last_terminal_day"] = previous_terminal
    write_state(root, account, summary)
    return summary


def _public(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in ("player_id", "identity", "source", "status", "ok", "text", "error")
            if record.get(key) is not None}


async def _default_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
