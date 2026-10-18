#!/usr/bin/env python3
"""LaunchAgent entrypoint: one 藏娇阁 双侍妾 loop tick for all six business accounts.

Usage:
  python3 -m pavilion_loop.run_cycle              # scheduled tick (honours per-account next_due_at)
  python3 -m pavilion_loop.run_cycle --force      # ignore persisted timers once
  python3 -m pavilion_loop.run_cycle --account tgXXXX
  python3 -m pavilion_loop.run_cycle --check      # offline configuration check
  python3 -m pavilion_loop.run_cycle --dry-run    # read companions and timers, no command
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from miniapp_shared.accounts import ACCOUNT_USERNAMES  # noqa: E402
from miniapp_shared.dwelling import error_code, make_bridge, missing_sessions, open_dwelling, verify_username  # noqa: E402
from miniapp_shared.notify import Notifier  # noqa: E402
from miniapp_shared.priority import yield_to_qyz  # noqa: E402
from miniapp_shared.state import LockBusy, exclusive_lock, write_json  # noqa: E402
from miniapp_shared.summaries import pavilion_notification  # noqa: E402

from .coordinator import run_coordinator  # noqa: E402
from .protocol import (  # noqa: E402
    DIVINATION_COOLDOWN_MIN, DREAM_MAP_COOLDOWN_MIN, HEART_TRIAL_COOLDOWN_MIN, ROUTES, ZHUIMO_COOLDOWN_MIN,
    companions_from_payload,
)
from .runner import official_transport  # noqa: E402
from .state import ALLOWED_ACCOUNTS, NO_SAIL, ROUTE_OVERRIDES  # noqa: E402

SUMMARY_PATH = ROOT / "state" / "pavilion_loop" / "summary.json"
HARD_FAILURES = frozenset({"account_failed", "account_run_failed"})


def check_configuration(root: Path) -> dict[str, Any]:
    missing = missing_sessions(Path(root), ALLOWED_ACCOUNTS)
    return {"ok": not missing, "flow": "pavilion_loop", "accounts": list(ALLOWED_ACCOUNTS),
            "usernames": dict(ACCOUNT_USERNAMES), "missing_sessions": missing,
            "cooldowns_min": {"入梦寻图": DREAM_MAP_COOLDOWN_MIN, "天机代卜": DIVINATION_COOLDOWN_MIN, "坠魔心劫": ZHUIMO_COOLDOWN_MIN,
                              "共历心劫(群内)": HEART_TRIAL_COOLDOWN_MIN},
            "routes": {k: v[0] for k, v in ROUTES.items()}, "route_overrides": ROUTE_OVERRIDES,
            "no_sail": {account: sorted(names) for account, names in NO_SAIL.items()},
            "order": [".远航归来", ".天机代卜", ".入梦寻图", ".坠魔心劫", ".共历心劫(群内 稳/稳/稳)", ".拼图", ".侍妾远航"], "both_companions": True}


async def dry_run(root: Path, accounts: tuple[str, ...]) -> dict[str, Any]:
    transport = official_transport()
    now = time.time()
    results = []
    for account in accounts:
        try:
            async with make_bridge(root, account) as bridge:
                username = await verify_username(bridge, account)
                dwelling = await open_dwelling(bridge, transport)
                rows = companions_from_payload(dwelling.start_payload, now_ts=now)
                for row in rows:
                    for key in ("dream_map_ready_ts", "divination_ready_ts", "zhuimo_ready_ts", "heart_trial_ready_ts", "voyage_end_ts"):
                        row[key.replace("_ts", "_in_min")] = round((row[key] - now) / 60) if row[key] else None
                results.append({"account": account, "username": username, "status": "ready", "companions": rows})
        except Exception as exc:  # noqa: BLE001
            results.append({"account": account, "status": "failed", "error": error_code(exc)})
        await asyncio.sleep(1)
    return {"flow": "pavilion_loop", "mode": "dry_run", "accounts": results}


async def _run(root: Path, *, accounts: tuple[str, ...], force: bool) -> dict[str, Any]:
    yielded = yield_to_qyz("pavilion_loop", time.time(), root)
    if yielded is not None and not force:
        return yielded
    config = check_configuration(root)
    if not config["ok"]:
        return {"status": "configuration_error", **config}
    try:
        with exclusive_lock(root / "state" / "pavilion_loop" / "fleet.lock"):
            now = time.time()
            results = await run_coordinator(root, now=now, bridge_factory=make_bridge, transport=official_transport(),
                                            accounts=accounts, force=force)
            summary = {"status": "completed", "flow": "pavilion_loop", "updated_at": now, "accounts": results}
            if accounts == tuple(ALLOWED_ACCOUNTS):
                due = pavilion_notification(summary, now_ts=time.time())
                if due is not None:
                    summary["notification"] = Notifier(root, "pavilion_loop").send_once(*due)
            write_json(SUMMARY_PATH, summary)
            return summary
    except LockBusy:
        return {"status": "lock_busy", "flow": "pavilion_loop"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--account", default="all", choices=["all", *ALLOWED_ACCOUNTS])
    args = parser.parse_args(argv)
    accounts = tuple(ALLOWED_ACCOUNTS) if args.account == "all" else (args.account,)
    if args.check:
        result = check_configuration(ROOT)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["ok"] else 2
    if args.dry_run:
        result = asyncio.run(dry_run(ROOT, accounts))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if all(item.get("status") == "ready" for item in result["accounts"]) else 1
    result = asyncio.run(_run(ROOT, accounts=accounts, force=args.force))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result.get("status") in {"lock_busy", "yielding_to_qyz"}:
        return 0
    if result.get("status") != "completed":
        return 1
    return 1 if any(item.get("status") in HARD_FAILURES for item in result["accounts"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
