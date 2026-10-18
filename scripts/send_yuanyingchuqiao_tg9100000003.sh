#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000003.env
export XIU_FLOW_SLUG='yuanyingchuqiao'
source ./scripts/lib/account_flow_paths.sh

source ./scripts/lib/run_yuanyingchuqiao_send.sh
