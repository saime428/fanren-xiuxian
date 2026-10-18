#!/usr/bin/env python3
"""LaunchAgent entrypoint: one 宗门日常 tick for all six business accounts.

Usage:
  python3 -m sect_daily.run_cycle                   # scheduled tick (01:00 Asia/Shanghai gate, 3-min offsets)
  python3 -m sect_daily.run_cycle --force           # ignore the admission gate and cached day
  python3 -m sect_daily.run_cycle --account tgXXXX  # limit to one account
  python3 -m sect_daily.run_cycle --check           # offline configuration check
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
from miniapp_shared.dwelling import make_bridge, missing_sessions  # noqa: E402
from miniapp_shared.notify import Notifier  # noqa: E402
from miniapp_shared.state import LockBusy, day_key, exclusive_lock, write_json  # noqa: E402
from miniapp_shared.summaries import sect_notification  # noqa: E402

from .coordinator import run_coordinator  # noqa: E402
from .runner import ACCOUNT_MINUTE_OFFSETS, START_HOUR, START_MINUTE, admitted  # noqa: E402
from .state import ALLOWED_ACCOUNTS  # noqa: E402

SUMMARY_PATH = ROOT / "state" / "sect_daily" / "summary.json"
HARD_FAILURES = frozenset({"account_failed", "account_run_failed"})


def check_configuration(root: Path) -> dict[str, Any]:
    missing = missing_sessions(Path(root), ALLOWED_ACCOUNTS)
    return {"ok": not missing, "flow": "sect_daily", "accounts": list(ALLOWED_ACCOUNTS),
            "usernames": dict(ACCOUNT_USERNAMES), "missing_sessions": missing,
            "commands": [".宗门点卯", ".边境军功", ".搜集军报", ".公开军报 <真报编号>", ".支援慕兰 <路线>"],
            "admission": f"{START_HOUR:02d}:{START_MINUTE:02d} Asia/Shanghai", "account_minute_offsets": ACCOUNT_MINUTE_OFFSETS}


async def _run(root: Path, *, accounts: tuple[str, ...], force: bool) -> dict[str, Any]:
    from miniapp_shared.priority import yield_to_qyz
    yielded = yield_to_qyz("sect_daily", time.time(), root)
    if yielded is not None and not force:
        return yielded
    config = check_configuration(root)
    if not config["ok"]:
        return {"status": "configuration_error", **config}
    try:
        with exclusive_lock(root / "state" / "sect_daily" / "fleet.lock"):
            now = time.time()
            results = await run_coordinator(root, day_key=day_key(now), now=now, bridge_factory=make_bridge,
                                            accounts=accounts, force=force)
            summary = {"status": "completed", "flow": "sect_daily", "day": day_key(now), "updated_at": now,
                       "admitted": {account: admitted(account, now) for account in accounts}, "accounts": results}
            if accounts == tuple(ALLOWED_ACCOUNTS):
                due = sect_notification(summary, now_ts=time.time())
                if due is not None:
                    summary["notification"] = Notifier(root, "sect_daily").send_once(*due)
            write_json(SUMMARY_PATH, summary)
            return summary
    except LockBusy:
        return {"status": "lock_busy", "flow": "sect_daily"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--account", default="all", choices=["all", *ALLOWED_ACCOUNTS])
    args = parser.parse_args(argv)
    accounts = tuple(ALLOWED_ACCOUNTS) if args.account == "all" else (args.account,)
    if args.check:
        result = check_configuration(ROOT)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["ok"] else 2
    result = asyncio.run(_run(ROOT, accounts=accounts, force=args.force))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result.get("status") in {"lock_busy", "yielding_to_qyz"}:
        return 0
    if result.get("status") != "completed":
        return 1
    return 1 if any(item.get("status") in HARD_FAILURES for item in result["accounts"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
