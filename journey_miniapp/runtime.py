#!/usr/bin/env python3
"""Use every currently available 谨慎 attempt for one identity; follow server timers."""
from __future__ import annotations

import asyncio
from typing import Any

from miniapp_shared.dwelling import MiniAppError

from .protocol import COOLDOWN_ERRORS, LIMIT_ERRORS, TARGET_DAILY_COUNT, classify_attempt, parse_wild_state

ACTION_DELAY_SECONDS = 2.0
UNCONFIRMED_RETRY_SECONDS = 120
AWAITING_RESET_RETRY_SECONDS = 120


def _target(state: dict[str, Any]) -> int:
    limit = int(state.get("daily_limit") or 0)
    return min(TARGET_DAILY_COUNT, limit) if limit > 0 else TARGET_DAILY_COUNT


def awaiting_server_reset(state: dict[str, Any], *, now_ts: float, target: int) -> bool:
    """True when the counter is full but the server's daily reset is already due.

    Right after Asia/Shanghai midnight the server may still show yesterday's
    eight attempts with a ``resetAt`` in the past; treating that as today's
    completion would skip the whole day. Wait for the server to roll instead.
    """
    reset_at = float(state.get("reset_at") or 0)
    return bool(state.get("daily_count", 0) >= target and reset_at > 0 and reset_at <= float(now_ts))


async def run_journey_identity(client: Any, *, now_ts: float, sleep: Any = asyncio.sleep) -> dict[str, Any]:
    """Return ``{"status", "daily_count", "target", "actions", "retry_after", ...}``.

    ``status`` values: ``daily_complete`` (server count reached today's target),
    ``cooldown`` (wait ``retry_after`` seconds for the server timer),
    ``mutation_uncertain`` (one action's outcome unknown; reconcile before any
    replay), ``unconfirmed`` (server replied but did not confirm the attempt),
    ``rejected`` (explicit server error), ``state_missing``.
    """
    state = parse_wild_state(await client.details(), now_ts=now_ts)
    if not state:
        return {"status": "state_missing", "actions": 0}
    target = _target(state)
    if awaiting_server_reset(state, now_ts=now_ts, target=target):
        return {"status": "awaiting_reset", "daily_count": state["daily_count"], "target": target, "actions": 0,
                "attempts": [], "retry_after": AWAITING_RESET_RETRY_SECONDS, "reset_at": state.get("reset_at", 0)}
    actions = 0
    attempts: list[dict[str, Any]] = []
    while state["daily_count"] < target:
        if actions >= target:
            return {"status": "action_budget_exhausted", "daily_count": state["daily_count"], "target": target,
                    "actions": actions, "attempts": attempts, "retry_after": UNCONFIRMED_RETRY_SECONDS}
        if not state["available"] or state["daily_remaining"] <= 0:
            wait = int(state.get("remaining_seconds") or 0)
            return {"status": "cooldown", "daily_count": state["daily_count"], "target": target,
                    "actions": actions, "attempts": attempts,
                    "retry_after": max(30, wait) if wait > 0 else UNCONFIRMED_RETRY_SECONDS,
                    "server_available": state["available"]}
        before = int(state["daily_count"])
        try:
            reply = await client.act()
        except MiniAppError as exc:
            if exc.uncertain:
                return {"status": "mutation_uncertain", "daily_count": before, "target": target, "actions": actions,
                        "attempts": attempts, "pending_before_count": before, "error": exc.code,
                        "retry_after": UNCONFIRMED_RETRY_SECONDS}
            if exc.code in COOLDOWN_ERRORS or exc.code in LIMIT_ERRORS:
                try:
                    state = parse_wild_state(await client.details(), now_ts=now_ts) or state
                except Exception:  # noqa: BLE001
                    pass
                wait = int(state.get("remaining_seconds") or 0)
                return {"status": "cooldown" if exc.code in COOLDOWN_ERRORS else "daily_limit",
                        "daily_count": state["daily_count"], "target": target, "actions": actions,
                        "attempts": attempts, "error": exc.code,
                        "retry_after": max(30, wait) if wait > 0 else UNCONFIRMED_RETRY_SECONDS}
            return {"status": "rejected", "daily_count": before, "target": target, "actions": actions,
                    "attempts": attempts, "error": exc.code, "retry_after": UNCONFIRMED_RETRY_SECONDS}
        except Exception:  # noqa: BLE001
            return {"status": "mutation_uncertain", "daily_count": before, "target": target, "actions": actions,
                    "attempts": attempts, "pending_before_count": before, "error": "journey_transport_uncertain",
                    "retry_after": UNCONFIRMED_RETRY_SECONDS}
        verdict = classify_attempt(reply, before_count=before, now_ts=now_ts)
        if verdict["status"] == "rejected":
            code = verdict["error"]
            if code in COOLDOWN_ERRORS or code in LIMIT_ERRORS:
                refreshed = verdict.get("state") or {}
                wait = int(refreshed.get("remaining_seconds") or 0)
                return {"status": "cooldown" if code in COOLDOWN_ERRORS else "daily_limit",
                        "daily_count": refreshed.get("daily_count", before), "target": target, "actions": actions,
                        "attempts": attempts, "error": code,
                        "retry_after": max(30, wait) if wait > 0 else UNCONFIRMED_RETRY_SECONDS}
            return {"status": "rejected", "daily_count": before, "target": target, "actions": actions,
                    "attempts": attempts, "error": code, "retry_after": UNCONFIRMED_RETRY_SECONDS}
        if verdict["status"] != "confirmed":
            # The server answered but did not prove the attempt. Re-read once;
            # only a server counter that advanced counts.
            try:
                refreshed = parse_wild_state(await client.details(), now_ts=now_ts)
            except Exception:  # noqa: BLE001
                refreshed = {}
            if refreshed and refreshed["daily_count"] == before + 1:
                verdict = {"status": "confirmed", "state": refreshed, "outcome": "", "mode": "",
                           "cultivation_delta": 0, "title": "", "error": ""}
            else:
                return {"status": "unconfirmed", "daily_count": (refreshed or state)["daily_count"], "target": target,
                        "actions": actions, "attempts": attempts, "error": verdict["error"],
                        "retry_after": UNCONFIRMED_RETRY_SECONDS}
        actions += 1
        attempts.append({"count": before + 1, "outcome": verdict.get("outcome", ""), "mode": verdict.get("mode", ""),
                         "cultivation_delta": verdict.get("cultivation_delta", 0), "title": verdict.get("title", "")})
        state = verdict.get("state") or {}
        if not state:
            state = parse_wild_state(await client.details(), now_ts=now_ts)
            if not state:
                return {"status": "state_missing", "daily_count": before + 1, "target": target, "actions": actions,
                        "attempts": attempts, "retry_after": UNCONFIRMED_RETRY_SECONDS}
        if state["daily_count"] < target and state["available"] and state["daily_remaining"] > 0:
            await sleep(ACTION_DELAY_SECONDS)
    return {"status": "daily_complete", "daily_count": state["daily_count"], "target": target, "actions": actions,
            "attempts": attempts, "retry_after": 0, "reset_at": state.get("reset_at", 0)}
