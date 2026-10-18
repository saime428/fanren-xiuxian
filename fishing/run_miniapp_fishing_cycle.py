#!/usr/bin/env python3
"""Run one independent, source-compatible fishing cycle per eligible account."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import math
import time
from pathlib import Path

import sys
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from miniapp_shared.accounts import stagger_offsets  # noqa: E402
from miniapp_shared.priority import yield_to_qyz  # noqa: E402
from miniapp_shared.state import LockBusy, day_key, exclusive_lock, shanghai_now  # noqa: E402

from miniapp_fishing_integration import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_DAILY_CAST_LIMIT,
    DEFAULT_STATE_ROOT,
    UpstreamFishingProtocol,
    _safe,
    _utc_now,
    _write_json,
    account_options,
    daily_quota_done,
    load_fishing_config,
    merge_confirmed_cast_count,
)

DAILY_CAST_LIMIT = DEFAULT_DAILY_CAST_LIMIT
FISHING_RUNTIME_VERSION = "fishing-isolated-v1"
RATE_LIMIT_RETRY_SECONDS = (5 * 60, 15 * 60, 60 * 60)
# Business failures carry no server deadline; without a backoff the 20s tick
# re-authenticated the account (session copy, connect, entry discovery) every time.
FAILURE_STATUSES = frozenset({
    "shop_failed", "bait_missing", "bait_missing_after_confirmed_purchase", "no_rod", "next_failed",
    "start_failed", "start_bite_failed", "cast_unconfirmed", "challenge_unclassified", "finish_rejected",
})
FAILURE_RETRY_SECONDS = (120, 300, 900, 1800)
# admitted() spaces only each day's first cast; afterwards every tick logged all
# eligible accounts in at once (six MTProto handshakes, entry reads and dwelling
# /start + /external together).
# ponytail: fixed spacing rather than a 2-worker pool: casts stay parallel (one
# may wait BITE_WAIT_MAX_SECONDS for its bite) and the last login waits 20 s.
LOGIN_STAGGER_SECONDS = 4.0


def account_result(state_root: Path, account: str) -> dict:
    path = state_root / "accounts" / account / "fishing.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict) or payload.get("account", account) != account:
        raise ValueError("fishing result account mismatch")
    result = payload.get("result") or {}
    if not isinstance(result, dict):
        raise ValueError("invalid fishing result")
    result = dict(result)
    today = day_key()
    if result.get("settlement_date") != today:
        # A delayed midnight helper must not leave yesterday's five casts as
        # today's quota. Preserve any server backoff that crosses midnight.
        result.update(daily_cast_count=0, counted_settlement_ids=[], settlement_date=today,
                      server_daily_limit_reached=False, catches=[])
    return result


def coordinator_lock(state_root: Path, *, blocking: bool = False):
    """Non-blocking by default: an overlapping tick gets LockBusy and skips."""
    return exclusive_lock(state_root / "coordinator.lock", blocking=blocking)


# Timetable slot 00:00-00:10 Asia/Shanghai (docs/automation-profiles/README.md):
# the operator wants 灵溪垂钓 to open the day, right after the local reset.
# Accounts still start two minutes apart so six dwelling logins never coincide.
START_HOUR, START_MINUTE = 0, 0
ACCOUNT_MINUTE_OFFSETS = stagger_offsets("fishing", 2)


def admitted(account: str, now_ts: float) -> bool:
    now = shanghai_now(now_ts)
    gate = now.replace(hour=START_HOUR, minute=START_MINUTE, second=0, microsecond=0)
    return (now - gate).total_seconds() >= ACCOUNT_MINUTE_OFFSETS.get(account, 0) * 60


def quota_done(state: dict, limit: int | None = DAILY_CAST_LIMIT) -> bool:
    return daily_quota_done({**state, "daily_cast_limit": limit})


def eligible_accounts(accounts: list[str], states: dict[str, dict], *, now_ts: float,
                      limits: dict[str, int | None] | None = None) -> list[str]:
    eligible = []
    for account in accounts:
        state = states.get(account, {})
        count = int(state.get("daily_cast_count", 0) or 0)
        if quota_done(state, (limits or {}).get(account, DAILY_CAST_LIMIT)):
            continue
        if count == 0 and not admitted(account, now_ts):
            continue
        if retry_deadline(state) <= now_ts:
            eligible.append(account)
    return eligible


def retry_deadline(state: dict) -> float:
    try:
        retry_not_before = float(state.get("retry_not_before_ts", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid fishing retry deadline") from exc
    if not math.isfinite(retry_not_before) or retry_not_before < 0:
        raise ValueError("invalid fishing retry deadline")
    return retry_not_before


def valid_states(state_root: Path, accounts: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Each readable account's state, and the accounts whose file is invalid.

    A corrupt or foreign fishing.json must stop only its own account: it is
    skipped (no network work, no overwrite) until an operator fixes it.
    """
    states, invalid = {}, []
    for account in accounts:
        try:
            state = account_result(state_root, account)
            retry_deadline(state)
            int(state.get("daily_cast_count", 0) or 0)
        except (TypeError, ValueError) as exc:
            print(f"fishing state invalid account={account}: {_safe(str(exc))}; skipped")
            invalid.append(account)
            continue
        states[account] = state
    return states, invalid


