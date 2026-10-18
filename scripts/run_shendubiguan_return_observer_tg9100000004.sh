#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000004.env
export XIU_FLOW_SLUG='shendubiguan'
source ./scripts/lib/account_flow_paths.sh

export TG_FLOW_SLUG='shendubiguan'
export TG_OBSERVER_FLOW_SLUG='shendubiguan_observer'
export TG_SESSION_NAME="${TG_STATE_DIR}/observer.session"
export TG_SCAN_LIMIT='200'
export TG_TRIGGER_MAX_EVENT_AGE_SECONDS='21600'
export TG_RUN_MAX_SECONDS='120'
export TG_NO_EVENT_LOG_INTERVAL_SECONDS='600'
export TG_OBSERVER_PERSISTENT='1'
export TG_OBSERVER_RECONCILE_SECONDS='60'
export TG_SEQUENCE_SEND_WRAPPER='./scripts/send_shendubiguan_tg9100000004.sh'
export TG_TRIGGER_TARGET_USERNAME='demo_player_04'
export TG_RETURN_MARKER_TEXT='深度闭关总结|深度閉關總結|你并未处于深度闭关之中|你並未處於深度閉關之中'
# Some shendubiguan return summaries are emitted in the root chat instead of the configured topic.
# Still require trusted sender ID + exact @username + return-summary markers before triggering.
export TG_RETURN_ALLOW_ROOT_TOPIC_EVENTS='1'
export TG_OBSERVER_STATE_PATH="${TG_STATE_DIR}/observer_last_seen.json"
export TG_OBSERVER_LOCK_PATH="${TG_STATE_DIR}/observer.lock"
export TG_SEQUENCE_LOCK_PATH="${TG_LOCK_PATH}"

xiu_ensure_session_authorized
exec ./.venv/bin/python3 observe_return.py
