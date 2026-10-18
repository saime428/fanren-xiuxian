#!/usr/bin/env python3
"""LaunchAgent entrypoint: one 星宫灵圃 tick for the star-palace accounts (demo_player_01 only as 示例绑定角色).

Usage:
  python3 -m star_observatory.run_cycle            # scheduled tick (honours next_due_at)
  python3 -m star_observatory.run_cycle --force    # ignore the persisted timer once
  python3 -m star_observatory.run_cycle --check    # offline configuration check
  python3 -m star_observatory.run_cycle --dry-run  # authenticate + read farm state, no mutation
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
from miniapp_shared.dwelling import error_code, make_bridge, missing_sessions, verify_username  # noqa: E402
from miniapp_shared.notify import Notifier  # noqa: E402
from miniapp_shared.state import LockBusy, exclusive_lock, write_json  # noqa: E402
from miniapp_shared.summaries import star_notification  # noqa: E402

from .adapter import StarAccountSession, official_transport  # noqa: E402
from .coordinator import run_coordinator  # noqa: E402
from .protocol import STAR_NAME, choose_action, parse_farm_state, safe_plot_snapshot  # noqa: E402
from .runner import HARD_FAILURE_STATUSES  # noqa: E402
from .state import ALLOWED_ACCOUNTS, identity_label  # noqa: E402

SUMMARY_PATH = ROOT / "state" / "star_observatory" / "summary.json"


def check_configuration(root: Path) -> dict[str, Any]:
    missing = missing_sessions(Path(root), ALLOWED_ACCOUNTS)
    return {"ok": not missing, "flow": "star_observatory", "star_name": STAR_NAME,
            "accounts": list(ALLOWED_ACCOUNTS), "usernames": {a: ACCOUNT_USERNAMES[a] for a in ALLOWED_ACCOUNTS}, "missing_sessions": missing,
            "identity_scope": {a: (identity_label(a) or "personal identity") for a in ALLOWED_ACCOUNTS}}


async def dry_run(root: Path) -> dict[str, Any]:
    transport = official_transport()
    results = []
    for account in ALLOWED_ACCOUNTS:
        try:
            async with make_bridge(root, account) as bridge:
                username = await verify_username(bridge, account)
                session = StarAccountSession(bridge, transport)
                start = await session.prepare()
                state = parse_farm_state(start)
                results.append({"account": account, "username": username, "status": "ready" if state else "invalid_state",
                                "player_id": session.player_id, "identity": session.identity_label, "sect_name": state.get("sect_name"),
                                "plots": safe_plot_snapshot(state), "max_wait_sec": state.get("max_wait"),
                                "decision": choose_action(state) if state else None})
        except Exception as exc:  # noqa: BLE001
            results.append({"account": account, "status": "failed", "error": error_code(exc)})
        await asyncio.sleep(1)
    return {"flow": "star_observatory", "mode": "dry_run", "accounts": results}


async def _run(root: Path, *, force: bool) -> dict[str, Any]:
    from miniapp_shared.priority import yield_to_qyz
    yielded = yield_to_qyz("star_observatory", time.time(), root)
    if yielded is not None and not force:
        return yielded
    config = check_configuration(root)
    if not config["ok"]:
        return {"status": "configuration_error", **config}
    try:
        with exclusive_lock(root / "state" / "star_observatory" / "coordinator.lock"):
            result = await run_coordinator(root, now=time.time(), bridge_factory=make_bridge,
                                           transport=official_transport(), force=force)
            due = star_notification(result, now_ts=time.time())
            if due is not None:
                result["notification"] = Notifier(root, "star_observatory").send_once(*due)
            write_json(SUMMARY_PATH, result)
            return result
    except LockBusy:
        return {"status": "lock_busy", "flow": "star_observatory"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        result = check_configuration(ROOT)
    elif args.dry_run:
        result = asyncio.run(dry_run(ROOT))
    else:
        result = asyncio.run(_run(ROOT, force=args.force))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.check:
        return 0 if result["ok"] else 2
    if args.dry_run:
        return 0 if all(item.get("status") == "ready" for item in result["accounts"]) else 1
    if result.get("status") in {"lock_busy", "yielding_to_qyz"}:
        return 0
    if result.get("status") != "completed":
        return 1
    return 1 if any(item.get("status") in HARD_FAILURE_STATUSES for item in result["accounts"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
