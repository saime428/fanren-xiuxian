#!/usr/bin/env python3
"""Helpers shared by the daily/periodic MiniApp flow packages."""
from __future__ import annotations

import asyncio
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .accounts import ALL_ACCOUNTS, BOUND_IDENTITY
from .state import exclusive_lock, shanghai_now

# Identity scope shared by the flows that act on dwelling identities (星宫灵圃,
# 游历): an account's ``bound_identity`` in the accounts config. On such an
# account only that bound character runs and the main soul stays manual (demo:
# @demo_player_01 runs only 示例绑定角色). Accounts without an entry are unrestricted.


def scoped_identities(account: str, identities: Any) -> list[Any]:
    """All ``identities``, or for a scoped account only its bound one.

    The stable ``player_id`` decides; the dao-name label is only a fallback
    when the server no longer lists that id.
    """
    rows = list(identities or ())
    scope = BOUND_IDENTITY.get(account)
    if not scope:
        return rows
    by_id = [row for row in rows if int(getattr(row, "player_id", 0) or 0) == scope["player_id"]]
    return by_id or [row for row in rows if str(getattr(row, "label", "") or "") == scope["label"]]


def account_dir(root: Path, account: str, flow: str, allowed: tuple[str, ...] = ALL_ACCOUNTS) -> Path:
    """``state/accounts/<account>/<flow>``; an account outside ``allowed`` raises ValueError."""
    if account not in allowed:
        raise ValueError("account_not_allowed")
    return Path(root) / "state" / "accounts" / account / flow


def account_lock(root: Path, account: str, flow: str,
                 allowed: tuple[str, ...] = ALL_ACCOUNTS) -> AbstractContextManager[None]:
    """The account's non-blocking ``run.lock`` for ``flow`` (raises LockBusy when held)."""
    return exclusive_lock(account_dir(root, account, flow, allowed) / "run.lock")


# Official replies usually land within a few seconds: read early, then space out.
POLL_DELAYS_SECONDS = (2.0, 3.0, 5.0, 8.0)


async def poll_until(read: Callable[[], Awaitable[Any]], *, timeout: float, sleep: Callable[[float], Awaitable[Any]],
                     monotonic: Callable[[], float], delays: tuple[float, ...] = POLL_DELAYS_SECONDS) -> Any:
    """Call ``read()`` until it returns non-None or ``timeout`` seconds pass (last read at the deadline)."""
    deadline = monotonic() + timeout
    attempt = 0
    while True:
        value = await read()
        remaining = deadline - monotonic()
        if value is not None or remaining <= 0:
            return value
        await sleep(min(delays[min(attempt, len(delays) - 1)], remaining))
        attempt += 1


def admitted(account: str, now_ts: float, *, start_hour: int, start_minute: int, offsets: Mapping[str, int]) -> bool:
    """True once today's Asia/Shanghai slot ``start + offsets[account]`` minutes has begun."""
    now = shanghai_now(now_ts)
    gate = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    return (now - gate).total_seconds() >= offsets.get(account, 0) * 60


async def run_accounts(accounts: tuple[str, ...], run_one: Callable[[str], Awaitable[dict[str, Any]]], *,
                       gap: float, idle: frozenset[str] = frozenset(),
                       sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                       failed_status: str = "account_run_failed") -> list[dict[str, Any]]:
    """Run accounts one at a time, pausing ``gap`` s after any result whose status is not in ``idle``."""
    results: list[dict[str, Any]] = []
    for index, account in enumerate(accounts):
        try:
            result = await run_one(account)
        except Exception as exc:  # noqa: BLE001 - one account never stops the others
            result = {"ok": False, "account": account, "status": failed_status, "error": type(exc).__name__}
        results.append(result)
        if index + 1 < len(accounts) and result.get("status") not in idle:
            await sleep(gap)
    return results
