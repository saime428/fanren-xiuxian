#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# @demo_player_01 only.  This is deliberately separate from every other account and
# flow: its session, lock, attempts, reply evidence, and cooldown state all
# live under state/accounts/tg9100000001/fumofabao_xuantianzhanlingjian/.
source ./config/accounts/tg9100000001.env
export XIU_FLOW_SLUG='fumofabao_xuantianzhanlingjian'
source ./scripts/lib/account_flow_paths.sh

export TG_MESSAGE='.抚摸法宝 玄天斩灵剑'
export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"

# The two-hour interval begins only after a qualifying same-topic bot reply.
# A Telegram send without an observed reply stays in command_pending.json
# for scheduled retry; only a successful official reply updates last_success.
export TG_MIN_REPEAT_SECONDS='7200'
export TG_PRE_REPEAT_PROBE_SECONDS='0'
export TG_DISABLE_IMMEDIATE_RESEND='1'
export TG_REQUIRE_ACK_REPLY='1'
export TG_ACK_ANY_REPLY='0'
export TG_REQUIRE_SUCCESS_OR_COOLDOWN='1'
export TG_POST_SEND_WAIT_SECONDS='8'
export TG_POST_SEND_MAX_WAIT_SECONDS='120'
export TG_POST_SEND_POLL_SECONDS='5'
export TG_SCAN_LIMIT='100'
export TG_LATE_ACK_SCAN_LIMIT='200'
export TG_PENDING_SENT_MAX_AGE_SECONDS='180'

# Server success wording varies by interaction animation.  Every listed phrase
# carries the same authoritative reward tuple (默契/经验) for 玄天斩灵剑.
# Countdown remains non-success and is stored as account-scoped retry state.
export TG_SUCCESS_MARKER_TEXT='蹭了蹭你的掌心|发出一声清脆的鸣叫|在你手中微微颤动|联系更加紧密|傲娇地闪烁'
export TG_ACK_MARKER_TEXT='蹭了蹭你的掌心|发出一声清脆的鸣叫|在你手中微微颤动|联系更加紧密|傲娇地闪烁'
export TG_FAILURE_MARKER_TEXT='没有这件拥有器灵的法宝|名字输入错误'
export TG_FAILURE_RESEND_MAX='1'
export TG_COOLDOWN_MARKER_TEXT='后再与它互动|时间没到|時間沒到|冷却中|冷卻中|请在|請在|后再来|後再來|剩余|剩餘'
export TG_COOLDOWN_COUNTS_AS_SUCCESS='0'
export TG_COOLDOWN_RETRY_MAX_WAIT_SECONDS='9000'
export TG_COOLDOWN_INLINE_WAIT_SAFETY_MARGIN_SECONDS='5'

xiu_ensure_session_authorized
exec ./.venv/bin/python3 send_topic_message.py
