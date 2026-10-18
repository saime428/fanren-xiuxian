#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source ./config/accounts/tg9100000001.env
if [[ -r ./config/accounts/mtproto.env ]]; then source ./config/accounts/mtproto.env; fi  # optional, as in account_flow_paths.sh
export PYTHONUNBUFFERED=1
exec ./.venv/bin/python3 scripts/run_yuanshen_return_observer.py --listen "$@"
