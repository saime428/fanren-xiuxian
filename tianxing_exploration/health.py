"""Read-only health for the exploration owner, its receipts, and report outbox."""
from pathlib import Path

from miniapp_shared.state import read_json
from . import ACCOUNT, FLOW, PLAYER_ID, VERSION
from .protocol import day


def health(directory, account, now, *, running=False):
    state = read_json(Path(directory) / "state.json")
    if not state:
        return "warn", "exploration_not_observed", "No exploration cycle observed yet", None
    if (account != ACCOUNT or state.get("account") != ACCOUNT or state.get("actor_id") != PLAYER_ID
            or state.get("flow") != FLOW or state.get("version") != VERSION):
        return "bad", "exploration_identity_or_version_invalid", "Actor or runtime version is unverified", None
    due = state.get("next_due_at")
    if state.get("pending"):
        age = now - float(state["pending"].get("at", 0))
        if running and age < 300:
            return "ok", "exploration_in_progress", "Waiting for the current action receipt", due
        return "bad", "exploration_result_unconfirmed", "A game action has no confirmed result; no replay", due
    reports = state.get("outbox", [])
    if any(row.get("status") in {"delivery_uncertain", "sending"} and now-row.get("at", 0)>180 for row in reports):
        return "bad", "exploration_report_unconfirmed", "A report delivery requires reconciliation", due
    if any(row.get("status") == "failed" for row in reports):
        return "warn", "exploration_report_retry", "A report has not been delivered; notification retry is pending", due
    if state.get("error"):
        return "warn", "exploration_blocked", str(state["error"]), due
    if state.get("day") != day(now):
        return "warn", "exploration_new_day_pending", "Today's server quota has not been read", due
    if running:
        return "ok", "exploration_in_progress", "The account-scoped cycle is running", due
    if due and due > now:
        journey = state.get("journey") or {}
        return "ok", "exploration_waiting_deadline", f"Server journey quota {journey.get('count', '?')}/{journey.get('limit', '?')}; waiting for next due action", due
    if now-float(state.get("updated_at", 0)) < 180:
        return "ok", "exploration_dispatch_due", "Waiting for the minute dispatcher", due
    return "bad", "exploration_overdue", "An action deadline passed without a fresh cycle", due
