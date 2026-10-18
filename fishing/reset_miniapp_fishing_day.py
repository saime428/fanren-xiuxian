#!/usr/bin/env python3
"""Reset the daily fishing quota at Asia/Shanghai midnight."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from miniapp_fishing_integration import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_STATE_ROOT,
    _utc_now,
    _write_json,
    load_fishing_config,
)
from run_miniapp_fishing_cycle import coordinator_lock
from miniapp_shared.state import day_key


def archive_json_evidence(state_root: Path, day: str) -> None:
    history = state_root / "history" / day
    history.mkdir(parents=True, exist_ok=True)
    if (state_root / "fishing_coordinator.json").is_file():
        shutil.copy2(state_root / "fishing_coordinator.json", history / "fishing_coordinator.json")
    for source in (state_root / "accounts").glob("*/fishing.json"):
        target = history / "accounts" / source.parent.name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target / source.name)


def _reset_day() -> int:
    config = load_fishing_config(DEFAULT_CONFIG_PATH)
    if not config["enabled"]:
        print("fishing daily reset: disabled")
        return 0
    state_root = DEFAULT_STATE_ROOT
    day = day_key()
    marker = state_root / "daily_reset.json"
    previous = json.loads(marker.read_text()) if marker.exists() else {}
    if previous.get("day") == day:
        print(f"fishing daily reset: already_reset day={day}")
        return 0
    # Every configured account runs its own independent MiniApp fishing cycle;
    # rod-market operations (listing, purchase, transfer) are retired.
    archive_json_evidence(state_root, day)
    for account in config["accounts"]:
        path = state_root / "accounts" / account / "fishing.json"
        if not path.exists():
            continue
        try:
            result = json.loads(path.read_text()).get("result") or {}
            carried = float(result.get("retry_not_before_ts") or 0) > time.time()
        except (OSError, ValueError, TypeError, AttributeError):
            # Like run_miniapp_fishing_cycle.valid_states(): a damaged
            # file stops only its account, untouched for the operator.
            print(f"fishing daily reset: invalid state account={account}; skipped")
            continue
        if result.get("settlement_date") == day:
            # A cast finishing across midnight already belongs to
            # today; the delayed reset must preserve its receipt.
            continue
        if carried:
            # Same day rollover as run_miniapp_fishing_cycle.account_result().
            result.update(daily_cast_count=0, counted_settlement_ids=[], settlement_date=day,
                          server_daily_limit_reached=False, catches=[])
            _write_json(path, {"account": account, "updated_at": _utc_now(), "result": result})
            continue
        path.unlink(missing_ok=True)
    _write_json(marker, {"day": day, "updated_at": _utc_now(), "accounts": config["accounts"], "trade": "disabled"})
    _write_json(state_root / "fishing_coordinator.json", {
        "status": "armed",
        "updated_at": _utc_now(),
        "result": {"status": "parallel_daily_reset", "daily_cast_count": 0},
        "accounts": config["accounts"],
        "trade": "disabled",
    })
    print(f"fishing daily reset: day={day} accounts={len(config['accounts'])} trade=disabled")
    return 0


def main() -> int:
    with coordinator_lock(DEFAULT_STATE_ROOT, blocking=True):
        return _reset_day()


if __name__ == "__main__":
    raise SystemExit(main())
