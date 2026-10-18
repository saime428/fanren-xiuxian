#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
source ./config/accounts/tg9100000001.env
export XIU_ACCOUNT_SLUG='tg9100000001'
export XIU_FLOW_SLUG='recipe_notice'
source ./scripts/lib/account_flow_paths.sh
export TG_TARGET='@example_game_group'
export TG_TOPIC_ID='7310786'
export TG_RECIPE_RECONCILE_SECONDS='3600'
export TG_RECIPE_HISTORY_LIMIT='500'
export TG_RECIPE_DELIVERY_TARGET='telegram:9100000001'
export TG_RECIPE_TARGET_USERNAMES='demo_player_01,demo_player_03'
export HERMES_SEND_BIN='/Users/operator/.hermes/hermes-agent/venv/bin/hermes'
# Verified game senders come from command_confirmation.OFFICIAL_REPLY_SENDER_IDS.
export PYTHONUNBUFFERED='1'

xiu_ensure_session_authorized
exec ./.venv/bin/python3 -u ./recipe_notice_monitor.py \
  --account tg9100000001 \
  --flow recipe_notice \
  --state-dir state/accounts/tg9100000001/recipe_notice
