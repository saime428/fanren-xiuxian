#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec ./.venv/bin/python3 -B -m treasure_hunt.run_cycle --account tg9100000002
