#!/usr/bin/env python3
"""Clear account-scoped duel pauses for an explicit under-quota campaign."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ACCOUNTS = (
    "tg9100000005",
    "tg9100000003",
    "tg9100000004",
    "tg9100000002",
    "tg9100000006",
)
FLOW = "doufa_demo_player_02"
HARD_LIMIT = 5


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    for account in ACCOUNTS:
        base = Path("state/accounts") / account / FLOW
        pause_path = base / "pause_until.json"
        if not pause_path.exists():
            continue
        quota = load_json(base / "daily_quota.json")
        sent = int(quota.get("sent_count") or 0) if quota.get("date") == today else 0
        limit = min(HARD_LIMIT, max(0, int(quota.get("limit") or HARD_LIMIT)))
        if sent >= limit:
            print(f"斗法 explicit campaign keeps pause at quota: account={account} quota={sent}/{limit}")
            continue
        pause_path.unlink()
        print(f"斗法 explicit campaign cleared account pause: account={account} quota={sent}/{limit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
