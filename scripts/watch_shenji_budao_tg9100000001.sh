#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000001.env
export XIU_FLOW_SLUG='shenji_budao'
source ./scripts/lib/account_flow_paths.sh

export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
export TG_WATCHDOG_REQUIRE_SUCCESS_STATE='0'
export TG_WATCHDOG_LOCK_STALE_SECONDS='600'

exec ./.venv/bin/python3 watch_send_health.py
