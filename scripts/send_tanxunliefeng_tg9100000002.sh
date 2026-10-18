#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

source ./config/accounts/tg9100000002.env
export XIU_FLOW_SLUG='tanxunliefeng'
source ./scripts/lib/account_flow_paths.sh

source ./scripts/lib/run_tanxunliefeng_send.sh
