#!/usr/bin/env python3
"""天机命脉 (dwelling 外府 → 游历杂务 → ``fate_cards``) and the 噬金虫进化 run it may require.

Official flow, parsed from ``/miniapp/xianxia-fate-cards`` (2026-09-26):
``start`` → ``draw{questionKey}`` (三牌阵, 天机残痕 +1) → ``interpret`` →
``choose{choiceKey}`` (irreversible) → finish the quest → ``settle``. One per
identity per day. Choices and rewards come from the server's ``start.choices``:
``accept`` 顺势承命 (2, "今日获得 30 点修为"), ``defy`` 逆势改命 (4, one 噬金虫
challenge), ``hide`` 藏锋避劫 (1, wait three minutes).

噬金虫进化 (``/miniapp/xianxia-beast-merge``) is simulated by the server:
``run/start`` → ``move{runToken, column, seq}`` → ``submit{runToken}``; five
runs a day; ``run_too_fast`` rejects a run played faster than the server allows.
"""
from __future__ import annotations

from typing import Any

FATE_PREFIX = "/api/miniapp/xianxia-fate-cards/"
BEAST_PREFIX = "/api/miniapp/xianxia-beast-merge/"
FATE_WEB_PATH = "/miniapp/xianxia-fate-cards"
BEAST_WEB_PATH = "/miniapp/xianxia-beast-merge"
FATE_ACTION = "fate_cards"
BEAST_ACTION = "beast_merge"
FATE_TOKEN_PREFIXES = ("fate_", "fate-")
BEAST_TOKEN_PREFIXES = ("beastmerge_", "beastmerge-")

FATE_PATHS = {  # path -> mutates game state
    FATE_PREFIX + "start": False,
    FATE_PREFIX + "draw": True,
    FATE_PREFIX + "interpret": True,
    FATE_PREFIX + "choose": True,
    FATE_PREFIX + "settle": True,
}
BEAST_PATHS = {
    BEAST_PREFIX + "start": False,
    BEAST_PREFIX + "run/start": True,
    BEAST_PREFIX + "move": True,
    BEAST_PREFIX + "submit": True,
}

CHOICE_LABELS = {"accept": "顺势承命", "defy": "逆势改命", "hide": "藏锋避劫"}
DEFAULT_QUESTION = "cultivation"

# 噬金虫进化 board rules, identical to the official page's local simulation.
COLS, ROWS, MAX_TIER, MAX_MOVES = 5, 7, 9, 160
SIZE = COLS * ROWS
# Fixed pause between moves. The server rejects runs that are played too fast
# (``run_too_fast``); this is the server's pace rule, applied as a constant.
MOVE_INTERVAL_SECONDS = 0.8


def gravity(values: list[int]) -> list[int]:
    result = [0] * SIZE
    for column in range(COLS):
        stack = [values[row * COLS + column] for row in range(ROWS - 1, -1, -1) if values[row * COLS + column] > 0]
        for depth, value in enumerate(stack):
            result[(ROWS - 1 - depth) * COLS + column] = value
    return result


def find_pair(values: list[int]) -> tuple[int, int, int] | None:
    for row in range(ROWS - 1, -1, -1):
        for column in range(COLS):
            index = row * COLS + column
            value = values[index]
            if not value or value >= MAX_TIER:
                continue
            if column + 1 < COLS and values[index + 1] == value:
                return index, index + 1, value
            if row > 0 and values[index - COLS] == value:
                return index, index - COLS, value
    return None


def apply_move(values: list[int], column: int, piece: int) -> dict[str, Any]:
    board = list(values)
    landed = False
    for row in range(ROWS - 1, -1, -1):
        index = row * COLS + column
        if not board[index]:
            board[index] = piece
            landed = True
            break
    if not landed:
        return {"accepted": False, "board": board, "gained": 0, "merges": 0}
    gained = merges = 0
    while True:
        pair = find_pair(board)
        if pair is None:
            break
        first, second, value = pair
        board[first] = min(MAX_TIER, value + 1)
        board[second] = 0
        board = gravity(board)
        merges += 1
        gained += 10 * (2 ** min(12, value)) + max(0, merges - 1) * 5
    return {"accepted": True, "board": board, "gained": gained, "merges": merges}


def choose_column(board: list[int], piece: int) -> int | None:
    """Deterministic greedy column: most points, then most free cells, then lowest stack."""
    best, best_key = None, None
    for column in range(COLS):
        outcome = apply_move(board, column, piece)
        if not outcome["accepted"]:
            continue
        after = outcome["board"]
        free = sum(1 for value in after if not value)
        height = sum(1 for row in range(ROWS) if board[row * COLS + column])
        key = (outcome["gained"], free, -height, -column)
        if best_key is None or key > best_key:
            best, best_key = column, key
    return best


def board_full(board: list[int]) -> bool:
    return all(board[column] for column in range(COLS))


def parse_board(values: Any) -> list[int] | None:
    """The server board as 35 tiers (``None`` cells are empty), or ``None`` when its shape changed."""
    if not isinstance(values, list) or len(values) != SIZE:
        return None
    if any(value is not None and (type(value) is not int or not 0 <= value <= MAX_TIER) for value in values):
        return None
    return [value or 0 for value in values]


def choice_rewards(start: dict[str, Any]) -> dict[str, int]:
    rewards = {}
    for choice in start.get("choices") or []:
        if isinstance(choice, dict) and choice.get("key") in CHOICE_LABELS:
            try:
                rewards[choice["key"]] = int(choice.get("rewardTrace") or 0)
            except (TypeError, ValueError):
                rewards[choice["key"]] = 0
    return rewards


def quest_view(record: Any) -> dict[str, Any]:
    quest = (record or {}).get("quest") if isinstance(record, dict) else None
    if not isinstance(quest, dict):
        return {}
    keep = ("choiceKey", "key", "metric", "title", "description", "progress", "target", "unit",
            "status", "canSettle", "startedAt", "expiresAt", "reward")
    return {key: quest.get(key) for key in keep if key in quest}
