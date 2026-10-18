#!/bin/bash
set -euo pipefail

if [[ "$#" -ne 4 ]]; then
  echo "Usage: $0 <business-account> <credential-source> <username> <reply-command>" >&2
  exit 64
fi

account="$1"
source_slug="$2"
username="$3"
reply_command="$4"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source "./config/accounts/${source_slug}.env"
export XIU_ACCOUNT_SLUG="$account"
export XIU_CREDENTIAL_SOURCE="$source_slug"
export XIU_FLOW_SLUG='jiyin_choice'
source ./scripts/lib/account_flow_paths.sh
export TG_SESSION_FALLBACK_NAME="${TG_SESSION_FALLBACK_NAME:-state/${source_slug}}"
export TG_MONITOR_TARGETS="${TG_MONITOR_TARGETS:-${XIU_GAME_TARGET}|${XIU_GAME_SECOND_CHAT_ID}}"
export TG_MONITOR_ROOT_TARGETS="${TG_MONITOR_ROOT_TARGETS:-${XIU_GAME_SECOND_CHAT_ID}}"
export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
export TG_AUTO_REPLY_MODE='fixed'
export TG_AUTO_REPLY_MESSAGE="$reply_command"
export TG_TRIGGER_MARKER_TEXT='回复本消息|.献上魂魄|.收敛气息'
export TG_TRIGGER_TRUSTED_SENDER_IDS='7228205882,7965897083,8735907987,8816935632,8918867302,8944702077,8964348409,8980154525,8788895566,8917921351,8388633812,8861328042'
export TG_TRIGGER_TARGET_USERNAME="$username"
export TG_TOPIC_MATCH_MODE='any'
export TG_REPLY_TO_EVENT='1'
export TG_SCAN_LIMIT='300'
export TG_LOG_SKIPS='0'
# The official split choice states 180 minutes. The monitor also applies any
# shorter deadline printed in the individual question, from its original date.
export TG_TRIGGER_MAX_EVENT_AGE_SECONDS='10800'
export TG_RUN_MAX_SECONDS='30'
export TG_AUTO_REPLY_PERSISTENT='1'
export TG_AUTO_REPLY_RECONCILE_SECONDS='10'
# A completed no-event scan is positive recovery evidence after a transport
# failure. Keep it visible to the health collector for this persistent flow.
export TG_LOG_IDLE='1'
export PYTHONUNBUFFERED='1'
xiu_ensure_session_authorized
exec ./.venv/bin/python3 -u auto_reply_topic_event.py
