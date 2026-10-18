#!/usr/bin/env python3
"""Six-account 宗门日常 coordinator: sequential, one account at a time, staggered by admission offsets."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from miniapp_shared.daily import run_accounts

from .runner import run_account
from .state import ALLOWED_ACCOUNTS


async def run_coordinator(root: Path, *, day_key: str, now: float, bridge_factory: Any,
                          run_account_fn: Any = run_account, accounts: tuple[str, ...] = ALLOWED_ACCOUNTS,
                          force: bool = False, sleep_fn: Any = asyncio.sleep) -> list[dict[str, Any]]:
    return await run_accounts(
        accounts, lambda account: run_account_fn(root, account, day_key=day_key, now=now,
                                                 bridge_factory=bridge_factory, force=force),
        gap=5.0, idle=frozenset({"daily_already_complete", "waiting_admission", "scheduled_wait"}), sleep=sleep_fn)
