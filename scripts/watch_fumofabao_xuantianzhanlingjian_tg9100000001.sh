#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000001.env
export XIU_FLOW_SLUG='fumofabao_xuantianzhanlingjian'
source ./scripts/lib/account_flow_paths.sh

export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
export TG_MESSAGE='.抚摸法宝 玄天斩灵剑'
# 2h cadence + 120s reply-confirmation window, with a small scheduler margin.
export TG_WATCHDOG_MAX_AGE_SECONDS='7500'

exec ./.venv/bin/python3 watch_send_health.py
