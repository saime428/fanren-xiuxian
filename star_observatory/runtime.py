#!/usr/bin/env python3
"""Drive one 星宫灵圃 maintenance cycle against server-confirmed plot state."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from miniapp_shared.dwelling import MiniAppError

from .protocol import (
    BENIGN_ACTION_ERRORS,
    STAR_NAME,
    choose_action,
    classify_action,
    parse_farm_state,
    safe_plot_snapshot,
)

ActionSender = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
Refresher = Callable[[], Awaitable[dict[str, Any]]]


async def drive_observatory(
    start_data: dict[str, Any],
    send_action: ActionSender,
    *,
    refresh: Refresher | None = None,
    star_name: str = STAR_NAME,
) -> dict[str, Any]:
    """Run soothe → collect → pull until every plot is maturing.

    Every mutation is judged only by the server's ``actionResult`` and the
    refreshed ``domain``. A transport failure after a mutation returns
    ``unknown_mutation_outcome`` with the pre-action snapshot so the caller can
    reconcile read-only before any replay.
    """
    current: dict[str, Any] = start_data
    state = parse_farm_state(current)
    if not state:
        return {"ok": False, "status": "invalid_state", "action_counts": {}}
    if state.get("sect_name") and state["sect_name"] != "星宫":
        return {"ok": False, "status": "sect_mismatch", "action_counts": {}, "sect_name": state["sect_name"]}

    counts = {"soothe": 0, "collect": 0, "pull": 0}
    benign: list[str] = []
    skip: set[str] = set()
    max_actions = len(state["plots"]) * 2 + 4
    for _ in range(max_actions):
        state = parse_farm_state(current)
        if not state:
            return {"ok": False, "status": "invalid_state", "action_counts": counts, "benign": benign}
        decision = choose_action(state, star_name=star_name, skip=skip)
        action = str(decision.get("action") or "")
        if action in {"wait", "inspect"}:
            return {
                "ok": True, "status": action, "action_counts": counts, "benign": benign,
                "next_wait_sec": int(decision.get("wait_sec", 0) or 0), "farm_state": state,
            }
        payload = {"action": action, "plotKey": str(decision.get("plotKey") or "")}
        if action == "pull":
            payload["starName"] = str(decision.get("starName") or star_name)
        before = safe_plot_snapshot(state)
        try:
            reply = await send_action(payload)
        except MiniAppError as exc:
            if exc.uncertain:
                return _uncertain(payload, before, counts, benign)
            if exc.code in BENIGN_ACTION_ERRORS.get(action, frozenset()):
                # Top-level ``{"ok": false, "error": "nothing_ready"}`` without an
                # actionResult is the same harmless no-op as the nested form.
                reply = {"ok": False, "actionResult": {"ok": False, "error": exc.code}}
            else:
                return {"ok": False, "status": "action_failed", "error": exc.code, "action": action,
                        "action_counts": counts, "benign": benign, "farm_state": state}
        except Exception:  # noqa: BLE001 - transport internals never reach state files
            return _uncertain(payload, before, counts, benign)
        verdict = classify_action(reply, action)
        if not verdict["ok"]:
            return {"ok": False, "status": "action_failed", "error": verdict["error"] or "action_rejected",
                    "action": action, "action_counts": counts, "benign": benign, "farm_state": state}
        refreshed_raw = reply if parse_farm_state(reply) else None
        if refreshed_raw is None and refresh is not None:
            try:
                refreshed_raw = await refresh()
            except Exception:  # noqa: BLE001
                return {"ok": False, "status": "refresh_failed", "action_counts": counts, "benign": benign}
        refreshed = parse_farm_state(refreshed_raw)
        if not refreshed:
            return {"ok": False, "status": "invalid_state", "action_counts": counts, "benign": benign}
        if verdict["benign"]:
            benign.append(f"{action}:{verdict['error']}")
            if safe_plot_snapshot(refreshed) == before:
                # The server reported nothing to do and the board did not move:
                # stop applying this rule for the rest of the cycle.
                skip.add(action)
        else:
            counts[action] += 1
            if action == "pull" and safe_plot_snapshot(refreshed) == before:
                # A confirmed pull that leaves the board unchanged would loop on
                # the same empty plot; treat it as an unconfirmed outcome.
                return {"ok": False, "status": "action_unconfirmed", "error": "board_unchanged_after_pull",
                        "action": action, "action_counts": counts, "benign": benign, "farm_state": refreshed}
        current = refreshed_raw

    return {"ok": True, "status": "action_limit", "action_counts": counts, "benign": benign,
            "next_wait_sec": 0, "farm_state": parse_farm_state(current)}


def _uncertain(payload: dict[str, Any], before: list[dict[str, Any]], counts: dict[str, int],
               benign: list[str]) -> dict[str, Any]:
    return {
        "ok": False, "status": "unknown_mutation_outcome", "error": "mutation_transport_uncertain",
        "pending_action": dict(payload), "pending_before": before, "action_counts": counts, "benign": benign,
    }
