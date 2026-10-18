#!/bin/bash
set -euo pipefail

: "${XIU_ACCOUNT_SLUG:?missing XIU_ACCOUNT_SLUG}"
: "${XIU_FLOW_SLUG:?missing XIU_FLOW_SLUG}"
: "${TG_MESSAGE:?missing TG_MESSAGE}"

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source ./scripts/lib/account_flow_paths.sh

export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
# The dispatcher owns cross-account cadence; each account may participate again
# after the same global 5-minute guard, while the selector prevents consecutive
# dispatches from the same account.
export TG_MIN_REPEAT_SECONDS='300'
export TG_PRE_REPEAT_PROBE_SECONDS='0'
export TG_DISABLE_IMMEDIATE_RESEND='1'
export TG_ACK_ANY_REPLY='0'
export TG_REQUIRE_ACK_REPLY='1'
export TG_POST_SEND_WAIT_SECONDS='8'
export TG_POST_SEND_MAX_WAIT_SECONDS='120'
export TG_POST_SEND_POLL_SECONDS='5'
export TG_SCAN_LIMIT='80'
export TG_LATE_ACK_SCAN_LIMIT='120'
# The game's "24小时内已交锋过多" receipt is the authoritative hard stop.
# Keep the local quota aligned at five successful duels per account/day so the
# regular rotation never probes a known exhausted account.
export DOUFA_DAILY_LIMIT='5'
export DOUFA_INTERVAL_SECONDS='300'
# If launchd fires a few seconds early relative to the per-account repeat
# guard, wait through that tiny gap instead of dropping the whole rotation.
export DOUFA_DEFER_MAX_SECONDS='90'
export DOUFA_TIMEZONE='Asia/Shanghai'
export DOUFA_QUOTA_PATH="${TG_STATE_DIR}/daily_quota.json"
export DOUFA_QUOTA_LOCK="${TG_STATE_DIR}/daily_quota.lock"

mkdir -p "${TG_STATE_DIR}"

# A server-confirmed anti-repeat response stops automatic retries for only this
# account+斗法 flow.  A later explicit Boss request may remove this marker if the
# same account remains below its hard local quota of 5/5.
DOUFA_PAUSE_PATH="${DOUFA_PAUSE_PATH:-${TG_STATE_DIR}/pause_until.json}"
export DOUFA_PAUSE_PATH
if [[ -f "${DOUFA_PAUSE_PATH}" ]]; then
  echo "斗法 manually paused after anti-repeat for account=${XIU_ACCOUNT_SLUG}; explicit resume required: ${DOUFA_PAUSE_PATH}"
  exit 0
fi

# Held on fd 8 for the rest of this run and released by the kernel on any
# exit: a waiter that times out cannot remove another run's lock, and a
# killed run leaves no stale lock.
exec 8>>"${DOUFA_QUOTA_LOCK}"
if ! ./.venv/bin/python3 -c 'import fcntl, sys, time
for _ in range(120):
    try:
        fcntl.flock(8, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except BlockingIOError:
        time.sleep(1)
sys.exit(75)'; then
  echo "斗法配额锁等待超时: ${DOUFA_QUOTA_LOCK}" >&2
  exit 75
fi

quota_json="$(./.venv/bin/python3 - "${DOUFA_QUOTA_PATH}" "${DOUFA_DAILY_LIMIT}" <<'PY'
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

path = Path(sys.argv[1])
limit = int(sys.argv[2])
today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    data = {}
if data.get("date") != today:
    data = {"date": today, "sent_count": 0, "limit": limit, "reset_reason": "new Asia/Shanghai calendar day"}
else:
    data["limit"] = limit
    data["sent_count"] = max(0, int(data.get("sent_count", 0)))
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(data, ensure_ascii=False))
PY
)"
sent_count="$(./.venv/bin/python3 - "${quota_json}" <<'PY'
import json, sys
print(int(json.loads(sys.argv[1]).get("sent_count", 0)))
PY
)"
if (( sent_count >= DOUFA_DAILY_LIMIT )); then
  echo "斗法 daily quota reached: ${sent_count}/${DOUFA_DAILY_LIMIT}; no send"
  exit 0
fi

