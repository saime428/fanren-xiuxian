# Shared body for the .探寻裂缝 send flow. Wrappers source this after setting
# the account env and XIU_FLOW_SLUG and sourcing account_flow_paths.sh.
export TG_MESSAGE='.探寻裂缝'
export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
export TG_MIN_REPEAT_SECONDS='43050'
export TG_PRE_REPEAT_PROBE_SECONDS='150'
export TG_COOLDOWN_RETRY_MAX_WAIT_SECONDS='43200'
export TG_COOLDOWN_INLINE_WAIT_SAFETY_MARGIN_SECONDS='5'
export TG_COOLDOWN_MARKER_TEXT='远航尚未结束|遠航尚未結束|请在|請在|后再来接引|後再來接引|后再来|後再來|冷却中|冷卻中|还不能|還不能|剩余|剩餘'
export TG_SUCCESS_MARKER_TEXT='冷却时间被重置|冷卻時間被重置|可以立刻再次闭关|可以立刻再次接引|毫无疲惫之感|毫無疲憊之感|【探寻成功】|【探尋成功】|满载而归|滿載而歸|意外之喜|法则碎片|法則碎片'
export TG_SETTLEMENT_MARKER_TEXT='遭遇风暴|遭遇風暴|被迫逃回|元气大伤|元氣大傷|修为倒退|修為倒退'
export TG_DISABLE_IMMEDIATE_RESEND='1'

if [[ "${TG_RECONCILE_ONLY:-0}" == "1" ]] || ./.venv/bin/python3 command_confirmation.py --pending "${TG_STATE_DIR}"; then
  xiu_ensure_session_authorized
  exec ./.venv/bin/python3 send_topic_message.py
fi

wait_seconds="$(./.venv/bin/python3 command_confirmation.py --cooldown-remaining cooldown_retry)"
if [[ -n "${wait_seconds:-}" ]]; then
  echo "Cooldown retry pending for ${TG_TARGET}|${TG_TOPIC_ID}|${TG_MESSAGE}; skip this run (${wait_seconds}s remaining)"
  exit 0
fi

xiu_ensure_session_authorized
exec ./.venv/bin/python3 send_topic_message.py
