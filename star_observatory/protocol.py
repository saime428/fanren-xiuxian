#!/usr/bin/env python3
"""Pure parsing and decision logic for the 星宫灵圃 (sect star farm) MiniApp.

Upstream contract (references/upstream-fanrenxiuxian-auto/miniapp_dwelling.py and
references/xianxia-companion .../biz_xinggong_miniapp.py):

* ``POST /api/miniapp/xianxia-sect-farm/start`` returns ``domain`` with
  ``mode == "stars"`` and ``plots`` (each: ``key``, ``empty``, ``name``,
  ``status``/``statusLabel``, ``remainingSeconds``/``remainingText``).
* ``POST /api/miniapp/xianxia-sect-farm/action`` accepts ``action`` in
  ``soothe`` (一键安抚, ``plotKey`` ""), ``collect`` (一键收集, ``plotKey`` "")
  or ``pull`` (牵引星辰, ``plotKey`` + ``starName``), and returns
  ``actionResult`` plus the refreshed ``domain``.
* ``nothing_to_soothe`` / ``nothing_ready`` are harmless no-op rejections.
"""
from __future__ import annotations

import re
from typing import Any

STAR_NAME = "天雷星"
BAD_STATUSES = frozenset({"星光黯淡", "元磁紊乱"})
READY_STATUSES = frozenset({"可收集", "精华已成", "精华已成 · 待收集"})
IDLE_STATUSES = frozenset({"空闲", "星轨已校准", "READY"})
BENIGN_ACTION_ERRORS = {
    "soothe": frozenset({"nothing_to_soothe"}),
    "collect": frozenset({"nothing_ready"}),
    "pull": frozenset(),
}
_DURATION = re.compile(
    r"(?:(?P<day>\d+)\s*天)?\s*(?:(?P<hour>\d+)\s*小时)?\s*(?:(?P<minute>\d+)\s*分)?\s*(?:(?P<second>\d+)\s*秒)?"
)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _remaining_seconds(item: dict[str, Any]) -> int:
    for key in ("remainingSeconds", "remainingSec", "remainSeconds", "remaining_seconds", "remaining"):
        try:
            seconds = int(float(item.get(key)))
        except (TypeError, ValueError, OverflowError):
            continue
        if seconds > 0:
            return seconds
    for key in ("remainingText", "statusLabel", "status"):
        text = str(item.get(key) or "")
        for match in _DURATION.finditer(text):
            if not any(match.groupdict().values()):
                continue
            seconds = (
                int(match.group("day") or 0) * 86400
                + int(match.group("hour") or 0) * 3600
                + int(match.group("minute") or 0) * 60
                + int(match.group("second") or 0)
            )
            if seconds > 0:
                return seconds
    return 0


