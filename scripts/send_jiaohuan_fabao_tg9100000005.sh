#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000005.env
export XIU_FLOW_SLUG='jiaohuan_fabao'
source ./scripts/lib/account_flow_paths.sh

export TG_TRIGGER_TARGET_USERNAME='demo_player_05'
export TG_AUTO_REPLY_MESSAGE='.交换 法宝'
export TG_AUTO_REPLY_PERSISTENT=1
export TG_AUTO_REPLY_RECONCILE_SECONDS=10
export TG_LOG_SKIPS=0
export TG_LOG_IDLE=0
export TG_TRIGGER_MAX_EVENT_AGE_SECONDS=600
source ./scripts/lib/run_jiaohuan.sh
