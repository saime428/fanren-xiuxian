#!/usr/bin/env python3
"""星宫灵圃 coordinator (demo_player_04, demo_player_05, demo_player_01/示例绑定角色)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from miniapp_shared.daily import run_accounts

from .runner import run_account
from .state import ALLOWED_ACCOUNTS


async def run_coordinator(root: Path, *, now: float, bridge_factory: Any, transport: Any,
                          account_runner: Any = run_account, accounts: tuple[str, ...] = ALLOWED_ACCOUNTS,
                          force: bool = False, sleep_fn: Any = asyncio.sleep) -> dict[str, Any]:
    results = await run_accounts(
        accounts, lambda account: account_runner(root, account, now=now, bridge_factory=bridge_factory,
                                                 transport=transport, force=force),
        gap=2.0, sleep=sleep_fn, failed_status="runtime_error")
    return {"status": "completed", "flow": "star_observatory", "updated_at": float(now), "accounts": results}
