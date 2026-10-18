#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec ./scripts/run_jiyin_choice_account.sh 'tg9100000002' 'tg9100000002' 'demo_player_02' '.收敛气息'
