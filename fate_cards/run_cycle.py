#!/usr/bin/env python3
"""LaunchAgent entrypoint: one 天机命脉 + 噬金虫进化 tick for all six accounts and every dwelling identity.

After an identity's 天机命脉 choice, every 噬金虫 run the server still allows
today is played (operator 2026-09-26: "噬金虫要打满"); 【天机命脉】 and
【噬金虫进化】 are reported separately.

Usage:
  python3 -m fate_cards.run_cycle                   # scheduled tick (every 5 min, admitted from 03:00)
  python3 -m fate_cards.run_cycle --force           # ignore admission and timers once
  python3 -m fate_cards.run_cycle --account tgXXXX  # limit to one account
  python3 -m fate_cards.run_cycle --check           # offline configuration check
  python3 -m fate_cards.run_cycle --dry-run         # authenticate and read start only, no action
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

from miniapp_shared import daily  # noqa: E402
from miniapp_shared.accounts import ACCOUNT_USERNAMES, accounts_for, stagger_offsets  # noqa: E402
from miniapp_shared.dwelling import (  # noqa: E402
    error_code, make_bridge, missing_sessions, open_dwelling, verify_username,
)
from miniapp_shared.notify import Notifier  # noqa: E402
from miniapp_shared.state import (  # noqa: E402
    LockBusy, clock_reached, day_key, exclusive_lock, read_json, write_json,
)
from miniapp_shared.summaries import beast_notification, fate_notification  # noqa: E402

from .adapter import FateIdentityClient, official_transport  # noqa: E402
from .protocol import choice_rewards, quest_view  # noqa: E402
from .runtime import (  # noqa: E402
    beast_failure, beast_runs_today, failure_row, fill_beast_runs, read_policy, run_identity,
)

FLOW = "fate_cards"
ACCOUNTS = accounts_for(FLOW)
SUMMARY_PATH = ROOT / "state" / FLOW / "summary.json"
# Timetable slot 03:00-03:15 Asia/Shanghai (docs/automation-profiles/README.md),
# after 琉璃问心塔 (02:00) and the 天机试炼 retries (02:30-02:55).
START_HOUR, START_MINUTE = 3, 0
ACCOUNT_MINUTE_OFFSETS = stagger_offsets(FLOW, 3)
PENDING_RETRY_SECONDS = 15 * 60
FAILURE_RETRY_SECONDS = 15 * 60
DONE_STATUSES = frozenset({"settled", "already_settled"})
# 噬金虫 runs wait for the identity's 天机命脉 choice (a 逆势改命 quest needs
# them); if that choice still fails this late in the day, play them anyway.
BEAST_WITHOUT_CHOICE_FROM = (21, 0)


def admitted(account: str, now_ts: float) -> bool:
    return daily.admitted(account, now_ts, start_hour=START_HOUR, start_minute=START_MINUTE,
                          offsets=ACCOUNT_MINUTE_OFFSETS)


def account_dir(root: Path, account: str) -> Path:
    return daily.account_dir(root, account, FLOW)


async def run_account(root: Path, account: str, *, now: float, bridge_factory: Any = make_bridge,
                      transport: Any = None, force: bool = False, sleep: Any = asyncio.sleep,
                      clock: Any = time.time) -> dict[str, Any]:
    day = day_key(now)
    prior = read_json(account_dir(root, account) / "summary.json")
    if not force and prior.get("day") == day:
        fate_done = prior.get("status") == "daily_complete"
        if fate_done and prior.get("beast_status") == "full":
            return {**prior, "status": "daily_already_complete"}
        if float(prior.get("next_due_at") or 0) > now:
            # Only 噬金虫 runs may still be pending: the 【天机命脉】 report keeps seeing a finished fate day.
            return {**prior, "status": "daily_already_complete" if fate_done else "scheduled_wait"}
    if not force and not admitted(account, now):
        return {"account": account, "day": day, "status": "waiting_admission"}
    transport = transport or official_transport()
    try:
        with exclusive_lock(account_dir(root, account) / "run.lock"):
            async with bridge_factory(root, account) as bridge:
                await verify_username(bridge, account)
                dwelling = await open_dwelling(bridge, transport)
                rows = []
                for identity in dwelling.identities:
                    client = FateIdentityClient(transport, dwelling, identity.player_id)
                    try:
                        row = await run_identity(root, account, client, now=clock, sleep=sleep)
                    except Exception as exc:  # noqa: BLE001 - one identity never blocks another
                        row = failure_row(identity.player_id, exc)
                    if row.get("choice") or clock_reached(now, *BEAST_WITHOUT_CHOICE_FROM):
                        try:
                            row["beast"] = await fill_beast_runs(root, account, client, now=clock, sleep=sleep)
                        except Exception as exc:  # noqa: BLE001 - retried on the next pending tick
                            row["beast"] = beast_failure(exc, beast_runs_today(root, account, identity.player_id, now))
                    rows.append({"identity": identity.label, **row})
    except LockBusy:
        return {"account": account, "day": day, "status": "lock_busy"}
    except Exception as exc:  # noqa: BLE001
        summary = {"account": account, "day": day, "status": "account_failed", "error": error_code(exc),
                   "next_due_at": now + FAILURE_RETRY_SECONDS}
        write_json(account_dir(root, account) / "summary.json", summary)
        return summary
    done = bool(rows) and all(row.get("status") in DONE_STATUSES for row in rows)
    terminal = all(row.get("status") in DONE_STATUSES | {"defy_unmet"} for row in rows)
    beast_states = [(row.get("beast") or {}).get("status") for row in rows]
    beast_full = bool(rows) and all(state == "full" for state in beast_states)
    # A halted fill (changed board shape) is done for today, like a full one, but is reported as such.
    beast_done = bool(rows) and all(state in {"full", "halted"} for state in beast_states)
    summary = {"account": account, "day": day, "updated_at": time.time(), "identities": rows,
               "status": "daily_complete" if done else ("daily_unmet" if terminal and rows else "pending"),
               "beast_status": "full" if beast_full else "pending"}
    if not (done and beast_full):
        summary["next_due_at"] = now + (24 * 3600 if terminal and beast_done else PENDING_RETRY_SECONDS)
    write_json(account_dir(root, account) / "summary.json", summary)
    return summary


def check_configuration(root: Path) -> dict[str, Any]:
    missing = missing_sessions(Path(root), ACCOUNTS)
    return {"ok": not missing, "flow": FLOW, "accounts": list(ACCOUNTS), "usernames": dict(ACCOUNT_USERNAMES),
            "missing_sessions": missing, "policy": read_policy(root),
            "start": f"{START_HOUR:02d}:{START_MINUTE:02d} Asia/Shanghai with per-account offsets",
            "account_minute_offsets": ACCOUNT_MINUTE_OFFSETS}


async def dry_run(root: Path, accounts: tuple[str, ...]) -> dict[str, Any]:
    transport = official_transport()
    results = []
    for account in accounts:
        try:
            async with make_bridge(root, account) as bridge:
                await verify_username(bridge, account)
                dwelling = await open_dwelling(bridge, transport)
                identities = []
                for identity in dwelling.identities:
                    start = await FateIdentityClient(transport, dwelling, identity.player_id).fate("start")
                    record = start.get("record") if isinstance(start.get("record"), dict) else {}
                    identities.append({"player_id": identity.player_id, "identity": identity.label,
                                       "has_drawn": bool(start.get("hasDrawn")), "rewards": choice_rewards(start),
                                       "choice": record.get("choiceKey"), "quest": quest_view(record)})
                results.append({"account": account, "status": "ready", "identities": identities})
        except Exception as exc:  # noqa: BLE001
            results.append({"account": account, "status": "failed", "error": error_code(exc)})
        await asyncio.sleep(1)
    return {"flow": FLOW, "mode": "dry_run", "day": day_key(), "accounts": results}


async def _run(root: Path, *, accounts: tuple[str, ...], force: bool) -> dict[str, Any]:
    from miniapp_shared.priority import yield_to_qyz
    yielded = yield_to_qyz(FLOW, time.time(), root)
    if yielded is not None and not force:
        return yielded
    config = check_configuration(root)
    if not config["ok"]:
        return {"status": "configuration_error", **config}
    try:
        with exclusive_lock(root / "state" / FLOW / "fleet.lock"):
            results = []
            for account in accounts:
                result = await run_account(root, account, now=time.time(), force=force)
                results.append(result)
            summary = {"status": "completed", "flow": FLOW, "day": day_key(), "updated_at": time.time(),
                       "policy": read_policy(root), "accounts": results}
            if accounts == ACCOUNTS:
                for field, builder in (("notification", fate_notification), ("beast_notification", beast_notification)):
                    due = builder(summary, now_ts=time.time())
                    if due is not None:
                        summary[field] = Notifier(root, FLOW).send_once(*due)
            write_json(SUMMARY_PATH, summary)
            return summary
    except LockBusy:
        return {"status": "lock_busy", "flow": FLOW}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--account", default="all", choices=["all", *ACCOUNTS])
    args = parser.parse_args(argv)
    accounts = ACCOUNTS if args.account == "all" else (args.account,)
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
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
