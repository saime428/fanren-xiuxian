#!/usr/bin/env python3
"""Pure parsing for the dwelling 游历 tab (野外历练 / wild experience).

Upstream contract (references/upstream-fanrenxiuxian-auto/miniapp_journey.py,
references/upstream-xiuxian-mian/model/features/cave_treasure_miniapp.py and
references/xianxia-companion .../biz_wild_experience_miniapp.py):

* State lives at ``account.journey.wildExperience`` in ``dwelling/start`` and
  ``dwelling/details``: ``available``, ``dailyCount``, ``dailyLimit``,
  ``dailyRemaining``, ``remainingSeconds``, ``readyAt`` (ms), ``resetAt`` (ms),
  ``modes`` (``cautious`` = 谨慎, ``balanced`` = 均衡, ``deep`` = 深入).
* One attempt: ``POST /api/miniapp/xianxia-dwelling/journey`` with
  ``{"action": "wild_experience", "mode": "cautious", "playerId": ...}``; the reply
  carries ``actionResult`` (``type == "wild_experience"``, ``completed``,
  ``handled``, ``outcome``, ``dailyCount``) plus the refreshed account state.
* Server error codes: ``wild_experience_daily_limit``,
  ``wild_experience_unavailable`` (cooldown), ``wild_experience_failed``.
"""
from __future__ import annotations

from typing import Any

from miniapp_shared.dwelling import safe_text

MODE = "cautious"
MODE_LABEL = "谨慎"
TARGET_DAILY_COUNT = 8
COOLDOWN_ERRORS = frozenset({"wild_experience_unavailable", "wild_experience_cooldown"})
LIMIT_ERRORS = frozenset({"wild_experience_daily_limit"})


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value if value is not None else default).replace(",", "")))
    except (TypeError, ValueError, OverflowError):
        return default


def _seconds(value: Any) -> float:
    number = float(_int(value, 0))
    return number / 1000.0 if number > 10_000_000_000 else number


def _wild_raw(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    root = data.get("data") if isinstance(data.get("data"), dict) else data
    parents = [root.get("account"), root.get("overview"), root.get("state"), root]
    for parent in parents:
        if not isinstance(parent, dict):
            continue
        journey = parent.get("journey") if isinstance(parent.get("journey"), dict) else {}
        wild = journey.get("wildExperience") or journey.get("wild_experience") or parent.get("wildExperience")
        if isinstance(wild, dict) and wild:
            return wild
    return {}


def parse_wild_state(data: Any, *, now_ts: float | None = None) -> dict[str, Any]:
    raw = _wild_raw(data)
    if not raw:
        return {}
    count = _int(raw.get("dailyCount", raw.get("daily_count", raw.get("used"))), 0)
    limit = _int(raw.get("dailyLimit", raw.get("daily_limit", raw.get("limit"))), 0)
    remaining_value = raw.get("dailyRemaining", raw.get("remainingAttempts", raw.get("daily_remaining",
                                                                                 raw.get("remaining"))))
    remaining = _int(remaining_value, max(0, limit - count)) if remaining_value is not None else max(0, limit - count)
    remaining_seconds = max(0, _int(raw.get("remainingSeconds", raw.get("remaining_seconds")), 0))
    ready_at = _seconds(raw.get("readyAt", raw.get("ready_at")))
    if remaining_seconds <= 0 and ready_at > 0 and now_ts is not None:
        remaining_seconds = max(0, int(ready_at - float(now_ts)))
    available_value = raw.get("available")
    available = bool(available_value) if available_value is not None else (remaining > 0 and remaining_seconds <= 0)
    if remaining <= 0:
        available = False
    modes = []
    for item in raw.get("modes") if isinstance(raw.get("modes"), list) else ():
        if isinstance(item, dict):
            modes.append({"key": safe_text(item.get("key"), 20), "label": safe_text(item.get("label"), 20)})
    return {
        "present": True,
        "available": available,
        "daily_count": max(0, count),
        "daily_limit": max(0, limit),
        "daily_remaining": max(0, remaining),
        "remaining_seconds": remaining_seconds,
        "ready_at": ready_at,
        "reset_at": _seconds(raw.get("resetAt", raw.get("reset_at"))),
        "modes": modes,
    }


def action_payload(session_payload: dict[str, Any], player_id: int, *, mode: str = MODE) -> dict[str, Any]:
    if mode != MODE:
        raise ValueError("mode_not_allowed")
    return {**session_payload, "playerId": int(player_id), "action": "wild_experience", "mode": mode}


def action_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    root = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    value = root.get("actionResult") or root.get("action_result")
    return value if isinstance(value, dict) else {}


def classify_attempt(payload: Any, *, before_count: int, now_ts: float | None = None) -> dict[str, Any]:
    """Judge one attempt purely from server evidence.

    Confirmed when the refreshed counter or the action result shows exactly one
    more completed attempt than before, and the action result (if present) is
    not a rejection. Anything else is unconfirmed and must not be counted.
    """
    result = action_result(payload)
    after = parse_wild_state(payload, now_ts=now_ts)
    error = str(result.get("error") or "").strip().lower()
    if result.get("ok") is False or (isinstance(payload, dict) and payload.get("ok") is False and not result):
        code = error or str((payload or {}).get("error") or "wild_experience_failed").strip().lower()
        return {"status": "rejected", "error": code, "state": after}
    result_type = str(result.get("type") or "").strip()
    if result and result_type and result_type != "wild_experience":
        return {"status": "unconfirmed", "error": "action_type_mismatch", "state": after}
    if result and (result.get("completed") is False or result.get("handled") is False):
        return {"status": "unconfirmed", "error": "action_not_completed", "state": after}
    counts = []
    if after:
        counts.append(after["daily_count"])
    if result.get("dailyCount") is not None:
        counts.append(_int(result.get("dailyCount"), -1))
    if counts and all(count == before_count + 1 for count in counts):
        mode = result.get("mode") if isinstance(result.get("mode"), dict) else {}
        return {
            "status": "confirmed", "error": "", "state": after,
            "outcome": safe_text(result.get("outcome"), 20),
            "mode": safe_text(mode.get("key") or mode.get("label"), 20),
            "cultivation_delta": _int(result.get("cultivationDelta"), 0),
            "title": safe_text(result.get("title"), 60),
        }
    if counts and any(count > before_count + 1 for count in counts):
        return {"status": "unconfirmed", "error": "counter_jumped", "state": after}
    return {"status": "unconfirmed", "error": "counter_not_advanced", "state": after}
