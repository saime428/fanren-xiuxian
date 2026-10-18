#!/bin/bash
set -euo pipefail

# One explicit activation runs a complete duel campaign. It preserves the
# global five-minute cadence and account rotation. A rolling 24-hour rejection
# pauses only that account; unrelated eligible accounts continue.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DOUFA_ROTATION_STATE_PATH="state/doufa_demo_player_02_rotation.json"
export DOUFA_ROTATION_LOCK_PATH="state/doufa_demo_player_02_rotation.lock"
export DOUFA_GLOBAL_INTERVAL_SECONDS='300'
accounts=(tg9100000005 tg9100000003 tg9100000004 tg9100000002 tg9100000006)

# flock on an fd this shell keeps open: released by the kernel however the
# dispatcher dies, so a killed campaign never leaves a stale lock behind.
exec 9>>"$DOUFA_ROTATION_LOCK_PATH"
if ! ./.venv/bin/python3 -c 'import fcntl, sys
try: fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError: sys.exit(1)'; then
  echo '斗法 dynamic rotation: dispatcher lock busy; skip'
  exit 0
fi
# One activation is one Asia/Shanghai day's campaign. Local quotas reset at
# midnight but the server limit is a rolling 24 hours, so a campaign still
# running after midnight must stop instead of spending the next day's quota.
DOUFA_CAMPAIGN_DATE="$(./.venv/bin/python3 -c 'from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo("Asia/Shanghai")).date())')"
export DOUFA_CAMPAIGN_DATE

while true; do
  selection="$(./.venv/bin/python3 ./scripts/doufa_rotation_select.py "$DOUFA_ROTATION_STATE_PATH" "$DOUFA_GLOBAL_INTERVAL_SECONDS" "${accounts[@]}")"
  action="$(./.venv/bin/python3 - "$selection" <<'PY'
import json, sys
print(json.loads(sys.argv[1])['action'])
PY
)"

  case "$action" in
    wait)
      remaining="$(./.venv/bin/python3 - "$selection" <<'PY'
import json, sys
print(max(1, int(json.loads(sys.argv[1]).get('remaining') or 1)))
PY
)"
      echo "斗法 campaign: global 5-minute interval; waiting ${remaining}s"
      sleep "$remaining"
      ;;
    exhausted)
      reason="$(./.venv/bin/python3 - "$selection" <<'PY'
import json, sys
print(json.loads(sys.argv[1]).get('reason') or 'all accounts paused or at 5/5')
PY
)"
      echo "斗法 campaign complete: ${reason}"
      exit 0
      ;;
    send)
      account="$(./.venv/bin/python3 - "$selection" <<'PY'
import json, sys
print(json.loads(sys.argv[1])['account'])
PY
)"
      next_index="$(./.venv/bin/python3 - "$selection" <<'PY'
import json, sys
print(json.loads(sys.argv[1])['next_index'])
PY
)"
      before_ts="$(./.venv/bin/python3 - "$account" <<'PY'
import json, sys
from pathlib import Path
p = Path('state/accounts') / sys.argv[1] / 'doufa_demo_player_02' / 'last_attempt.json'
try: print(float(json.loads(p.read_text(encoding='utf-8')).get('actual_send_ts') or 0))
except Exception: print(0)
PY
)"
      echo "斗法 campaign: selected account=${account}; min global interval=300s"
      # A failing account must not end the campaign for the others; the guard
      # below still records this turn, so cadence and rotation hold.
      send_rc=0
      bash "./scripts/send_doufa_${account}.sh" || send_rc=$?
      if (( send_rc != 0 )); then
        echo "斗法 campaign: sender failed for account=${account} exit=${send_rc}; continuing with the rotation"
      fi
      stop_reason="$(./.venv/bin/python3 - "$DOUFA_ROTATION_STATE_PATH" "$account" "$next_index" "$before_ts" <<'PY'
import json, sys, time
from pathlib import Path
state_path = Path(sys.argv[1]); account = sys.argv[2]; next_index = int(sys.argv[3]); before = float(sys.argv[4])
p = Path('state/accounts') / account / 'doufa_demo_player_02' / 'last_attempt.json'
try: attempt = json.loads(p.read_text(encoding='utf-8'))
except Exception: attempt = {}
after = float(attempt.get('actual_send_ts') or 0)
recorded = after if after > before else time.time()
state = {
    'last_actual_send_ts': recorded,
    'last_account': account,
    'next_index': next_index,
    'updated_at_ts': time.time(),
    'sender_state_advanced': after > before,
}
state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
if attempt.get("duel_anti_repeat") is True:
    print('anti_repeat')
else:
    print('continue')
PY
)"
      echo "斗法 campaign recorded dispatch guard: account=${account}; next_index=${next_index}; result=${stop_reason}"
      if [[ "$stop_reason" == 'anti_repeat' ]]; then
        echo "斗法 campaign skips paused account after 24-hour anti-repeat: account=${account}"
      fi
      ;;
  esac
done
