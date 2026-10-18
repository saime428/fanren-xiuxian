#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec ./.venv/bin/python3 -B -m tianxing_exploration.run_cycle --account tg9100000001
