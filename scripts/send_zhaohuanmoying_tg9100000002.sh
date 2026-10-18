#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000002.env
export XIU_FLOW_SLUG='zhaohuanmoying'
source ./scripts/lib/account_flow_paths.sh
xiu_route_miniapp_command

export TG_MESSAGE='.召唤魔影'
export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
# 召唤魔影的成功回覆同閉關類唔同，需要單獨納入 success markers。
export TG_SUCCESS_MARKER_TEXT='召唤成功，镇压成功|召喚成功，鎮壓成功|魔影被你成功击溃|魔影被你成功擊潰'
# A failed suppression is still an authoritative terminal server settlement.
# Record the settlement in last_result, without recording a successful reward.
export TG_SETTLEMENT_MARKER_TEXT='召唤成功，镇压失败|召喚成功，鎮壓失敗|魔影过于强大|魔影過於強大|修为暴跌|修為暴跌'
# The rift reply is a real server cooldown with a parseable duration.
export TG_COOLDOWN_MARKER_TEXT='魔域裂隙尚未平复|魔域裂隙尚未平復|后再行召唤|後再行召喚'
export TG_DISABLE_IMMEDIATE_RESEND='1'
# 每 8 小时最多发一次；避免手动和定时近乎同时触发。
export TG_MIN_REPEAT_SECONDS='28800'
# Telegram 最近对这个号有明显连接抖动；给 sender 更宽的完成预算，
# 避免消息已发出但 cleanup / reply polling 因重连在 180s 内超时后被误判失败。
export TG_RUN_MAX_SECONDS='300'

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
