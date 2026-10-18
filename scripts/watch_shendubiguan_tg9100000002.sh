#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000002.env
export XIU_FLOW_SLUG='shendubiguan'
source ./scripts/lib/account_flow_paths.sh

export TG_MESSAGE='.深度闭关'
export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
export TG_WATCHDOG_MAX_AGE_SECONDS='30000'

exec ./.venv/bin/python3 watch_send_health.py