# launchd can fire just before a 30-minute repeat guard expires.  Defer short
# collisions in-process, preserving the account's next turn without allowing a
# broad retry or a second message.
remaining_seconds="$(./.venv/bin/python3 - "${TG_LAST_ATTEMPT_PATH}" "${TG_MIN_REPEAT_SECONDS}" <<'PY'
import json, sys, time
from pathlib import Path
try:
    attempt = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    attempt = {}
last = float(attempt.get("actual_send_ts") or attempt.get("ts") or 0)
print(max(0, int(float(sys.argv[2]) - (time.time() - last))))
PY
)"
if (( remaining_seconds > 0 )); then
  if (( remaining_seconds <= DOUFA_DEFER_MAX_SECONDS )); then
    echo "斗法 automatic defer: account=${XIU_ACCOUNT_SLUG}; waiting ${remaining_seconds}s for repeat guard"
    sleep "${remaining_seconds}"
  else
    echo "斗法 repeat guard has ${remaining_seconds}s remaining; keep next scheduled rotation"
    exit 0
  fi
fi

echo "斗法 quota before send: ${sent_count}/${DOUFA_DAILY_LIMIT}; account=${XIU_ACCOUNT_SLUG}; message=${TG_MESSAGE}"
xiu_ensure_session_authorized
set +e
./.venv/bin/python3 send_topic_message.py
send_rc=$?
set -e

# The quota and the anti-repeat pause below are persisted independently: a
# failure in one must not skip the other (an unrecorded anti-repeat pause
# would be sent again by the next rotation turn).
# 今日神念 is authoritative when included in the receipt.  Some valid duel
# outcome replies omit it, so a fresh, account-attributed acknowledged outcome
# consumes exactly one local slot; transport-only sends never do.
quota_rc=0
count_result="$(./.venv/bin/python3 - "${DOUFA_QUOTA_PATH}" "${TG_STATE_DIR}/last_result.json" "${XIU_ACCOUNT_SLUG}" <<'PYCODE'
import json, sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from doufa_receipts import apply_receipt
from state_store import load_json_dict, persist_json_atomic
path = Path(sys.argv[1])
receipt = load_json_dict(Path(sys.argv[2]))
today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
quota = apply_receipt(load_json_dict(path), receipt, account=sys.argv[3], today=today)
persist_json_atomic(path, quota)
if receipt.get("duel_anti_repeat"):
    print(f"斗法 quota unchanged after anti-repeat: {quota.get('sent_count', 0)}/5; anti_repeat_paused")
else:
    print(f"斗法 quota from official settlement: {quota.get('sent_count', 0)}/5; daily_mind_remaining={receipt.get('daily_mind_remaining')}")
PYCODE
)" || quota_rc=$?
printf '%s\n' "${count_result}"

# Persist an account-scoped manual pause only from the exact direct server reply
# classified by send_topic_message.py. This rejection does not consume quota.
pause_rc=0
./.venv/bin/python3 - "${TG_LAST_ATTEMPT_PATH}" "${DOUFA_PAUSE_PATH}" "${XIU_ACCOUNT_SLUG}" <<'PY' || pause_rc=$?
import json, sys, time
from pathlib import Path
attempt_path, pause_path, account = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
try:
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
except Exception:
    attempt = {}
if attempt.get("duel_anti_repeat") is True:
    payload = {
        "mode": "manual_resume_required",
        "account": account,
        "scope": f"{account}:doufa_demo_player_02:@example_game_group:7310786",
        "reason": "anti-repeat reply: 交锋过多 / 暂不可再次斗法",
        "reply_message_id": attempt.get("ack_reply_message_id"),
        "reply_text": attempt.get("ack_reply_text") or "",
        "saved_at_ts": time.time(),
    }
    temporary = pause_path.with_suffix(pause_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(pause_path)
    print(f"斗法 anti-repeat pause persisted: account={account}; path={pause_path}")
PY
# Preserve transport result for observability, but a proven send-no-ack is not retried immediately.
if (( send_rc != 0 )); then
  echo "斗法 sender exit=${send_rc}; no immediate resend"
fi
if (( quota_rc != 0 || pause_rc != 0 )); then
  echo "斗法 receipt bookkeeping failed: quota_exit=${quota_rc} pause_exit=${pause_rc}; account=${XIU_ACCOUNT_SLUG}" >&2
  exit 1
fi
exit 0