def _domain(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    domain = data.get("domain") if isinstance(data.get("domain"), dict) else {}
    if not domain and isinstance(data.get("data"), dict):
        nested = data["data"].get("domain")
        domain = nested if isinstance(nested, dict) else {}
    return domain


def parse_farm_state(data: Any) -> dict[str, Any]:
    """Normalize a ``/start`` or ``/action`` payload into plot facts."""
    domain = _domain(data)
    if domain.get("mode") != "stars":
        return {}
    raw = domain.get("plots")
    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(raw, dict):
        entries = [(str(key), value) for key, value in raw.items() if isinstance(value, dict)]
    elif isinstance(raw, list):
        entries = [(str(item.get("key") or item.get("plotKey") or index), item)
                   for index, item in enumerate(raw, start=1) if isinstance(item, dict)]
    plots: list[dict[str, Any]] = []
    for key, item in entries:
        status = str(item.get("status") or item.get("statusLabel") or "").strip()
        name = str(item.get("name") or item.get("starName") or item.get("star_name") or "").strip()
        empty = _as_bool(item.get("empty")) or (not name and (status in IDLE_STATUSES or not status))
        plots.append({
            "key": key.strip(),
            "name": name,
            "status": status,
            "empty": empty,
            "remaining_sec": 0 if empty else _remaining_seconds(item),
            "bad": (not empty) and (status in BAD_STATUSES or "需安抚" in status),
            "ready": (not empty) and (status in READY_STATUSES or "待收集" in status),
        })
    if not plots:
        return {}
    waits = [plot["remaining_sec"] for plot in plots if plot["remaining_sec"] > 0]
    return {
        "mode": "stars",
        "sect_name": str(domain.get("sectName") or "").strip(),
        "plots": plots,
        "bad_keys": [plot["key"] for plot in plots if plot["bad"]],
        "ready_keys": [plot["key"] for plot in plots if plot["ready"]],
        "empty_keys": [plot["key"] for plot in plots if plot["empty"]],
        "busy_keys": [plot["key"] for plot in plots
                      if not plot["empty"] and not plot["ready"] and plot["remaining_sec"] > 0],
        "max_wait": max(waits) if waits else 0,
        "min_wait": min(waits) if waits else 0,
    }


def safe_plot_snapshot(state: Any) -> list[dict[str, Any]]:
    return [
        {"key": str(plot.get("key") or ""), "empty": bool(plot.get("empty")),
         "name": str(plot.get("name") or ""), "status": str(plot.get("status") or "")}
        for plot in list((state or {}).get("plots") or ())
    ]


def choose_action(state: Any, *, star_name: str = STAR_NAME, skip: Any = frozenset()) -> dict[str, Any]:
    """Pick the next single server action, mirroring the official client order.

    1. 一键安抚 whenever any occupied plot is 星光黯淡 / 元磁紊乱.
    2. 一键收集 whenever any plot is 可收集 / 精华已成.
    3. 牵引星辰 <star> into every empty plot.
    4. Otherwise wait for the earliest maturing plot (server timer).

    ``skip`` lists actions the server already answered with a harmless no-op
    during this cycle; they are not proposed again.
    """
    state = state or {}
    skip = set(skip or ())
    if not state.get("plots"):
        return {"action": "inspect", "plotKey": "", "reason": "no_plots"}
    if state.get("bad_keys") and "soothe" not in skip:
        return {"action": "soothe", "plotKey": "", "reason": "bad_plots", "keys": list(state["bad_keys"])}
    if state.get("ready_keys") and "collect" not in skip:
        return {"action": "collect", "plotKey": "", "reason": "ready_plots", "keys": list(state["ready_keys"])}
    if state.get("empty_keys") and "pull" not in skip:
        return {"action": "pull", "plotKey": str(state["empty_keys"][0]), "starName": star_name,
                "reason": "empty_plot"}
    wait = int(state.get("min_wait") or 0)
    if wait > 0:
        return {"action": "wait", "plotKey": "", "wait_sec": wait, "reason": "maturing"}
    return {"action": "inspect", "plotKey": "", "reason": "unknown"}


def action_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get("actionResult")
    if not isinstance(value, dict) and isinstance(payload.get("data"), dict):
        value = payload["data"].get("actionResult")
    return value if isinstance(value, dict) else {}


def classify_action(payload: Any, action: str) -> dict[str, Any]:
    """Return ``{"ok", "benign", "error", "message"}`` for one mutation reply."""
    result = action_result(payload)
    error = str(result.get("error") or "").strip().lower()
    message = str(result.get("rawMessage") or result.get("message") or "").strip()
    if isinstance(payload, dict) and payload.get("ok") is False and not result:
        error = str(payload.get("error") or "request_rejected").strip().lower()
    if result.get("ok") is False or (not result and error):
        benign = error in BENIGN_ACTION_ERRORS.get(action, frozenset())
        return {"ok": benign, "benign": benign, "error": error or "action_failed", "message": message}
    if not result and not _domain(payload):
        return {"ok": False, "benign": False, "error": "action_result_missing", "message": message}
    return {"ok": True, "benign": False, "error": "", "message": message}
