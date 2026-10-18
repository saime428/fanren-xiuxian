#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000002.env
export XIU_FLOW_SLUG='yuanyingchuqiao'
source ./scripts/lib/account_flow_paths.sh

export TG_FLOW_SLUG='yuanyingchuqiao'
export TG_OBSERVER_FLOW_SLUG='yuanyingchuqiao_observer'
export TG_SESSION_NAME="${TG_STATE_DIR}/observer.session"
export TG_SCAN_LIMIT="${TG_SCAN_LIMIT:-1000}"
export TG_TRIGGER_MAX_EVENT_AGE_SECONDS='21600'
export TG_RUN_MAX_SECONDS='120'
export TG_OBSERVER_PERSISTENT='1'
export TG_OBSERVER_RECONCILE_SECONDS='60'
export TG_SEQUENCE_SEND_WRAPPER='./scripts/send_yuanyingchuqiao_tg9100000002.sh'
export TG_TRIGGER_TARGET_USERNAME='demo_player_02'
export TG_RETURN_MARKER_TEXT='元神归窍总结|你的元婴在虚空中神游八小时'
# Some yuanying return summaries are emitted in the root chat instead of the configured topic.
# Still require trusted sender ID + exact @username + return-summary markers before triggering.
export TG_RETURN_ALLOW_ROOT_TOPIC_EVENTS='1'
export TG_OBSERVER_KICK_RESET_PENDING='1'
export TG_OBSERVER_STATE_PATH="${TG_STATE_DIR}/observer_last_seen.json"
export TG_OBSERVER_LOCK_PATH="${TG_STATE_DIR}/observer.lock"
export TG_SEQUENCE_LOCK_PATH="${TG_LOCK_PATH}"

xiu_ensure_session_authorized
exec ./.venv/bin/python3 observe_return.py
