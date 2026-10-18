"""Shared journal rules for the QYZ host and its read-only health collector."""
from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any

from miniapp_shared.accounts import GAME_CHAT_ID, GAME_SECOND_CHAT_ID, GAME_TOPIC_ID

RUNTIME_VERSION = "qyz-reference-v32"
SOURCE_CHAT_TOPICS = {GAME_CHAT_ID: GAME_TOPIC_ID, GAME_SECOND_CHAT_ID: None}
SETTLED_STATUSES = frozenset({"settled_perfect", "settled_partial", "settled_damage_reduced"})
FAILED_EVENT_STATUSES = frozenset({
    "runtime_error",
    "unknown_mutation_outcome",
    "settled_partial",
    "settled_damage_reduced",
    "blocked_official_verification_required",
})
MUTATION_PHASES = frozenset(
    f"{endpoint}_{phase}"
    for endpoint in ("start", "begin", "charge-start", "hit", "finish")
    for phase in ("submitted", "confirmed")
) | {"hit_accepted", "verification_accepted", "battle_started"}


def server_damage_multiplier_min(record: Any) -> float | None:
    """Read official multipliers, including retained v16 nested receipts.

    Do not infer a penalty from a risk label, a local perfect count, or a score.
    A final multiplier of 1 also cannot erase penalties on earlier real hits.
    """
    if not isinstance(record, dict):
        return None
    result = record.get("protocol_result", record)
    if not isinstance(result, dict):
        return None
    outcome = result.get("reference_outcome")
    diagnostics = outcome.get("diagnostics", {}) if isinstance(outcome, dict) else {}
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    finish = diagnostics.get("finish")
    sources = [result.get("server_result"),
               finish.get("server_result") if isinstance(finish, dict) else None]
    values = []
    for source in sources:
        risk = source.get("automation_risk") if isinstance(source, dict) else None
        if isinstance(risk, dict):
            values.append(risk.get("damageMultiplier"))
    hits = diagnostics.get("hits", [])
    for hit in hits if isinstance(hits, list) else []:
        server = hit.get("server_hit") if isinstance(hit, dict) else None
        if isinstance(server, dict):
            values.append(server.get("automationDamageMultiplier"))
    valid = [float(value) for value in values if not isinstance(value, bool)
             and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1]
    return min(valid) if valid else None


# An event lives about eight minutes on the server (60 s admission, ~300 s
# battle, settlement post ~7 min after the entry). A record older than this can
# never be resolved by a later heartbeat: the outcome stays whatever the journal
# says, and the next event is an independent battle. Until 2026-09-21 every
# failed-after-/start record blocked health forever (six accounts red since
# 09-15), so the total miss of event 132 raised no new alarm.
EVENT_RECONCILIATION_WINDOW_SECONDS = 30 * 60


def record_updated_ts(record: Any) -> float | None:
    """Host-local ``updated_at`` (``%Y-%m-%d %H:%M:%S``) as a timestamp, else None."""
    if not isinstance(record, dict):
        return None
    text = record.get("updated_at")
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


# Keys whose only consumer is the immutable per-entry archive. Keeping them
# in the live journal made every account's file ~1 MB (11 events), so each
# in-battle save cost 5-7 ms on the host loop (2026-09-22 review).
HEAVY_RESULT_KEYS = ("transport_diagnostics", "window_states", "verification_requests")


def slim_event_record(record: Any) -> bool:
    """Drop per-window diagnostics from an archived event, keeping health inputs.

    Preserved: statuses, counts, receipts, ``server_result`` and the two paths
    ``server_damage_multiplier_min`` reads (``diagnostics.finish.server_result``
    and ``diagnostics.hits[].server_hit.automationDamageMultiplier``).
    Returns True when something was removed.
    """
    if not isinstance(record, dict) or record.get("slimmed"):
        return False
    result = record.get("protocol_result")
    changed = False
    if isinstance(result, dict):
        for key in HEAVY_RESULT_KEYS:
            if key in result:
                result.pop(key, None)
                changed = True
        diagnostics = result.get("diagnostics")
        if isinstance(diagnostics, dict) and "transport_diagnostics" in diagnostics:
            diagnostics.pop("transport_diagnostics", None)
            changed = True
        outcome = result.get("reference_outcome")
        if isinstance(outcome, dict) and isinstance(outcome.get("diagnostics"), dict):
            full = outcome["diagnostics"]
            finish = full.get("finish") if isinstance(full.get("finish"), dict) else {}
            hits = full.get("hits") if isinstance(full.get("hits"), list) else []
            outcome["diagnostics"] = {
                "slimmed": True,
                "finish": {"server_result": finish.get("server_result")} if finish else {},
                "hits": [{"server_hit": {"automationDamageMultiplier": hit["server_hit"].get("automationDamageMultiplier")}}
                         for hit in hits if isinstance(hit, dict) and isinstance(hit.get("server_hit"), dict)],
            }
            changed = True
    for key in ("window_states", "missed_window_records"):
        if key in record:
            record.pop(key, None)
            changed = True
    if changed:
        record["slimmed"] = True
    return changed


def event_needs_reconciliation(record: Any, *, now: float | None = None,
                               window_seconds: float | None = EVENT_RECONCILIATION_WINDOW_SECONDS) -> bool:
    """A heartbeat cannot resolve an unfinished, possibly submitted mutation.

    With ``window_seconds`` (the default) a record whose ``updated_at`` is older
    than the event lifetime is a closed, failed event rather than pending work.
    Records without a parseable ``updated_at`` stay conservative. Pass
    ``window_seconds=None`` for the ageless rule (process-restart recovery).
    """
    if not isinstance(record, dict):
        return False
    status = record.get("status")
    if not isinstance(status, str):
        pending = bool(record.get("mutation_started"))
    elif status in SETTLED_STATUSES:
        return False
    else:
        pending = (status == "unknown_mutation_outcome"
                   or bool(record.get("mutation_started")) or status in MUTATION_PHASES)
    if not pending or window_seconds is None:
        return pending
    updated = record_updated_ts(record)
    if updated is None:
        return True
    current = time.time() if now is None else float(now)
    return current - updated <= float(window_seconds)
