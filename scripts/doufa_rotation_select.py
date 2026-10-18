#!/usr/bin/env python3
"""Select an unpaused, sub-quota duel account for automatic rotation."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def main() -> int:
    state_path = Path(sys.argv[1])
    interval = int(sys.argv[2])
    accounts = sys.argv[3:]
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    now = time.time()
    last = float(state.get("last_actual_send_ts") or 0)
    last_account = str(state.get("last_account") or "")
    # Recover the global cadence and last-sender guard from authoritative
    # account-scoped attempts too. This keeps an activation safe if the
    # dispatcher was killed after Telegram sent but before rotation state was
    # committed, or if rotation state is otherwise stale/corrupt.
    for account in accounts:
        attempt_path = Path("state/accounts") / account / "doufa_demo_player_02" / "last_attempt.json"
        try:
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            attempt_ts = float(attempt.get("actual_send_ts") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            attempt_ts = 0
        if attempt_ts > last:
            last = attempt_ts
            last_account = account
    remaining = max(0, int(interval - (now - last)))
    if remaining:
        print(json.dumps({"action": "wait", "remaining": remaining}, ensure_ascii=False))
        return 0
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    campaign_date = os.environ.get("DOUFA_CAMPAIGN_DATE", "")
    if campaign_date and campaign_date != today:
        # Never roll into the next day's local quota inside one activation.
        print(json.dumps({"action": "exhausted", "reason": f"campaign day {campaign_date} ended"}, ensure_ascii=False))
        return 0
    start = int(state.get("next_index", 0)) % len(accounts)
    for offset in range(len(accounts)):
        index = (start + offset) % len(accounts)
        account = accounts[index]
        if account == last_account:
            continue
        base = Path("state/accounts") / account / "doufa_demo_player_02"
        try:
            quota = json.loads((base / "daily_quota.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            quota = {}
        sent = int(quota.get("sent_count") or 0) if quota.get("date") == today else 0
        limit = min(5, max(0, int(quota.get("limit") or 5)))
        pause_path = base / "pause_until.json"
        if pause_path.exists():
            print(f"automatic_rotation_paused account={account} quota={sent}/{limit}", file=sys.stderr)
            continue
        if sent < limit:
            print(json.dumps({"action": "send", "account": account, "next_index": (index + 1) % len(accounts)}, ensure_ascii=False))
            return 0
    print(json.dumps({"action": "exhausted"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