def apply_rate_limit_backoff(previous: dict, result: dict, *, now_ts: float) -> dict:
    value = dict(result)
    if value.get("error_code") == "external_action_rate_limited":
        try:
            previous_failures = max(0, int(previous.get("rate_limit_failures", 0) or 0))
        except (TypeError, ValueError):
            previous_failures = 0
        failures = previous_failures + 1
        wait = RATE_LIMIT_RETRY_SECONDS[min(failures, len(RATE_LIMIT_RETRY_SECONDS)) - 1]
        value.update(rate_limit_failures=failures, retry_not_before_ts=now_ts + wait)
    else:
        # A pending finish/cast already carries its confirmation deadline.
        # Clearing it here would reopen Telegram/WebView on every 20s tick.
        retry_at = float(value.get("retry_not_before_ts") or 0)
        if not math.isfinite(retry_at) or retry_at < 0:
            raise ValueError("invalid fishing retry deadline")
        status = value.get("status")
        if status in FAILURE_STATUSES:
            try:
                failures = max(0, int(previous.get("rate_limit_failures", 0) or 0)) + 1
            except (TypeError, ValueError):
                failures = 1
            if retry_at <= now_ts:
                retry_at = now_ts + FAILURE_RETRY_SECONDS[min(failures, len(FAILURE_RETRY_SECONDS)) - 1]
        else:
            failures = previous.get("rate_limit_failures", 0) if status == "failed" else 0
            if status in {"failed", "finish_pending_settlement", "cast_reply_pending", "waiting"} and retry_at <= now_ts:
                retry_at = now_ts + 120
        value.update(rate_limit_failures=failures, retry_not_before_ts=retry_at)
    return value


async def run_account(account: str, config: dict) -> tuple[str, dict]:
    try:
        result = await UpstreamFishingProtocol().run_account(account, config)
    except Exception as exc:
        result = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error_code": _safe(str(exc)),
        }
    return account, result


async def run_all(accounts: list[str], config: dict) -> list[tuple[str, dict]]:
    if not accounts:
        return []
    # The protocol contains synchronous HTTP (including entry discovery).
    # Each account owns its event loop, client and timers: a slow request must
    # never consume another account's bite/reel window. Logins start
    # LOGIN_STAGGER_SECONDS apart instead of all six in the same tick second.
    def worker(index, account, options):
        time.sleep(index * LOGIN_STAGGER_SECONDS)
        return asyncio.run(run_account(account, options))

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=min(6, len(accounts)), thread_name_prefix="fishing-account") as pool:
        pending = [loop.run_in_executor(pool, worker, index, account, account_options(config, account))
                   for index, account in enumerate(accounts)]
        return list(await asyncio.gather(*pending))


