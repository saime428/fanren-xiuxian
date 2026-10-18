#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# This is one coordinated two-account flow. Runtime evidence belongs to the
# initiating business actor while each Telegram identity keeps its own sqlite.
# The pair comes from the accounts config: the shuangxiu_wenyang account and
# its "partner" option.
read -r initiator initiator_source initiator_seed partner partner_source partner_seed < <(./.venv/bin/python3 - <<'PY'
from miniapp_shared.accounts import CREDENTIAL_SOURCE, DWELLING_SESSIONS
from shuangxiu_wenyang_reply import INITIATOR_ACCOUNT, PARTNER_ACCOUNT
print(*(value for account in (INITIATOR_ACCOUNT, PARTNER_ACCOUNT) for value in
        (account, CREDENTIAL_SOURCE[account], DWELLING_SESSIONS[account].removesuffix('.session'))))
PY
)

source "./config/accounts/${initiator_source}.env"
if [[ -r ./config/accounts/mtproto.env ]]; then source ./config/accounts/mtproto.env; fi  # optional, as in account_flow_paths.sh

export TG_STATE_DIR="state/accounts/${initiator}/shuangxiu_wenyang"
mkdir -p "${TG_STATE_DIR}"

export INITIATOR_API_ID="${TG_API_ID}"
export INITIATOR_API_HASH="${TG_API_HASH}"
export INITIATOR_SESSION_NAME="state/accounts/${initiator}/shuangxiu_wenyang/session"
export TG_SESSION_NAME="${INITIATOR_SESSION_NAME}"
export TG_SESSION_FALLBACK_NAME="${initiator_seed}"
./.venv/bin/python3 ./scripts/ensure_session_authorized.py

source "./config/accounts/${partner_source}.env"
if [[ -r ./config/accounts/mtproto.env ]]; then source ./config/accounts/mtproto.env; fi  # optional, as in account_flow_paths.sh
export PARTNER_API_ID="${TG_API_ID}"
export PARTNER_API_HASH="${TG_API_HASH}"
export PARTNER_SESSION_NAME="state/accounts/${partner}/shuangxiu_wenyang/session"
export TG_SESSION_NAME="${PARTNER_SESSION_NAME}"
export TG_SESSION_FALLBACK_NAME="${partner_seed}"
./.venv/bin/python3 ./scripts/ensure_session_authorized.py

if [[ -r ./config/accounts/game.env ]]; then source ./config/accounts/game.env; fi
export TG_TARGET="${XIU_GAME_TARGET:-@example_game_group}"
export TG_TOPIC_ID="${XIU_GAME_TOPIC_ID:-7310786}"
export TG_MIN_REPEAT_SECONDS='3600'
export TG_ACTION_REPLY_TIMEOUT_SECONDS='180'
export TG_REPLY_POLL_SECONDS='2'
export TG_SCAN_LIMIT='120'

exec ./.venv/bin/python3 ./shuangxiu_wenyang.py
