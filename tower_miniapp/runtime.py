#!/usr/bin/env python3
"""One identity's daily 琉璃问心塔 attempt with server-confirmed settlement."""
from __future__ import annotations

import asyncio
from typing import Any

from miniapp_shared.dwelling import MiniAppError

from .protocol import attempted_today, classify_challenge_response, parse_state, result_text

RECONCILE_ATTEMPTS = 6
RECONCILE_DELAY_SECONDS = 10.0
# An uncertain challenge is reconciled read-only first. If the server keeps
# showing no attempt for this long, the request never reached it (on 2026-09-22
# @demo_player_04's 00:41 challenge timed out, ``start`` showed todayHighest 0 /
# canChallenge true for 23 hours, and the old rule never challenged again; the
# operator climbed by hand). The pagoda ``start`` counters are the server's own
# record of an attempt, so a replay after that wait cannot double-spend, and
# the server's daily gate would reject one anyway.
REPLAY_AFTER_SECONDS = 10 * 60
MAX_REPLAYS_PER_DAY = 2
TERMINAL_STATUSES = frozenset({"settled_confirmed", "already_recorded", "reconciled_terminal"})


async def _reconcile(client: Any, *, attempts: int, delay: float, sleep: Any) -> dict[str, Any]:
    """Re-read ``start`` until the server shows today's attempt as recorded."""
    last: dict[str, Any] = {}
    for index in range(max(1, attempts)):
        try:
            last = parse_state(await client.start())
        except Exception:  # noqa: BLE001 - reconciliation is best effort, read-only
            last = {}
        if last and (attempted_today(last) or not last["can_challenge"]):
            return last
        if index + 1 < attempts:
            await sleep(delay)
    return last


async def run_tower_identity(client: Any, *, pending_uncertain: bool = False,
                             pending_age_seconds: float = 0.0, replay_count: int = 0,
                             sleep: Any = asyncio.sleep) -> dict[str, Any]:
    state = parse_state(await client.start())
    if not state:
        return {"ok": False, "status": "start_state_invalid", "error": "pagoda_state_missing"}
    if attempted_today(state):
        return {"ok": True, "status": "already_recorded", "state": state, "text": result_text({}, state)}
    if not state["can_challenge"]:
        return {"ok": False, "status": "not_ready", "state": state, "error": "pagoda_not_ready"}
    replaying = False
    if pending_uncertain:
        # An earlier challenge had an unknown outcome and the server still shows
        # no attempt: reconcile first; challenge again only after the wait.
        reconciled = await _reconcile(client, attempts=2, delay=RECONCILE_DELAY_SECONDS, sleep=sleep)
        if reconciled and (attempted_today(reconciled) or not reconciled["can_challenge"]):
            return {"ok": True, "status": "reconciled_terminal", "state": reconciled,
                    "text": result_text({}, reconciled)}
        if pending_age_seconds < REPLAY_AFTER_SECONDS or replay_count >= MAX_REPLAYS_PER_DAY:
            return {"ok": False, "status": "mutation_uncertain", "state": state,
                    "error": "pending_challenge_not_replayed" if replay_count < MAX_REPLAYS_PER_DAY
                             else "replay_limit_reached", "replay_count": replay_count}
        replaying = True

    replays = replay_count + 1 if replaying else replay_count
    try:
        reply = await client.challenge()
    except MiniAppError as exc:
        if not exc.uncertain:
            return {"ok": False, "status": "challenge_rejected", "state": state, "error": exc.code,
                    "replay_count": replays}
        reconciled = await _reconcile(client, attempts=RECONCILE_ATTEMPTS, delay=RECONCILE_DELAY_SECONDS, sleep=sleep)
        if reconciled and (attempted_today(reconciled) or not reconciled["can_challenge"]):
            return {"ok": True, "status": "reconciled_terminal", "state": reconciled,
                    "text": result_text({}, reconciled), "replay_count": replays}
        return {"ok": False, "status": "mutation_uncertain", "state": reconciled or state,
                "error": "challenge_transport_uncertain", "replay_count": replays}
    except Exception:  # noqa: BLE001
        return {"ok": False, "status": "mutation_uncertain", "state": state, "error": "challenge_transport_uncertain",
                "replay_count": replays}

    classified = classify_challenge_response(reply)
    if classified["status"] == "settled_confirmed":
        return {"ok": True, "status": "settled_confirmed", "state": classified["state"],
                "replay": classified["replay"], "text": result_text(classified["replay"], classified["state"]),
                "replay_count": replays}
    reconciled = await _reconcile(client, attempts=RECONCILE_ATTEMPTS, delay=RECONCILE_DELAY_SECONDS, sleep=sleep)
    if reconciled and (attempted_today(reconciled) or not reconciled["can_challenge"]):
        return {"ok": True, "status": "reconciled_terminal", "state": reconciled,
                "replay": classified.get("replay") or {}, "text": result_text(classified.get("replay"), reconciled),
                "replay_count": replays}
    return {"ok": False, "status": "oracle_missing", "state": reconciled or classified.get("state") or state,
            "replay": classified.get("replay") or {}, "error": "challenge_settlement_unconfirmed",
            "replay_count": replays}
