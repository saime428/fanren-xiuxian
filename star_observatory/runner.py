#!/usr/bin/env python3
"""Per-account 星宫灵圃 runner with server-timer scheduling and safe reconciliation."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from miniapp_shared.accounts import ACCOUNT_USERNAMES
from miniapp_shared.dwelling import MiniAppError, error_code, safe_text, verify_username
from miniapp_shared.state import LockBusy

from .adapter import StarAccountSession
from .protocol import STAR_NAME, parse_farm_state, safe_plot_snapshot
from .runtime import drive_observatory
from .state import account_lock, identity_label, read_account_state, write_account_state

WAKE_GRACE_SECONDS = 15
RETRY_SECONDS = 15 * 60
UNCERTAIN_RETRY_SECONDS = 30 * 60
# Even while every plot is maturing (~36 h for 天雷星), re-read the board a few
# times per cycle so a plot that turned 星光黯淡/元磁紊乱 gets soothed promptly.
MAX_IDLE_WAIT_SECONDS = 2 * 3600
LOCK_BUSY_RECHECK_SECONDS = 60
HARD_FAILURE_STATUSES = frozenset({"account_identity_mismatch", "sect_mismatch", "runtime_error"})


def _next_due(now: float, result: dict[str, Any]) -> float:
    status = str(result.get("status") or "")
    if status == "wait":
        wait = max(0, int(result.get("next_wait_sec", 0) or 0)) + WAKE_GRACE_SECONDS
        return float(now) + min(wait, MAX_IDLE_WAIT_SECONDS)
    if status in {"inspect", "action_limit", "action_unconfirmed", "action_failed"}:
        return float(now) + RETRY_SECONDS
    if status == "unknown_mutation_outcome":
        return float(now) + UNCERTAIN_RETRY_SECONDS
    if status in {"account_identity_mismatch", "sect_mismatch"}:
        return float(now) + 6 * 3600
    return float(now) + RETRY_SECONDS


async def run_account(root: Path, account: str, *, now: float, bridge_factory: Any, transport: Any,
                      force: bool = False) -> dict[str, Any]:
    try:
        with account_lock(root, account):
            return await _run_account_locked(root, account, now=now, bridge_factory=bridge_factory,
                                             transport=transport, force=force)
    except LockBusy:
        return {"account": account, "status": "lock_busy", "next_due_at": float(now) + LOCK_BUSY_RECHECK_SECONDS}


async def _run_account_locked(root: Path, account: str, *, now: float, bridge_factory: Any, transport: Any,
                              force: bool) -> dict[str, Any]:
    state = read_account_state(root, account)
    next_due_at = float(state.get("next_due_at", 0) or 0)
    if not force and next_due_at > float(now):
        return {"account": account, "status": "scheduled_wait", "next_due_at": next_due_at,
                "last_status": state.get("status")}

    result: dict[str, Any]
    label = identity_label(account)
    try:
        async with bridge_factory(root, account) as bridge:
            await verify_username(bridge, account)
            session = StarAccountSession(bridge, transport)
            start = await session.prepare()
            label = session.identity_label or label
            held = False
            # Only reachable through ``--force``: a scheduled run comes back no
            # earlier than UNCERTAIN_RETRY_SECONDS (see _next_due), when the hold
            # window has already passed. It keeps a forced re-run from replaying
            # an unconfirmed action on a board that has not moved yet.
            if state.get("status") == "unknown_mutation_outcome":
                current = safe_plot_snapshot(parse_farm_state(start))
                pending_before = list(state.get("pending_before") or ())
                age = float(now) - float(state.get("updated_at") or 0)
                if pending_before and current == pending_before and age < UNCERTAIN_RETRY_SECONDS:
                    # The board has not moved yet: hold this cycle instead of
                    # replaying the unconfirmed action. After the hold window a
                    # fresh read decides again; every action is validated by the
                    # server against the live board (a pull into a plot that is
                    # no longer empty cannot succeed twice).
                    held = True
                    result = {"ok": False, "status": "unknown_mutation_outcome",
                              "error": "mutation_transport_uncertain",
                              "pending_action": dict(state.get("pending_action") or {}),
                              "pending_before": pending_before,
                              "action_counts": dict(state.get("action_counts") or {})}
            if not held:
                result = await drive_observatory(start, session.action, refresh=session.snapshot,
                                                 star_name=STAR_NAME)
    except MiniAppError as exc:
        result = {"ok": False, "status": "account_identity_mismatch" if exc.code == "account_identity_mismatch"
                  else "runtime_error", "error": exc.code, "action_counts": {}}
    except Exception as exc:  # noqa: BLE001 - never leak transport internals
        result = {"ok": False, "status": "runtime_error", "error": error_code(exc), "action_counts": {}}

    persisted: dict[str, Any] = {
        "account": account,
        "username": ACCOUNT_USERNAMES.get(account, ""),
        "flow": "star_observatory",
        "star_name": STAR_NAME,
        "identity": label,
        "status": str(result.get("status") or "runtime_error"),
        "ok": bool(result.get("ok")),
        "updated_at": float(now),
        "next_due_at": _next_due(now, result),
        "action_counts": dict(result.get("action_counts") or {}),
        "benign": list(result.get("benign") or ()),
    }
    farm_state = result.get("farm_state")
    if isinstance(farm_state, dict) and farm_state:
        persisted["plots"] = safe_plot_snapshot(farm_state)
        persisted["max_wait_sec"] = int(farm_state.get("max_wait") or 0)
        persisted["min_wait_sec"] = int(farm_state.get("min_wait") or 0)
    if result.get("status") == "unknown_mutation_outcome":
        persisted["pending_action"] = dict(result.get("pending_action") or {})
        persisted["pending_before"] = list(result.get("pending_before") or ())
        persisted["error"] = "mutation_transport_uncertain"
    elif result.get("error"):
        persisted["error"] = safe_text(result.get("error"), 80)
    if result.get("ok") and result.get("status") in {"wait", "inspect", "action_limit"}:
        persisted["last_success_at"] = float(now)
    elif state.get("last_success_at"):
        persisted["last_success_at"] = float(state["last_success_at"])
    write_account_state(root, account, persisted)
    return persisted
