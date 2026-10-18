#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000001.env
export XIU_FLOW_SLUG='jiaohuan_gongfa'
source ./scripts/lib/account_flow_paths.sh

export TG_TRIGGER_TARGET_USERNAME='demo_player_01'
source ./scripts/lib/run_jiaohuan.sh
