# Shared body for the .元婴出窍 send flow. Wrappers source this after setting
# the account env and XIU_FLOW_SLUG and sourcing account_flow_paths.sh.
# The registry's MiniApp route runs first and ends the wrapper; the group
# command path below is the fallback channel (MiniApp exit code 3 only).
xiu_route_miniapp_command

export TG_MESSAGE='.元婴出窍'
export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
export TG_MIN_REPEAT_SECONDS="${TG_MIN_REPEAT_SECONDS:-28800}"
export TG_PRE_REPEAT_PROBE_SECONDS="${TG_PRE_REPEAT_PROBE_SECONDS:-150}"
export TG_COOLDOWN_RETRY_MAX_WAIT_SECONDS='43200'
export TG_COOLDOWN_INLINE_WAIT_SAFETY_MARGIN_SECONDS='5'
export TG_COOLDOWN_MARKER_TEXT='远航尚未结束|遠航尚未結束|请在|請在|后再来接引|後再來接引|后再来|後再來|冷却中|冷卻中|还不能|還不能|剩余|剩餘|你的元婴正在执行|你的元嬰正在執行|元神出窍|元神出竅|无法分身|無法分身|请先使用 .元婴归窍|請先使用 .元嬰歸竅'
export TG_SUCCESS_MARKER_TEXT='冷却时间被重置|冷卻時間被重置|可以立刻再次闭关|可以立刻再次接引|毫无疲惫之感|毫無疲憊之感|你心念一动|你心念一動|元婴化作一道流光飞出|元嬰化作一道流光飛出|将在外云游 8 小时|將在外雲遊 8 小時|下一次发言时若已归来|下一次發言時若已歸來'
export TG_ACK_MARKER_TEXT='你的元婴正在执行|你的元嬰正在執行|元神出窍|元神出竅|无法分身|無法分身|请先使用 .元婴归窍|請先使用 .元嬰歸竅|尚未凝聚元婴|尚未凝聚元嬰|无法施展此术|無法施展此術'
export TG_DISABLE_IMMEDIATE_RESEND='1'
export TG_STATUS_PROBE_MESSAGE='.元婴状态'
export TG_STATUS_PROBE_PATH="${TG_STATE_DIR}/status_probe.json"
export TG_STATUS_PROBE_CACHE_GRACE_SECONDS='60'
export TG_STATUS_PROBE_WAIT_SECONDS='3'
export TG_STATUS_PROBE_SCAN_LIMIT='40'
export TG_STATUS_PROBE_IN_PROGRESS_MARKERS='你的元婴正在执行|你的元嬰正在執行|元神出窍|元神出竅|无法分身|無法分身|请先使用 .元婴归窍|請先使用 .元嬰歸竅'
export TG_STATUS_PROBE_CLEAR_MARKERS='已归窍|已歸竅|当前没有进行中的元婴任务|當前沒有進行中的元嬰任務|空闲|空閒|可再次出窍|可再次出竅|窍中温养|竅中溫養|使用 .元婴出窍 或 .元婴闭关 派遣元婴|使用 .元嬰出竅 或 .元嬰閉關 派遣元嬰'
export TG_NOMINAL_COOLDOWN_SECONDS='28800'
export TG_PENDING_SENT_MAX_AGE_SECONDS="${TG_PENDING_SENT_MAX_AGE_SECONDS:-0}"

# Resume the exact unconfirmed command before a status probe can skip it.
if [[ "${TG_RECONCILE_ONLY:-0}" == "1" ]] || ./.venv/bin/python3 command_confirmation.py --pending "${TG_STATE_DIR}"; then
  xiu_ensure_session_authorized
  exec ./.venv/bin/python3 send_topic_message.py
fi

if [[ "${TG_OBSERVER_IMMEDIATE_KICK:-0}" == "1" ]]; then
  echo "Observer immediate kick for ${TG_MESSAGE}; bypassing status probe/cache and sending now"
  export TG_MIN_REPEAT_SECONDS='0'
  export TG_PRE_REPEAT_PROBE_SECONDS='0'
  xiu_ensure_session_authorized
  exec ./.venv/bin/python3 send_topic_message.py
fi

wait_seconds="$(./.venv/bin/python3 command_confirmation.py --cooldown-remaining status_probe "${TG_STATUS_PROBE_MESSAGE}")"
if [[ -n "${wait_seconds:-}" ]]; then
  echo "Status probe cache says ${TG_MESSAGE} still cooling down; skip this run (${wait_seconds}s remaining)"
  exit 0
fi

xiu_ensure_session_authorized
if ./.venv/bin/python3 probe_yuanyingchuqiao_status.py; then
  export TG_MIN_REPEAT_SECONDS='0'
  export TG_PRE_REPEAT_PROBE_SECONDS='0'
  exec ./.venv/bin/python3 send_topic_message.py
else
  code=$?
  if [[ "$code" == "10" ]]; then
    echo "Status probe shows ${TG_MESSAGE} still active; skip send this run"
    exit 0
  fi
  if [[ "$code" == "11" ]]; then
    echo "Status probe got no trustworthy reply; skip send this run"
    exit 0
  fi
  if [[ "$code" == "12" ]]; then
    echo "Status probe says ${TG_MESSAGE} is blocked for this account right now; skip send this run"
    exit 0
  fi
  exit "$code"
fi
