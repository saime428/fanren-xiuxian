#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec ./scripts/run_jiyin_choice_account.sh 'tg9100000006' 'tg9100000007' 'demo_player_06' '.收敛气息'
