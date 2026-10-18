#!/usr/bin/env python3
"""青元子 battle priority: every other game-API flow yields during the fight.

Operator rule (2026-09-20): the 青元子 world-boss battle is the highest priority
and is judged on results, so it must never compete with routine automation for
the game origin or Telegram. The battle host itself is not throttled by this
module; instead the daily/periodic flows call ``yield_to_qyz`` at the top of
their tick and skip the tick while a battle is possible or in progress.

Two signals define the window:

* clock: 13:36-13:52 Asia/Shanghai every day (official entry ~13:40, browser
  verification from ~13:38, last /finish ~13:45, report by ~13:48);
* evidence: a shared entry written by the host less than ``ACTIVE_ENTRY_SECONDS``
  ago (covers unscheduled or late openings).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .state import shanghai_now

WINDOW_START = (13, 36)
WINDOW_END = (13, 52)
ACTIVE_ENTRY_SECONDS = 12 * 60
SHARED_ENTRY = Path("qingyuanzi-new-source") / "state" / "world_boss_shared_entry.json"
STATUS = "yielding_to_qyz"


def _entry_published_ts(root: Path) -> float:
    try:
        payload = json.loads((Path(root) / SHARED_ENTRY).read_text(encoding="utf-8"))
        text = str(payload.get("published_at") or "")
    except (OSError, ValueError, AttributeError):
        return 0.0
    if not text:
        return 0.0
    from datetime import datetime
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=shanghai_now(0).tzinfo)
    return parsed.timestamp()


def qyz_battle_window(now_ts: float, root: Path | None = None) -> dict[str, Any]:
    """Return ``{"active": bool, "reason": str}`` for the given instant."""
    now = shanghai_now(now_ts)
    clock = (now.hour, now.minute)
    if WINDOW_START <= clock < WINDOW_END:
        return {"active": True, "reason": "daily_battle_clock_window"}
    if root is not None:
        published = _entry_published_ts(root)
        if published and 0 <= float(now_ts) - published <= ACTIVE_ENTRY_SECONDS:
            return {"active": True, "reason": "battle_entry_in_progress"}
    return {"active": False, "reason": ""}


def yield_to_qyz(flow: str, now_ts: float, root: Path | None = None) -> dict[str, Any] | None:
    """Tick guard: a non-None result means "skip this tick" and is the summary to print."""
    window = qyz_battle_window(now_ts, root)
    if not window["active"]:
        return None
    return {"status": STATUS, "flow": flow, "reason": window["reason"], "updated_at": float(now_ts),
            "window": f"{WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}-{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d} Asia/Shanghai"}
