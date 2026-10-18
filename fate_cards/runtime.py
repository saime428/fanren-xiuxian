#!/usr/bin/env python3
"""One identity's daily 天机命脉: draw, interpret, choose, finish the quest, settle.

Choice policy (reasoned from the server's own ``start.choices`` on 2026-09-26):
逆势改命 ``defy`` pays 4 天机残痕 and needs one 噬金虫 challenge, which the
server simulates and this flow can play; 顺势承命 ``accept`` pays 2 and its
"今日获得 30 点修为" quest was already complete right after drawing on every
account; 藏锋避劫 ``hide`` pays 1. A choice cannot be changed, and the defy
quest's exact metric is only visible after choosing, so ``defy`` starts on one
canary account; once a defy quest settles every account uses it, and if it
cannot be met the fleet stays on ``accept``.

噬金虫进化 (operator 2026-09-26: "噬金虫要打满"): after the identity's choice,
every run the server still allows today is played (``attempts.limit -
attempts.used``, five a day), so a 逆势改命 quest always gets them first. Only
the server's attempts counter decides how many runs are left.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable

from miniapp_shared.accounts import accounts_for, feature_options
from miniapp_shared.dwelling import MiniAppError, error_code, is_uncertain, safe_text
from miniapp_shared.priority import qyz_battle_window
from miniapp_shared.state import read_json, write_json

from .protocol import (
    DEFAULT_QUESTION, MAX_MOVES, MOVE_INTERVAL_SECONDS, SIZE, board_full, choice_rewards, choose_column, parse_board,
    quest_view,
)

# The fate_cards account with "canary": true (else the first) tries 逆势改命 first.
CANARY_ACCOUNT = next((account for account in accounts_for("fate_cards")
                       if feature_options(account, "fate_cards").get("canary") is True),
                      next(iter(accounts_for("fate_cards")), None))
HIDE_WAIT_LIMIT_SECONDS = 240
# One 160-move run at the fixed pace takes about five minutes. A run is never
# started if it could still be going inside the 青元子 window.
BEAST_RUN_BUDGET_SECONDS = 6 * 60
# A lost move/submit reply is retried as the same request only (same seq or
# runToken), never as a new run; 2026-09-26 @demo_player_04 lost a played run
# when one move and its submit both hit a network blip.
SAME_REQUEST_RETRY_SECONDS = (2.0, 5.0, 15.0)
# ``run/start`` spends one of the day's runs before the board is seen. A board
# whose shape no longer matches the 5x7 rules would burn a run on every retry
# tick, so such a run is logged as halted and no further run starts that day.
BOARD_INVALID = "beast_board_invalid"


def policy_path(root: Path) -> Path:
    return Path(root) / "state" / "fate_cards" / "policy.json"


def read_policy(root: Path) -> dict[str, Any]:
    policy = read_json(policy_path(root))
    if policy.get("defy") not in {"canary", "verified", "failed"}:
        policy = {"defy": "canary", "canary_account": CANARY_ACCOUNT}
    return policy


def update_policy(root: Path, **fields: Any) -> dict[str, Any]:
    policy = {**read_policy(root), **fields, "updated_at": time.time()}
    write_json(policy_path(root), policy)
    return policy


def decide_choice(account: str, policy: dict[str, Any], rewards: dict[str, int], beast_runs_left: int) -> str:
    defy_allowed = (policy.get("defy") == "verified"
                    or (policy.get("defy") == "canary" and account == policy.get("canary_account", CANARY_ACCOUNT)))
    if "defy" in rewards and defy_allowed and beast_runs_left > 0:
        return "defy"
    if "accept" in rewards:
        return "accept"
    return max(rewards, key=rewards.get) if rewards else "accept"


async def _same_request(client: Any, endpoint: str, *, sleep: Callable[[float], Any], **fields: Any) -> dict[str, Any]:
    for delay in (*SAME_REQUEST_RETRY_SECONDS, None):
        try:
            return await client.beast(endpoint, **fields)
        except MiniAppError as exc:
            if delay is None or not is_uncertain(exc):
                raise
            await sleep(delay)
    raise AssertionError("unreachable")


async def play_beast_run(client: Any, *, sleep: Callable[[float], Any] = asyncio.sleep) -> dict[str, Any]:
    """Play one full 噬金虫 run with the deterministic greedy policy, then submit it."""
    run = await client.beast("run/start")
    token = str(run.get("runToken") or "")
    if not token:
        raise MiniAppError("beast_run_token_missing")
    board = parse_board(run.get("board") or [0] * SIZE)
    if board is None:
        return {"moves": 0, "halted": BOARD_INVALID}
    piece, seq, moves = int(run.get("nextPiece") or 1), int(run.get("seq") or 0), 0
    move_error = ""
    while moves < MAX_MOVES and not board_full(board):
        column = choose_column(board, piece)
        if column is None:
            break
        await sleep(MOVE_INTERVAL_SECONDS)
        try:
            reply = await _same_request(client, "move", sleep=sleep, runToken=token, column=column, seq=seq)
        except MiniAppError as exc:
            # The server owns the board; stop and let submit verify what it has.
            move_error = exc.code
            break
        moves += 1
        board = parse_board(reply.get("board") or board)
        if board is None:
            move_error = BOARD_INVALID  # the server owns the run: still submit what it has
            break
        piece, seq = int(reply.get("nextPiece") or piece), int(reply.get("seq") or seq)
        if reply.get("gameOver"):
            break
    submitted = await _same_request(client, "submit", sleep=sleep, runToken=token)
    verified = submitted.get("verified") if isinstance(submitted.get("verified"), dict) else {}
    reward = submitted.get("reward") if isinstance(submitted.get("reward"), dict) else {}
    return {"moves": moves, "score": verified.get("score"), "max_tier": verified.get("maxTier"),
            "trace": reward.get("tianjiTrace"), **({"move_error": move_error} if move_error else {}),
            **({"halted": BOARD_INVALID} if move_error == BOARD_INVALID else {})}


async def beast_attempts(client: Any) -> tuple[int, int]:
    """Today's ``(used, limit)`` 噬金虫 runs from the server's ``start``."""
    attempts = (await client.beast("start")).get("attempts") or {}
    return int(attempts.get("used") or 0), int(attempts.get("limit") or 0)


def beast_runs_today(root: Path, account: str, player_id: int, ts: float) -> list[dict[str, Any]]:
    log = read_json(_beast_log_path(root, account, player_id))
    return list(log.get("runs") or []) if log.get("day") == _day(ts) else []


def beast_halted(root: Path, account: str, player_id: int, ts: float) -> bool:
    return any(run.get("halted") for run in beast_runs_today(root, account, player_id, ts))


def _qyz_near(root: Path, ts: float) -> bool:
    return any(qyz_battle_window(point, root)["active"] for point in (ts, ts + BEAST_RUN_BUDGET_SECONDS))


async def _logged_run(root: Path, account: str, client: Any, purpose: str, *, now: Callable[[], float],
                      sleep: Callable[[float], Any]) -> dict[str, Any]:
    run = {**await play_beast_run(client, sleep=sleep), "purpose": purpose, "at": now()}
    runs = beast_runs_today(root, account, client.player_id, now()) + [run]
    write_json(_beast_log_path(root, account, client.player_id), {"day": _day(now()), "runs": runs})
    return run


async def fill_beast_runs(root: Path, account: str, client: Any, *, now: Callable[[], float] = time.time,
                          sleep: Callable[[float], Any] = asyncio.sleep) -> dict[str, Any]:
    """Play every 噬金虫 run the server still allows today; yields to 青元子 between runs."""
    used, limit = await beast_attempts(client)
    status = "full"
    halted = beast_halted(root, account, client.player_id, now())
    for _ in range(max(0, limit - used)):
        if halted:
            break
        if _qyz_near(root, now()):
            status = "yielding_to_qyz"
            break
        halted = bool((await _logged_run(root, account, client, "fill", now=now, sleep=sleep)).get("halted"))
    used, limit = await beast_attempts(client)
    if halted:
        status = "halted"
    elif limit <= 0:
        status = "unavailable"
    elif used < limit and status == "full":
        status = "pending"
    return {"status": status, "used": used, "limit": limit,
            "runs": beast_runs_today(root, account, client.player_id, now())}


async def _start(client: Any) -> dict[str, Any]:
    return await client.fate("start")


async def run_identity(root: Path, account: str, client: Any, *, now: Callable[[], float] = time.time,
                       sleep: Callable[[float], Any] = asyncio.sleep) -> dict[str, Any]:
    result: dict[str, Any] = {"player_id": client.player_id}
    start = await _start(client)
    rewards = choice_rewards(start)
    record = start.get("record") if start.get("hasDrawn") and isinstance(start.get("record"), dict) else None
    if record is None:
        drawn = await client.fate("draw", questionKey=DEFAULT_QUESTION)
        record = drawn.get("record") if isinstance(drawn.get("record"), dict) else {}
        result["drew"] = True
    if not (record.get("aiReading") or {}).get("overview"):
        try:
            interpreted = await client.fate("interpret")
            record = interpreted.get("record") if isinstance(interpreted.get("record"), dict) else record
        except MiniAppError as exc:
            result["interpret_error"] = exc.code  # the page falls back to a fixed reading too
    if not record.get("choiceKey"):
        beast_left = 0
        if "defy" in rewards:
            try:
                attempts = (await client.beast("start")).get("attempts") or {}
                beast_left = int(attempts.get("limit") or 0) - int(attempts.get("used") or 0)
            except MiniAppError:
                beast_left = 0
        choice = decide_choice(account, read_policy(root), rewards, beast_left)
        try:
            chosen = await client.fate("choose", choiceKey=choice)
            record = chosen.get("record") if isinstance(chosen.get("record"), dict) else record
        except MiniAppError as exc:
            if not is_uncertain(exc):
                raise
            # Never choose twice: the server's record decides what was taken.
            record = (await _start(client)).get("record") or {}
            if not record.get("choiceKey"):
                raise
        result["chose"] = record.get("choiceKey")
    quest = quest_view(record)
    result.update(choice=record.get("choiceKey"), question=record.get("questionKey"), quest=quest)
    if quest.get("status") == "settled":
        return {**result, "status": "already_settled"}

    if not quest.get("canSettle") and record.get("choiceKey") == "hide" and quest.get("metric") == "wait_seconds":
        wait = max(0.0, float(quest.get("target") or 180) - float(quest.get("progress") or 0)) + 3
        if wait <= HIDE_WAIT_LIMIT_SECONDS:
            await sleep(wait)
            record = (await _start(client)).get("record") or record
            quest = quest_view(record)

    if not quest.get("canSettle") and record.get("choiceKey") == "defy":
        # Today's runs are all played anyway, so the quest may use every one.
        runs = result.setdefault("beast_runs", [])
        used, limit = await beast_attempts(client)
        left = max(0, limit - used)
        while (left and not quest.get("canSettle") and not _qyz_near(root, now())
               and not beast_halted(root, account, client.player_id, now())):
            runs.append(await _logged_run(root, account, client, "defy", now=now, sleep=sleep))
            left -= 1
            record = (await _start(client)).get("record") or record
            quest = quest_view(record)
        result["quest"] = quest
        if not quest.get("canSettle"):
            halted = beast_halted(root, account, client.player_id, now())
            if left and not halted:
                return {**result, "status": "quest_pending"}
            if runs and not halted:  # a changed board says nothing about whether defy can be met
                update_policy(root, defy="failed", evidence={"account": account, "quest": quest, "runs": runs})
            return {**result, "status": "defy_unmet", **({"beast_halted": True} if halted else {})}

    if not quest.get("canSettle"):
        return {**result, "status": "quest_pending"}
    settled = await client.fate("settle")
    record = settled.get("record") if isinstance(settled.get("record"), dict) else record
    reward = settled.get("reward") if isinstance(settled.get("reward"), dict) else {}
    result.update(quest=quest_view(record), reward={key: reward.get(key) for key in ("tianjiTrace", "kunwuPass", "balance")},
                  already_settled=bool(settled.get("alreadySettled")))
    if record.get("choiceKey") == "defy" and read_policy(root).get("defy") != "verified":
        update_policy(root, defy="verified", evidence={"account": account, "quest": quest_view(record)})
    return {**result, "status": "settled"}


def _day(ts: float) -> str:
    from miniapp_shared.state import day_key
    return day_key(ts)


def _beast_log_path(root: Path, account: str, player_id: int) -> Path:
    return Path(root) / "state" / "accounts" / account / "fate_cards" / "beast" / f"{int(player_id)}.json"


def beast_failure(exc: BaseException, runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "failed", "error": error_code(exc), "uncertain": is_uncertain(exc), "runs": runs}


def failure_row(player_id: int, exc: BaseException) -> dict[str, Any]:
    return {"player_id": int(player_id), "status": "failed", "error": error_code(exc),
            "uncertain": is_uncertain(exc), "detail": safe_text(type(exc).__name__, 40)}
