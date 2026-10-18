#!/usr/bin/env python3
"""Six-account 藏娇阁 loop coordinator: sequential, one account at a time."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from miniapp_shared.daily import run_accounts

from .runner import run_account
from .state import ALLOWED_ACCOUNTS


async def run_coordinator(root: Path, *, now: float, bridge_factory: Any, transport: Any,
                          run_account_fn: Any = run_account, accounts: tuple[str, ...] = ALLOWED_ACCOUNTS,
                          force: bool = False, sleep_fn: Any = asyncio.sleep) -> list[dict[str, Any]]:
    return await run_accounts(
        accounts, lambda account: run_account_fn(root, account, now=now, bridge_factory=bridge_factory,
                                                 transport=transport, force=force),
        gap=4.0, idle=frozenset({"scheduled_wait", "lock_busy"}), sleep=sleep_fn)
