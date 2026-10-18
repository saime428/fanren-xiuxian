#!/usr/bin/env python3
"""LaunchAgent entrypoint: one 游历 tick for all six accounts (scoped identities).

Scope: every dwelling identity of every account, except @demo_player_01 where only the
bound character 示例绑定角色 runs (see journey_miniapp.state.IDENTITY_SCOPE).

Usage:
  python3 -m journey_miniapp.run_cycle                   # scheduled tick (every 5 min, from 00:00 Asia/Shanghai)
  python3 -m journey_miniapp.run_cycle --force           # ignore persisted timers once
  python3 -m journey_miniapp.run_cycle --account tgXXXX  # limit to one account
  python3 -m journey_miniapp.run_cycle --check           # offline configuration check
  python3 -m journey_miniapp.run_cycle --dry-run         # authenticate + read journey state, no action
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
from miniapp_shared.dwelling import (  # noqa: E402
    error_code, make_bridge, missing_sessions, open_dwelling, verify_username,
)
from miniapp_shared.notify import Notifier  # noqa: E402
from miniapp_shared.state import LockBusy, day_key, exclusive_lock, write_json  # noqa: E402
from miniapp_shared.summaries import journey_notification  # noqa: E402

from .adapter import JourneyIdentityClient, official_transport  # noqa: E402
from .coordinator import run_coordinator  # noqa: E402
from .protocol import MODE, MODE_LABEL, TARGET_DAILY_COUNT, parse_wild_state  # noqa: E402
from .runner import ACCOUNT_MINUTE_OFFSETS, START_HOUR, START_MINUTE, admitted  # noqa: E402
from .state import ALLOWED_ACCOUNTS, IDENTITY_SCOPE, scope_description, scoped_identities  # noqa: E402

SUMMARY_PATH = ROOT / "state" / "journey_miniapp" / "summary.json"
HARD_FAILURES = frozenset({"account_failed", "account_run_failed"})


def check_configuration(root: Path) -> dict[str, Any]:
    missing = missing_sessions(Path(root), ALLOWED_ACCOUNTS)
    return {"ok": not missing, "flow": "journey_miniapp", "mode": MODE, "mode_label": MODE_LABEL,
            "target_daily_count": TARGET_DAILY_COUNT, "accounts": list(ALLOWED_ACCOUNTS),
            "identity_scope": {account: scope_description(account) for account in ALLOWED_ACCOUNTS},
            "scoped_player_ids": {account: [scope["player_id"]] for account, scope in IDENTITY_SCOPE.items()},
            "usernames": dict(ACCOUNT_USERNAMES),
            "missing_sessions": missing,
            "start": f"{START_HOUR:02d}:{START_MINUTE:02d} Asia/Shanghai with per-account offsets, then every server cooldown",
            "account_minute_offsets": ACCOUNT_MINUTE_OFFSETS}


async def dry_run(root: Path, accounts: tuple[str, ...]) -> dict[str, Any]:
    transport = official_transport()
    results = []
    for account in accounts:
        try:
            async with make_bridge(root, account) as bridge:
                username = await verify_username(bridge, account)
                dwelling = await open_dwelling(bridge, transport)
                targets = scoped_identities(account, dwelling.identities)
                identities = []
                for identity in dwelling.identities:
                    row = {"player_id": identity.player_id, "identity": identity.label, "source": identity.source,
                           "in_scope": identity in targets}
                    if identity in targets:
                        client = JourneyIdentityClient(transport, dwelling, identity.player_id)
                        row["wild_experience"] = parse_wild_state(await client.details(), now_ts=time.time())
                        await asyncio.sleep(1)
                    identities.append(row)
                results.append({"account": account, "username": username,
                                "status": "ready" if targets else "scope_identity_missing", "identities": identities})
        except Exception as exc:  # noqa: BLE001
            results.append({"account": account, "status": "failed", "error": error_code(exc)})
        await asyncio.sleep(1)
    return {"flow": "journey_miniapp", "mode": "dry_run", "day": day_key(), "accounts": results}


async def _run(root: Path, *, accounts: tuple[str, ...], force: bool) -> dict[str, Any]:
    from miniapp_shared.priority import yield_to_qyz
    yielded = yield_to_qyz("journey_miniapp", time.time(), root)
    if yielded is not None and not force:
        return yielded
    config = check_configuration(root)
    if not config["ok"]:
        return {"status": "configuration_error", **config}
    try:
        with exclusive_lock(root / "state" / "journey_miniapp" / "fleet.lock"):
            now = time.time()
            results = await run_coordinator(root, day_key=day_key(now), now=now, bridge_factory=make_bridge,
                                            transport=official_transport(), accounts=accounts, force=force)
            summary = {"status": "completed", "flow": "journey_miniapp", "day": day_key(now), "updated_at": now,
                       "mode": MODE, "target_daily_count": TARGET_DAILY_COUNT,
                       "admitted": {account: admitted(account, now) for account in accounts}, "accounts": results}
            if accounts == tuple(ALLOWED_ACCOUNTS):
                due = journey_notification(summary, now_ts=time.time())
                if due is not None:
                    summary["notification"] = Notifier(root, "journey_miniapp").send_once(*due)
            write_json(SUMMARY_PATH, summary)
            return summary
    except LockBusy:
        return {"status": "lock_busy", "flow": "journey_miniapp"}


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
