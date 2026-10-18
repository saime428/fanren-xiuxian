#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000004.env
export XIU_FLOW_SLUG='shendubiguan'
source ./scripts/lib/account_flow_paths.sh

# Judge the persistent observer by its own verified-scan evidence
# (observer_health.json); report only, never kill or restart it.
export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
export TG_WATCHDOG_MODE='observer'
export TG_WATCHDOG_MAX_AGE_SECONDS='1800'
export TG_WATCHDOG_LOG_PATH="${TG_STATE_DIR}/observer.watchdog.log"

exec ./.venv/bin/python3 watch_send_health.py
