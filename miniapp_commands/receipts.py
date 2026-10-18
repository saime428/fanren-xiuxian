"""Separate MiniApp evidence; never manufacture Telegram message IDs or acks."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from miniapp_shared.state import read_json
from .catalog import ACCOUNTS, FLOW_COMMANDS, ORIGIN, PREFIX, RECEIPT_FILE, STATE_FILE, VERSION
from .protocol import number


def receipt(reply, *, account, flow, operation, now, status, succeeded=False, request_id="", text=""):
    from miniapp_shared.dwelling import safe_text
    route = "deep-seclusion" if flow == "shendubiguan" else "small-world" if flow == "shenji_budao" else "command-center"
    if operation in {"overview", "details"}:
        route = operation
    projection = {"account": account, "flow": flow, "operation": operation,
                  "server_account": (reply.get("account") or {}).get("playerId"),
                  "status": status, "succeeded": bool(succeeded), "message": safe_text(text, 1200)}
    return {"transport": "miniapp", "runtime_version": VERSION, "api_origin": ORIGIN,
            "endpoint": PREFIX+route, "account": account, "player_id": int(account[2:]),
            "flow": flow, "command": FLOW_COMMANDS[flow], "operation": operation,
            "server_ok": reply.get("ok") is True, "confirmed": True, "status": status,
            "succeeded": bool(succeeded), "received_at": now, "request_id": request_id,
            "message": projection["message"],
            "response_digest": hashlib.sha256(json.dumps(projection, sort_keys=True).encode()).hexdigest()}


def valid_receipt(row, account, flow):
    return (isinstance(row, dict) and account in ACCOUNTS.get(flow, ())
            and row.get("transport") == "miniapp" and row.get("runtime_version") == VERSION
            and row.get("api_origin") == ORIGIN
            and row.get("endpoint") in {PREFIX+p for p in ("overview", "details", "deep-seclusion", "command-center", "small-world")}
            and row.get("account") == account and type(row.get("player_id")) is int
            and row["player_id"] == int(account[2:]) and row.get("flow") == flow
            and row.get("command") == FLOW_COMMANDS[flow]
            and row.get("server_ok") is True and row.get("confirmed") is True
            and (number(row.get("received_at")) or 0) > 0
            and re.fullmatch(r"[a-f0-9]{64}", str(row.get("response_digest", ""))) is not None)


def health(directory: Path, account: str, flow: str, now: float):
    """Return health from the new writer, including unresolved outcomes."""
    state = read_json(Path(directory)/STATE_FILE)
    if not state:
        return "warn", "miniapp_not_observed", "Waiting for the first MiniApp cycle", None
    if (state.get("account") != account or state.get("flow") != flow
            or state.get("runtime_version") != VERSION):
        return "bad", "miniapp_state_invalid", "MiniApp state identity/version mismatch", None
    status = state.get("status", "unknown")
    due = number(state.get("next_due_ts"))
    if state.get("pending") or status in {"unknown_outcome", "request_failed", "unconfirmed"}:
        return "warn", status, "MiniApp outcome requires reconciliation; no group fallback", due
    if status in {"rejected", "unavailable", "state_invalid", "configuration_error"}:
        return "bad", status, "MiniApp request/state was rejected", due
    if status == "legacy_wait":
        from command_confirmation import valid_receipt_identity
        old = read_json(Path(directory)/"last_result.json")
        if (valid_receipt_identity(old, actor_id=int(account[2:]), flow=flow, command=FLOW_COMMANDS[flow])
                and number(old.get("next_due_ts")) == due and due and due > now):
            return "ok", status, "Existing official cooldown retained until the first MiniApp action", due
        return "warn", status, "First MiniApp cycle is due", due
    row = read_json(Path(directory)/RECEIPT_FILE)
    if not valid_receipt(row, account, flow):
        return "bad", "miniapp_receipt_invalid", "No account-matched MiniApp result", due
    if status == "settled_failure":
        return "warn", status, "Official settlement was a failure; the cooldown is retained", due
    if status in {"yielding_to_qyz", "resource_wait"}:
        return "warn", status, "MiniApp is waiting for battle priority or resources", due
    if due and now <= due + 180:
        return "ok", status, "Account-matched MiniApp result and server/cadence deadline", due
    return "warn", "miniapp_overdue", "MiniApp cycle is due; the existing sender owns it", due
