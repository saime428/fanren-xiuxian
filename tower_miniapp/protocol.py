#!/usr/bin/env python3
"""Pure parsing for the 琉璃问心塔 child MiniApp (``xianxia-pagoda``).

Upstream contract (references/upstream-fanrenxiuxian-auto/miniapp_daily_activities.py
and references/xianxia-companion .../biz_pagoda_miniapp.py):

* ``POST /api/miniapp/xianxia-pagoda/start`` → ``state`` with ``canChallenge``,
  ``todayHighest``, ``failedFloor``, ``resetsToday``, ``recordHighest``, ``towerMarks``.
* ``POST /api/miniapp/xianxia-pagoda/challenge`` → ``replay`` (``clearedCount``,
  ``startFloor``, ``endFloor``, ``failedFloor``, ``report``) plus refreshed ``state``.
* The server records today's attempt when ``todayHighest``/``failedFloor``/
  ``resetsToday`` is non-zero; ``canChallenge`` false means no further attempt.
"""
from __future__ import annotations

from typing import Any

from miniapp_shared.dwelling import safe_text


def _as_int(value: Any) -> int:
    try:
        return int(float(str(value if value is not None else 0).replace(",", "")))
    except (TypeError, ValueError, OverflowError):
        return 0


def _root(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return data.get("data") if isinstance(data.get("data"), dict) else data


def parse_state(data: Any) -> dict[str, Any]:
    root = _root(data)
    raw = root.get("state") if isinstance(root.get("state"), dict) else {}
    if not raw or "canChallenge" not in raw:
        return {}
    return {
        "dao_name": safe_text(raw.get("daoName"), 40),
        "level": safe_text(raw.get("level"), 40),
        "power": _as_int(raw.get("power")),
        "today_highest": _as_int(raw.get("todayHighest")),
        "record_highest": _as_int(raw.get("recordHighest")),
        "failed_floor": _as_int(raw.get("failedFloor")),
        "resets_today": _as_int(raw.get("resetsToday")),
        "tower_marks": _as_int(raw.get("towerMarks")),
        "can_challenge": bool(raw.get("canChallenge")),
    }


def attempted_today(state: Any) -> bool:
    """The upstream client treats any of these counters as 'already climbed today'."""
    state = state or {}
    return bool(state.get("today_highest", 0) > 0 or state.get("failed_floor", 0) > 0
                or state.get("resets_today", 0) > 0)


def parse_replay(data: Any) -> dict[str, Any]:
    root = _root(data)
    raw = root.get("replay") if isinstance(root.get("replay"), dict) else {}
    if not raw:
        return {}
    return {
        "start_floor": _as_int(raw.get("startFloor")),
        "end_floor": _as_int(raw.get("endFloor")),
        "failed_floor": _as_int(raw.get("failedFloor")),
        "cleared_count": _as_int(raw.get("clearedCount")),
        "record_broken": bool(raw.get("recordBroken")),
        "report": safe_text(raw.get("report"), 1200),
    }


def classify_challenge_response(data: Any) -> dict[str, Any]:
    state = parse_state(data)
    replay = parse_replay(data)
    has_oracle = bool(replay and (replay["cleared_count"] > 0 or replay["end_floor"] > 0
                                  or replay["failed_floor"] > 0 or replay["report"]))
    if state and has_oracle and (not state["can_challenge"] or attempted_today(state)):
        status = "settled_confirmed"
    elif has_oracle and not state:
        status = "settled_state_missing"
    else:
        status = "oracle_missing"
    return {"status": status, "state": state, "replay": replay}


def result_text(replay: Any, state: Any = None) -> str:
    replay = replay or {}
    if not replay:
        return "服务器已记录今日登塔" if attempted_today(state) else ""
    text = f"通过 {replay.get('cleared_count', 0)} 层，抵达第 {replay.get('end_floor', 0)} 层"
    if replay.get("failed_floor", 0) > 0:
        text += f"，止步第 {replay['failed_floor']} 层"
    if replay.get("record_broken"):
        text += "，刷新纪录"
    return text
