#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000002.env
export XIU_FLOW_SLUG='xuexishanlin'
source ./scripts/lib/account_flow_paths.sh
xiu_route_miniapp_command

export TG_MESSAGE='.血洗山林'
export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
# 每 4 小时最多发一次；避免手动和定时近乎同时触发。
export TG_MIN_REPEAT_SECONDS='14400'
# Telegram 最近对这个号有明显连接抖动；给 sender 更宽的完成预算，
# 避免消息已发出但 cleanup / reply polling 因重连在 180s 内超时后被误判失败。
export TG_RUN_MAX_SECONDS='300'

xiu_ensure_session_authorized
exec ./.venv/bin/python3 send_topic_message.py
