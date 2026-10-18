# Shared body for the shendubiguan deep-retreat send flow.
# Wrappers source this after setting the account env and XIU_FLOW_SLUG.
# Follow the yuanying pattern: honor cached countdown only as a coarse wait,
# then force a fresh .查看闭关 probe/action decision in Python.
xiu_route_miniapp_command
export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
export TG_SHENDUBIGUAN_BYPASS_MARKER="${TG_SHENDUBIGUAN_BYPASS_MARKER:-observer_immediate_kick_bypass_cache}"
export TG_MIN_REPEAT_SECONDS="${TG_MIN_REPEAT_SECONDS:-28800}"
export TG_PRE_REPEAT_PROBE_SECONDS="${TG_PRE_REPEAT_PROBE_SECONDS:-300}"
export TG_RUN_MAX_SECONDS="${TG_RUN_MAX_SECONDS:-150}"
export TG_STATUS_REPLY_TIMEOUT_SECONDS="${TG_STATUS_REPLY_TIMEOUT_SECONDS:-25}"
export TG_ACTION_REPLY_TIMEOUT_SECONDS="${TG_ACTION_REPLY_TIMEOUT_SECONDS:-25}"
export TG_REPLY_POLL_SECONDS="${TG_REPLY_POLL_SECONDS:-2}"
export TG_SCAN_LIMIT="${TG_SCAN_LIMIT:-80}"
export TG_WATCHDOG_MAX_AGE_SECONDS="${TG_WATCHDOG_MAX_AGE_SECONDS:-30000}"
export TG_COUNTDOWN_INLINE_WAIT_MAX_SECONDS="${TG_COUNTDOWN_INLINE_WAIT_MAX_SECONDS:-300}"
export TG_COUNTDOWN_INLINE_WAIT_SAFETY_MARGIN_SECONDS="${TG_COUNTDOWN_INLINE_WAIT_SAFETY_MARGIN_SECONDS:-8}"
export TG_STATUS_PROBE_PATH="${TG_STATUS_PROBE_PATH:-${TG_STATE_DIR}/status_probe.json}"
export TG_STATUS_PROBE_CACHE_GRACE_SECONDS="${TG_STATUS_PROBE_CACHE_GRACE_SECONDS:-60}"

wait_seconds=''
# Observer/manual immediate kicks set TG_MIN_REPEAT_SECONDS=0; in that path we
# must bypass cached coarse waiting and force a fresh .查看闭关 probe now.
if [[ "${TG_MIN_REPEAT_SECONDS}" -gt 0 ]]; then
  wait_seconds="$(./.venv/bin/python3 command_confirmation.py --cooldown-remaining status_probe '.查看闭关' '.深度闭关')"
fi
if [[ -n "${wait_seconds:-}" ]]; then
  echo "Status probe cache says ${TG_FLOW_SLUG} still cooling down; skip this run (${wait_seconds}s remaining)"
  exit 0
fi

if [[ "${TG_MIN_REPEAT_SECONDS:-28800}" -le 0 ]]; then
  echo "${TG_SHENDUBIGUAN_BYPASS_MARKER}=yes flow=${TG_FLOW_SLUG} source=observer_or_manual_kick"
else
  echo "${TG_SHENDUBIGUAN_BYPASS_MARKER}=no flow=${TG_FLOW_SLUG} source=scheduled_or_watchdog"
fi

xiu_ensure_session_authorized
# After the coarse cache wait, always force a fresh .查看闭关 decision so a
# real "你并未处于深度闭关之中" reply can immediately trigger .深度闭关.
export TG_MIN_REPEAT_SECONDS='0'
export TG_PRE_REPEAT_PROBE_SECONDS='0'
exec ./.venv/bin/python3 send_shendubiguan.py
