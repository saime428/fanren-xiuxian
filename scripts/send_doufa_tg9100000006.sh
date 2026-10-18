#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# @demo_player_06 business account. Its authorized credentials/session seed remain
# under the historical source slug tg9100000007.
source ./config/accounts/tg9100000007.env
export XIU_ACCOUNT_SLUG='tg9100000006'
export XIU_FLOW_SLUG='doufa_demo_player_02'
export TG_SESSION_FALLBACK_NAME='state/tg9100000007'
export TG_MESSAGE='.斗法 demo_player_01'
exec ./scripts/lib/run_doufa_daily_quota.sh
