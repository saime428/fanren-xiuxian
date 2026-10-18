# Shared body for the Nanlonghou trade-choice listeners (jiaohuan_fabao and
# jiaohuan_gongfa). Wrappers source this after setting the account env,
# XIU_FLOW_SLUG and the one per-account value TG_TRIGGER_TARGET_USERNAME.
# The registry and nanlonghou_trade.expected_trade_flow() decide which account
# answers with which choice; auto_reply_topic_event.py re-validates it.
case "${XIU_FLOW_SLUG}" in
  jiaohuan_fabao) trade_choice='.交换 法宝' ;;
  jiaohuan_gongfa) trade_choice='.交换 功法' ;;
  *)
    echo "Unsupported Nanlonghou trade flow: ${XIU_FLOW_SLUG}" >&2
    exit 64
    ;;
esac
export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
export TG_AUTO_REPLY_MESSAGE="${trade_choice}"
# Only the actionable choice is a send target. The account-bound monitor
# separately records the announcement and official cross-group result.
export TG_TRIGGER_MARKER_TEXT="${TG_TRIGGER_MARKER_TEXT:-南陇侯|回复本消息|${trade_choice}}"
export TG_TRIGGER_TRUSTED_SENDER_IDS="${TG_TRIGGER_TRUSTED_SENDER_IDS:-7228205882,7965897083,8735907987,8816935632,8918867302,8944702077,8964348409,8980154525,8788895566,8917921351,8388633812,8861328042}"
export TG_TOPIC_MATCH_MODE='top'
export TG_SCAN_LIMIT="${TG_SCAN_LIMIT:-260}"
export TG_TRIGGER_MAX_EVENT_AGE_SECONDS='600'
export TG_AUTO_REPLY_PERSISTENT='1'
export TG_AUTO_REPLY_RECONCILE_SECONDS='10'
export TG_LOG_SKIPS="${TG_LOG_SKIPS:-0}"
export TG_LOG_IDLE="${TG_LOG_IDLE:-0}"

xiu_ensure_session_authorized
exec ./.venv/bin/python3 auto_reply_topic_event.py
