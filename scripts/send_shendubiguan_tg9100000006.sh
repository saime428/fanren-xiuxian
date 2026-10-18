#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000007.env
export XIU_ACCOUNT_SLUG='tg9100000006'
export TG_SESSION_FALLBACK_NAME='state/tg9100000007'
export XIU_FLOW_SLUG='shendubiguan'
source ./scripts/lib/account_flow_paths.sh

source ./scripts/lib/run_shendubiguan_send.sh
