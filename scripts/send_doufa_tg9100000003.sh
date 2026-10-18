#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source ./config/accounts/tg9100000003.env
export XIU_FLOW_SLUG='doufa_demo_player_02'
export TG_MESSAGE='.斗法 demo_player_01'
exec ./scripts/lib/run_doufa_daily_quota.sh
