"""Safe classification and evidence recovery for outbound automation sends."""
from __future__ import annotations

from enum import Enum
from typing import Iterable, Mapping


class SendState(str, Enum):
    SENT = "sent"
    NOT_SENT = "not_sent"
    UNKNOWN = "unknown"


def classify_send_exception(exc: BaseException) -> SendState:
    """Classify without pretending an RPC timeout proves non-delivery."""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return SendState.UNKNOWN
    text = str(exc or "").lower()
    if "timeout" in text or "connection" in text or "incomplete" in text:
        return SendState.UNKNOWN
    return SendState.NOT_SENT


def _same_actor(sender_id: object, identity_id: int) -> bool:
    try:
        sender = int(sender_id or 0)
        identity = int(identity_id or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    return sender != 0 and identity > 0 and sender in {identity, -identity, int(f"100{identity}")}


def recover_unknown_send(
    *,
    state: SendState,
    command: str,
    identity_id: int,
    target_id: int,
    attempt_started_at: float,
    messages: Iterable[Mapping[str, object]],
    lookahead_seconds: float = 90.0,
) -> dict:
    """Recover only from a matching message fact; never infer from a reply alone."""
    if state is not SendState.UNKNOWN:
        return {"recovered": False, "reason": "state_not_unknown"}
    command = str(command or "").strip()
    try:
        started = float(attempt_started_at)
    except (TypeError, ValueError, OverflowError):
        return {"recovered": False, "reason": "attempt_time_invalid"}
    if not command or started <= 0 or int(identity_id or 0) <= 0:
        return {"recovered": False, "reason": "recovery_context_invalid"}

    candidates = []
    for raw in messages:
        item = dict(raw or {})
        try:
            ts = float(item.get("ts") or item.get("ts_epoch") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if not (started - 3 <= ts <= started + max(0.0, float(lookahead_seconds))):
            continue
        if int(item.get("chat_id") or 0) != int(target_id):
            continue
        if str(item.get("text") or item.get("message") or "").strip() != command:
            continue
        if not _same_actor(item.get("sender_id"), int(identity_id)):
            continue
        try:
            message_id = int(item.get("message_id") or item.get("id") or 0)
        except (TypeError, ValueError, OverflowError):
            message_id = 0
        if message_id > 0:
            candidates.append((ts, message_id))
    if not candidates:
        return {"recovered": False, "reason": "evidence_missing"}
    ts, message_id = max(candidates)
    return {"recovered": True, "reason": "message_log_match", "message_id": message_id, "sent_at": ts}


__all__ = ["SendState", "classify_send_exception", "recover_unknown_send"]
