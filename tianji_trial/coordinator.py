#!/usr/bin/env python3
"""Six-account Tianji Trial coordinator and command-line entrypoint."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from miniapp_shared.state import SHANGHAI, LockBusy, exclusive_lock

from .adapter import ACCOUNT_CONFIG, LiveAccountBridge
from .protocol import TianjiTrialError
from .runtime import TianjiTrialRunner, _load_json, _write_json
from .transport import DwellingTrialTransport


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_ROOT = Path(__file__).resolve().parent / "state" / "accounts"
START_HOUR, START_MINUTE = 1, 30
ACCOUNT_MINUTE_OFFSETS = {
    "tg9100000001": 0,
    "tg9100000002": 2,
    "tg9100000003": 4,
    "tg9100000004": 6,
    "tg9100000005": 8,
    "tg9100000006": 10,
}


@contextmanager
def account_lock(path: Path):
    with ExitStack() as stack:
        try:
            stack.enter_context(exclusive_lock(path))
        except LockBusy as exc:
            raise TianjiTrialError("account_lock_busy") from exc
        yield


async def run_account(
    project_root: Path,
    account: str,
    *,
    today: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    bridge_factory: Callable[[Path, str], Any] = LiveAccountBridge,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    notify: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if account not in ACCOUNT_CONFIG:
        raise TianjiTrialError("account_unknown")
    now = now or datetime.now(SHANGHAI)
    now = now.astimezone(SHANGHAI) if now.tzinfo is not None else now.replace(tzinfo=SHANGHAI)
    today = today or now.strftime("%Y-%m-%d")
    # Timetable slot 01:30-01:40 Asia/Shanghai (docs/automation-profiles/README.md):
    # after 宗门日常 (01:00-01:15) and clear of every other midnight batch.
    scheduled_at = now.replace(hour=START_HOUR, minute=START_MINUTE + ACCOUNT_MINUTE_OFFSETS[account],
                               second=0, microsecond=0)
    if not dry_run and now < scheduled_at:
        return {
            "account": account,
            "status": "waiting",
            "scheduled_at": scheduled_at.isoformat(),
        }
    if not dry_run:
        from miniapp_shared.priority import yield_to_qyz
        yielded = yield_to_qyz("tianji_trial", now.timestamp(), Path(project_root))
        if yielded is not None:
            return {"account": account, **yielded}
    state_dir = Path(project_root) / "tianji_trial" / "state" / "accounts" / account
    summary_path = state_dir / "summary.json"
    existing = _load_json(summary_path)
    existing_identities = existing.get("identities") or []
    confirmed_summary = (existing.get("account") == account and existing.get("receipt_confirmed") is True
                         and isinstance(existing_identities, list) and bool(existing_identities)
                         and existing.get("identity_count") == len(existing_identities))
    for item in existing_identities if confirmed_summary else []:
        if not isinstance(item, dict) or type(item.get("player_id")) is not int:
            confirmed_summary = False
            break
        receipt = _load_json(state_dir / "identities" / f"{item['player_id']}.json")
        if (receipt.get("player_id") != item["player_id"] or receipt.get("last_date") != today
                or receipt.get("receipt_confirmed") is not True or receipt.get("pending_finish")
                or type(receipt.get("limit")) is not int or receipt["limit"] <= 0
                or receipt.get("completed") != receipt["limit"]):
            confirmed_summary = False
            break
    if (not dry_run and confirmed_summary and existing.get("last_date") == today
            and existing.get("status") == "completed"):
        return {
            "account": account,
            "status": "done",
            "last_date": today,
            "identities": existing.get("identities", []),
        }

    with account_lock(state_dir / "run.lock"):
        async with bridge_factory(Path(project_root), account) as bridge:
            transport = DwellingTrialTransport(bridge.authenticate, bridge.post_json)
            auth = await transport.initialize()
            identities = tuple(auth.identities)
            if not identities:
                raise TianjiTrialError("miniapp_identity_missing")
            if dry_run:
                result = {
                    "account": account,
                    "status": "ready",
                    "identity_count": len(identities),
                    "identities": [
                        {"player_id": identity.player_id, "identity": identity.label, "source": identity.source}
                        for identity in identities
                    ],
                }
                # A dry run must never replace the day's confirmed summary: the
                # daily report and the next cron slot both read summary.json.
                _write_json(state_dir / "dry_run.json", {**result, "updated_at_ts": time.time()})
                return result

            runner = TianjiTrialRunner(state_dir, transport, sleep=sleep)
            identity_results: list[dict[str, Any]] = []
            for index, identity in enumerate(identities):
                try:
                    result = await runner.run_identity(identity, today=today)
                    identity_results.append(
                        {
                            "player_id": identity.player_id,
                            "identity": identity.label,
                            "source": identity.source,
                            **result,
                        }
                    )
                except asyncio.CancelledError:
                    raise
                except TianjiTrialError as exc:
                    identity_results.append(
                        {
                            "player_id": identity.player_id,
                            "identity": identity.label,
                            "source": identity.source,
                            "status": "unknown" if exc.uncertain else "failed",
                            "error": exc.code,
                        }
                    )
                if index + 1 < len(identities):
                    await sleep(1)

            completed = all(
                item.get("status") in {"done", "already", "completed"}
                for item in identity_results
            )
            aggregate = {
                "account": account,
                "status": "completed" if completed else "retry",
                "identity_count": len(identities),
                "identities": identity_results,
                "updated_at_ts": time.time(),
                "receipt_confirmed": completed,
                "attempt_date": today,
            }
            if completed:
                aggregate["last_date"] = today
            else:
                aggregate["retry_not_before_ts"] = time.time() + 120
            _write_json(summary_path, aggregate)
    # Report only after the bridge closed: the Hermes CLI (subprocess plus
    # retry sleeps, up to ~100 s) blocks the loop a live Telethon client needs.
    try:
        if notify is None:
            from .report import notify_if_due as notify
        aggregate["notification"] = notify(Path(project_root))
    except Exception as exc:  # noqa: BLE001 - reporting never fails the trial
        aggregate["notification"] = {"status": "error", "error": type(exc).__name__}
    return aggregate


async def run_selected(accounts: list[str], *, dry_run: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for account in accounts:
        try:
            results.append(await run_account(PROJECT_ROOT, account, dry_run=dry_run))
        except asyncio.CancelledError:
            raise
        except TianjiTrialError as exc:
            results.append(
                {
                    "account": account,
                    "status": "unknown" if exc.uncertain else "failed",
                    "error": exc.code,
                }
            )
        await asyncio.sleep(1)
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run upstream-faithful Tianji Trial automation")
    parser.add_argument("--account", default="all", choices=["all", *ACCOUNT_CONFIG])
    parser.add_argument("--dry-run", action="store_true", help="authenticate and list identities without starting a trial")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    accounts = list(ACCOUNT_CONFIG) if args.account == "all" else [args.account]
    results = asyncio.run(run_selected(accounts, dry_run=bool(args.dry_run)))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item.get("status") in {"ready", "waiting", "done", "completed"} for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