def _run_cycle() -> int:
    config = load_fishing_config(DEFAULT_CONFIG_PATH)
    if not config["enabled"]:
        return 0
    yielded = yield_to_qyz("miniapp_fishing", time.time(), PROJECT_ROOT)
    if yielded is not None:
        print(f"fishing status=yielding_to_qyz reason={yielded['reason']}")
        return 0
    state_root = DEFAULT_STATE_ROOT
    global_path = state_root / "fishing_coordinator.json"
    now_ts = time.time()
    states, invalid = valid_states(state_root, config["accounts"])
    accounts = [account for account in config["accounts"] if account in states]
    counts = {account: int(states[account].get("daily_cast_count", 0) or 0) for account in accounts}
    limits = {account: account_options(config, account)["daily_cast_limit"] for account in config["accounts"]}
    eligible = eligible_accounts(accounts, states, now_ts=now_ts, limits=limits)
    if not eligible:
        incomplete = invalid + [account for account in accounts if not quota_done(states[account], limits[account])]
        if incomplete:
            retry_values = []
            for account in incomplete:
                try:
                    value = float(states.get(account, {}).get("retry_not_before_ts", 0) or 0)
                except (TypeError, ValueError):
                    value = 0
                if value > now_ts:
                    retry_values.append(value)
            retry_not_before = min(retry_values) if retry_values else now_ts
            _write_json(global_path, {
                "status": "rate_limited_wait",
                "runtime_version": FISHING_RUNTIME_VERSION,
                "updated_at": _utc_now(),
                "retry_not_before_ts": retry_not_before,
                "result": {"status": "upstream_backoff", "daily_cast_counts": counts},
                "accounts": config["accounts"],
                "trade": "disabled", **({"invalid_accounts": invalid} if invalid else {}),
            })
            print(f"fishing status=rate_limited_wait incomplete={','.join(incomplete)}")
            return 0
        _write_json(global_path, {
            "status": "daily_complete",
            "runtime_version": FISHING_RUNTIME_VERSION,
            "updated_at": _utc_now(),
            "result": {"status": "all_accounts_full", "daily_cast_counts": counts},
            "accounts": config["accounts"],
            "trade": "disabled",
        })
        print("fishing status=daily_complete all_accounts_full; no trade")
        return 0

    # One isolated protocol/session per account. No account waits for, buys from,
    # lists to, or transfers a rod to any other account.
    results = dict(asyncio.run(run_all(eligible, config)))
    for account in eligible:
        result = results[account]
        previous = states[account]
        result = merge_confirmed_cast_count(previous, result, limit=limits[account])
        result = apply_rate_limit_backoff(previous, result, now_ts=time.time())
        _write_json(state_root / "accounts" / account / "fishing.json", {
            "account": account,
            "updated_at": _utc_now(),
            "result": result,
        })

    updated_states, invalid = valid_states(state_root, config["accounts"])
    accounts = [account for account in config["accounts"] if account in updated_states]
    updated_counts = {account: int(updated_states[account].get("daily_cast_count", 0) or 0) for account in accounts}
    incomplete = invalid + [account for account in accounts if not quota_done(updated_states[account], limits[account])]
    retry_values = []
    for account in incomplete:
        try:
            retry_value = float(updated_states.get(account, {}).get("retry_not_before_ts", 0) or 0)
        except (TypeError, ValueError):
            retry_value = 0
        if retry_value > time.time():
            retry_values.append(retry_value)
    all_incomplete_waiting = bool(incomplete) and len(retry_values) == len(incomplete)
    coordinator_status = (
        "daily_complete" if not incomplete else
        "rate_limited_wait" if all_incomplete_waiting else
        "running"
    )
    coordinator = {
        "status": coordinator_status,
        "runtime_version": FISHING_RUNTIME_VERSION,
        "updated_at": _utc_now(),
        "result": {"status": "parallel_cycles_complete", "daily_cast_counts": updated_counts},
        "accounts": config["accounts"],
        "trade": "disabled", **({"invalid_accounts": invalid} if invalid else {}),
    }
    if all_incomplete_waiting:
        coordinator["retry_not_before_ts"] = min(retry_values)
    _write_json(global_path, coordinator)
    print(f"fishing parallel accounts={','.join(eligible)}")
    return 0


