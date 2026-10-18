#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000005.env
export XIU_FLOW_SLUG='xuangu_zuoda'
source ./scripts/lib/account_flow_paths.sh

export TG_TRIGGER_TARGET_USERNAME='demo_player_05'
source ./scripts/lib/run_xuangu_zuoda.sh
