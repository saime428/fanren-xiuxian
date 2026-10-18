#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000007.env
export XIU_ACCOUNT_SLUG='tg9100000006'
export TG_SESSION_FALLBACK_NAME='state/tg9100000007'
export XIU_FLOW_SLUG='wendao'
source ./scripts/lib/account_flow_paths.sh
xiu_route_miniapp_command

export TG_MESSAGE='.问道'
export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
export TG_MIN_REPEAT_SECONDS='43200'
export TG_PRE_REPEAT_PROBE_SECONDS='0'
export TG_COOLDOWN_RETRY_MAX_WAIT_SECONDS='9000'
export TG_COOLDOWN_INLINE_WAIT_SAFETY_MARGIN_SECONDS='5'
export TG_COOLDOWN_MARKER_TEXT='天机不可频繁窥探|天機不可頻繁窺探|请在|請在|后再来问道|後再來問道|后再问道|後再問道|后再来|後再來|冷却中|冷卻中|还不能|還不能|还没好|還沒好|剩余|剩餘'
export TG_SUCCESS_MARKER_TEXT='【问道得宝】|【問道得寶】|冷却时间被重置|可以立刻再次闭关|毫无疲惫之感|可以立刻再次问道|问道成功|問道成功'
export TG_REQUIRE_SUCCESS_OR_COOLDOWN='1'
export TG_REQUIRE_ACK_REPLY='1'
export TG_DISABLE_IMMEDIATE_RESEND='1'

xiu_ensure_session_authorized
exec ./.venv/bin/python3 send_topic_message.py
