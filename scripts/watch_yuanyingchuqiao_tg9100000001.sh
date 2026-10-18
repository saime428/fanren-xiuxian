#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000001.env
export XIU_FLOW_SLUG='yuanyingchuqiao'
source ./scripts/lib/account_flow_paths.sh

export TG_FLOW_SLUG='yuanyingchuqiao'
export TG_MESSAGE='.元婴出窍'
export TG_STATUS_PROBE_PATH="${TG_STATE_DIR}/status_probe.json"
export TG_WATCHDOG_MAX_AGE_SECONDS='7500'

exec ./.venv/bin/python3 watch_send_health.py
