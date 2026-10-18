#!/bin/bash
set -euo pipefail

# Resume only the named authenticated sender's 斗法 flow after a server
# anti-repeat pause.  This never touches other accounts or non-斗法 flows.
if [[ "$#" -ne 1 ]]; then
  echo "Usage: $0 <tg9100000005|tg9100000003|tg9100000004|tg9100000002|tg9100000006>" >&2
  exit 64
fi

case "$1" in
  tg9100000005|tg9100000003|tg9100000004|tg9100000002|tg9100000006) account="$1" ;;
  *)
    echo "Unsupported 斗法 account: $1" >&2
    exit 64
    ;;
esac

cd "$(dirname "${BASH_SOURCE[0]}")/.."
pause_path="state/accounts/${account}/doufa_demo_player_02/pause_until.json"
quota_path="state/accounts/${account}/doufa_demo_player_02/daily_quota.json"
quota_status="$(./.venv/bin/python3 - "$quota_path" <<'PY'
import json, sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
path = Path(sys.argv[1])
today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    data = {}
sent_count = int(data.get("sent_count") or 0) if data.get("date") == today else 0
limit = min(5, max(0, int(data.get("limit") or 5)))
print(f"{sent_count} {limit}")
PY
)"
read -r sent_count limit <<<"$quota_status"
if (( sent_count >= limit )); then
  echo "Not resumed: ${account}/doufa_demo_player_02 daily quota is ${sent_count}/${limit}."
  exit 0
fi
if [[ -f "$pause_path" ]]; then
  rm -f "$pause_path"
  echo "Resumed ${account}/doufa_demo_player_02 under quota ${sent_count}/${limit}; a manual or later rotation request may continue."
else
  echo "${account}/doufa_demo_player_02 was not paused and remains under quota ${sent_count}/${limit}."
fi