def main() -> int:
    try:
        with coordinator_lock(DEFAULT_STATE_ROOT):
            code = _run_cycle()
            try:
                _notify_if_due(DEFAULT_STATE_ROOT)
            except Exception as exc:  # noqa: BLE001 - a summary must never break the cycle
                print(f"fishing notification skipped: {type(exc).__name__}")
            return code
    except LockBusy:
        print("fishing coordinator already running; skip duplicate tick")
        return 0


def _notify_if_due(state_root: Path, *, now_ts: float | None = None, sender=None,
                   notifier_root: Path | None = None) -> dict | None:
    """Send the once-per-day all-accounts fishing summary through Hermes.

    Runs after the cycle inside the coordinator lock. It reads only the
    coordinator and per-account result files; a delivery failure is recorded
    in ``state/miniapp_fishing/notifications.json`` and retried next tick.
    An account that finishes after the day's summary went out (for example one
    added mid-day) gets its own once-only 补报.
    """
    import sys

    fishing_root = Path(__file__).resolve().parent
    project_root = fishing_root.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    try:
        from miniapp_shared.notify import Notifier
        from miniapp_shared.summaries import fishing_late_notification, fishing_notification
    except Exception as exc:  # noqa: BLE001 - notification is optional for the cycle
        print(f"fishing notification unavailable: {type(exc).__name__}")
        return None
    try:
        coordinator = json.loads((state_root / "fishing_coordinator.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(coordinator, dict):
        return None
    accounts = [item for item in (coordinator.get("accounts") or []) if isinstance(item, str)]
    rows = []
    for account in accounts:
        try:
            rows.append({"account": account, **account_result(state_root, account)})
        except ValueError:
            rows.append({"account": account, "status": "state_invalid"})
    now = time.time() if now_ts is None else float(now_ts)
    due = fishing_notification(coordinator, rows, now_ts=now, daily_limit=DAILY_CAST_LIMIT)
    if due is None:
        return None
    notifier = Notifier(Path(notifier_root or fishing_root), "miniapp_fishing", sender=sender)
    day = due[0].split(":", 1)[0]
    reported_path = notifier.path.parent / REPORTED_ACCOUNTS_FILE
    covered = accounts
    if notifier.delivered(due[0]):
        # The day's summary is out; report once each account it did not cover.
        late = fishing_late_notification(day, rows, _reported_accounts(reported_path, day, accounts),
                                         daily_limit=DAILY_CAST_LIMIT)
        if late is None:
            return {"status": "already_delivered", "key": due[0]}
        due, covered = late, late[0].split(":late:", 1)[1].split("+")
    outcome = notifier.send_once(*due)
    if outcome.get("status") == "delivered":
        _record_reported(reported_path, day, covered)
    print(f"fishing notification {outcome.get('status')} key={outcome.get('key')}")
    return outcome


# Accounts each day's summary (and any 补报) already reported. A day without a
# record predates this file and counts as fully reported.
REPORTED_ACCOUNTS_FILE = "reported_accounts.json"
REPORTED_DAYS_KEPT = 14


def _reported_accounts(path: Path, day: str, default: list[str]) -> set[str]:
    try:
        days = json.loads(path.read_text(encoding="utf-8")).get("days") or {}
    except (OSError, ValueError, AttributeError):
        days = {}
    reported = days.get(day) if isinstance(days, dict) else None
    return set(reported) if isinstance(reported, list) else set(default)


def _record_reported(path: Path, day: str, accounts: list[str]) -> None:
    try:
        days = json.loads(path.read_text(encoding="utf-8")).get("days") or {}
    except (OSError, ValueError, AttributeError):
        days = {}
    days = days if isinstance(days, dict) else {}
    days[day] = sorted(set(days.get(day) or []) | set(accounts))
    kept = dict(sorted(days.items())[-REPORTED_DAYS_KEPT:])
    _write_json(path, {"days": kept, "updated_at": _utc_now()})


if __name__ == "__main__":
    raise SystemExit(main())
