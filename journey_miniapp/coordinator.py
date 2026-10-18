#!/usr/bin/env python3
"""Six-account 游历 coordinator (identity scope per account, see state.IDENTITY_SCOPE)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from miniapp_shared.daily import run_accounts

from .runner import run_account
from .state import ALLOWED_ACCOUNTS

IDLE_STATUSES = frozenset({"daily_already_complete", "scheduled_wait"})


async def run_coordinator(root: Path, *, day_key: str, now: float, bridge_factory: Any, transport: Any,
                          run_account_fn: Any = run_account, accounts: tuple[str, ...] = ALLOWED_ACCOUNTS,
                          force: bool = False, sleep_fn: Any = asyncio.sleep) -> list[dict[str, Any]]:
    return await run_accounts(
        accounts, lambda account: run_account_fn(root, account, day_key=day_key, now=now,
                                                 bridge_factory=bridge_factory, transport=transport, force=force),
        gap=3.0, idle=IDLE_STATUSES, sleep=sleep_fn)
